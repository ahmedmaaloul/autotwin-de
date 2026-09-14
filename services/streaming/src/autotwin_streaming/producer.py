"""The Kafka producer AutoTwin publishes every event through (BUILD_SPEC §8).

One class covers all four topics because the envelope, the serialisation and the counters are
identical across them; only the key extraction differs, and that is one line per payload type.
Splitting it into four producers would mean four TCP connections and four sets of metrics for
one logical stream of ~40 messages a second.

Two decisions are worth stating up front, because both are the kind that look wrong until you
know the workload:

**``acks=1``, not ``acks=all``.** The payload is *simulated* telemetry whose system of record
is PostgreSQL, and the next sample for the same vehicle arrives a tick later. Losing one point
to a leader failure costs a pixel on a map; waiting for every in-sync replica to acknowledge
costs latency on every one of the millions of points that do not fail. On the single-broker
development cluster ``acks=all`` is additionally indistinguishable from ``acks=1`` — there is
no second replica — so the setting only starts to mean anything on a cluster this project does
not run, and it would be the wrong meaning. A pipeline carrying payments or safety-relevant
signals would choose differently, and this docstring is where that reasoning would be revisited.

**Fire-and-collect, not fire-and-forget.** ``send()`` returns a future; ``send_and_wait()``
blocks until the broker answers, which would serialise the simulator's tick loop behind a
network round trip. This producer awaits the *batch* rather than each message: it keeps the
futures, and :meth:`TelemetryProducer.flush` resolves them so that a delivery failure is
counted and logged rather than lost to a garbage-collected future.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any, Final, Self

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from autotwin_contracts import (
    EVENT_TYPE_CHARGING,
    EVENT_TYPE_TELEMETRY,
    EVENT_TYPE_TRAFFIC,
    EVENT_TYPE_TRIP,
    TOPIC_CHARGING_EVENTS,
    TOPIC_TELEMETRY,
    TOPIC_TRAFFIC_EVENTS,
    TOPIC_TRIP_EVENTS,
    ChargingEvent,
    DataOrigin,
    EventEnvelope,
    TelemetryEvent,
    TrafficEventMessage,
    TripEvent,
)
from autotwin_core.errors import ProviderUnavailable
from autotwin_core.logging import get_logger
from autotwin_streaming.config import PRODUCER_NAME, StreamingConfig

__all__ = ["ProducerStats", "TelemetryProducer"]

_LOGGER = get_logger(__name__)

_UTF8: Final[str] = "utf-8"


@dataclass(slots=True)
class ProducerStats:
    """Delivery counters, scraped by ``GET /metrics`` (BUILD_SPEC §7).

    Mutable and owned by exactly one :class:`TelemetryProducer`; :meth:`snapshot` hands out an
    immutable copy so a metrics endpoint cannot observe a half-updated set of numbers.
    """

    messages_sent: int = 0
    """Messages the broker acknowledged."""

    messages_failed: int = 0
    """Messages the broker rejected or never acknowledged."""

    bytes_sent: int = 0
    """Serialised envelope bytes handed to the client, excluding protocol overhead."""

    batches: int = 0
    """Number of :meth:`TelemetryProducer.flush` cycles that resolved at least one send."""

    by_topic: dict[str, int] = field(default_factory=dict)
    """Acknowledged messages per topic — the breakdown a Grafana panel splits on."""

    last_error: str | None = None
    """Class name of the most recent delivery failure, or ``None`` if there has been none."""

    def snapshot(self) -> ProducerStats:
        """An independent copy, safe to serialise while the producer keeps running."""
        return ProducerStats(
            messages_sent=self.messages_sent,
            messages_failed=self.messages_failed,
            bytes_sent=self.bytes_sent,
            batches=self.batches,
            by_topic=dict(self.by_topic),
            last_error=self.last_error,
        )

    def as_dict(self) -> dict[str, Any]:
        """Flat mapping for the ``kafka`` block of ``GET /api/v1/simulations/status``."""
        return {
            "messages_sent": self.messages_sent,
            "messages_failed": self.messages_failed,
            "bytes_sent": self.bytes_sent,
            "batches": self.batches,
            "by_topic": dict(self.by_topic),
            "last_error": self.last_error,
        }


class TelemetryProducer:
    """Publishes AutoTwin events to Redpanda, wrapping each payload in an
    :class:`~autotwin_contracts.EventEnvelope`.

    Not safe to share between event loops; one instance per process is the intended shape, and
    :class:`~autotwin_streaming.sinks.KafkaTelemetrySink` owns it.
    """

    def __init__(
        self,
        config: StreamingConfig | None = None,
        *,
        producer_name: str = PRODUCER_NAME,
        data_origin: DataOrigin = DataOrigin.simulated,
    ) -> None:
        """Configure the producer without connecting; :meth:`start` opens the connection.

        ``data_origin`` is a constructor argument rather than a constant so that a future
        producer of *official* data cannot publish it under the simulator's honesty marker
        (BUILD_SPEC §0.2) simply by reusing this class.
        """
        self._config = config if config is not None else StreamingConfig.from_settings()
        self._producer_name = producer_name
        self._data_origin = data_origin
        self._producer: AIOKafkaProducer | None = None
        self._pending: list[asyncio.Future[Any]] = []
        self._pending_topics: list[tuple[str, int]] = []
        self._stats = ProducerStats()

    # ------------------------------------------------------------------ lifecycle

    @property
    def is_running(self) -> bool:
        """Whether the underlying client is connected."""
        return self._producer is not None

    @property
    def stats(self) -> ProducerStats:
        """Immutable snapshot of the delivery counters."""
        return self._stats.snapshot()

    @property
    def config(self) -> StreamingConfig:
        """The transport configuration this producer was built with."""
        return self._config

    async def start(self) -> None:
        """Connect to the broker. Idempotent.

        Raises :class:`~autotwin_core.errors.ProviderUnavailable` when the broker cannot be
        reached, so that a caller can fall back to the database sink instead of discovering the
        problem one message at a time.
        """
        if self._producer is not None:
            return
        producer = AIOKafkaProducer(
            bootstrap_servers=self._config.bootstrap_servers,
            client_id=self._config.component_client_id("producer"),
            acks=1,
            linger_ms=self._config.linger_ms,
            max_batch_size=self._config.max_batch_size_bytes,
            compression_type=self._config.compression_type,
            request_timeout_ms=self._config.request_timeout_ms,
        )
        try:
            await producer.start()
        except (KafkaError, OSError) as exc:
            msg = f"Kafka broker at {self._config.bootstrap_servers} is unreachable"
            raise ProviderUnavailable(msg, details={"error": type(exc).__name__}) from exc
        self._producer = producer
        _LOGGER.info(
            "kafka.producer.started",
            bootstrap_servers=self._config.bootstrap_servers,
            linger_ms=self._config.linger_ms,
            compression=self._config.compression_type,
        )

    async def stop(self) -> None:
        """Flush outstanding sends and close the connection. Idempotent."""
        producer = self._producer
        if producer is None:
            return
        try:
            await self.flush()
        finally:
            self._producer = None
            await producer.stop()
        _LOGGER.info("kafka.producer.stopped", **self._stats.as_dict())

    async def __aenter__(self) -> Self:
        """Start the producer for use in an ``async with`` block."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always stop the producer, even when the block raised."""
        await self.stop()

    # -------------------------------------------------------------------- sending

    async def send_telemetry(self, event: TelemetryEvent) -> None:
        """Publish one telemetry sample on ``vehicle.telemetry.v1``, keyed by ``vehicle_id``.

        Keying by vehicle is what guarantees a single vehicle's samples land on one partition
        and therefore arrive in order — without it, odometer and SOC could go backwards for a
        consumer reading two partitions at different speeds.
        """
        await self._publish(
            topic=TOPIC_TELEMETRY,
            key=event.vehicle_id,
            payload=event,
            event_type=EVENT_TYPE_TELEMETRY,
            occurred_at_from=event.recorded_at,
        )

    async def send_telemetry_many(self, events: Sequence[TelemetryEvent]) -> None:
        """Publish a tick's worth of samples, then resolve the whole batch at once.

        The per-message awaits below only enqueue into the client's accumulator; the single
        :meth:`flush` at the end is the one network wait. That is the entire reason the
        simulator can tick forty vehicles without its loop being paced by the broker.
        """
        for event in events:
            await self.send_telemetry(event)
        await self.flush()

    async def send_trip_event(self, event: TripEvent) -> None:
        """Publish a trip lifecycle transition on ``vehicle.trip-events.v1``, keyed by
        ``trip_id``."""
        await self._publish(
            topic=TOPIC_TRIP_EVENTS,
            key=event.trip_id,
            payload=event,
            event_type=EVENT_TYPE_TRIP,
            occurred_at_from=event.occurred_at,
        )

    async def send_traffic_event(
        self,
        event: TrafficEventMessage,
        *,
        data_origin: DataOrigin = DataOrigin.official,
    ) -> None:
        """Publish a traffic disruption on ``traffic.events.v1``, keyed by ``external_id``.

        Overrides the producer's default origin because a traffic report comes from Autobahn
        GmbH, not from the simulator, and the envelope has to say so even when this process
        publishes everything else as ``simulated``.
        """
        await self._publish(
            topic=TOPIC_TRAFFIC_EVENTS,
            key=event.external_id,
            payload=event,
            event_type=EVENT_TYPE_TRAFFIC,
            occurred_at_from=event.observed_at,
            data_origin=data_origin,
        )

    async def send_charging_event(self, event: ChargingEvent) -> None:
        """Publish a charging session phase on ``charging.events.v1``, keyed by ``station_id``."""
        await self._publish(
            topic=TOPIC_CHARGING_EVENTS,
            key=event.station_id,
            payload=event,
            event_type=EVENT_TYPE_CHARGING,
            occurred_at_from=event.occurred_at,
        )

    async def _publish[PayloadT](
        self,
        *,
        topic: str,
        key: str,
        payload: PayloadT,
        event_type: str,
        occurred_at_from: datetime,
        data_origin: DataOrigin | None = None,
    ) -> None:
        """Wrap, serialise and enqueue one message.

        ``occurred_at`` is taken from the payload's own timestamp rather than from the clock:
        the simulator runs at a speed factor, so "when it happened" in simulated time and "when
        it was produced" in wall-clock time are different numbers, and the envelope documents
        the former (BUILD_SPEC §8).
        """
        producer = self._require_producer()
        envelope: EventEnvelope[PayloadT] = EventEnvelope.wrap(
            payload,
            event_type=event_type,
            producer=self._producer_name,
            data_origin=data_origin if data_origin is not None else self._data_origin,
            occurred_at=occurred_at_from,
        )
        value = envelope.model_dump_json().encode(_UTF8)
        try:
            future = await producer.send(topic, value=value, key=key.encode(_UTF8))
        except (KafkaError, OSError) as exc:
            self._stats.messages_failed += 1
            self._stats.last_error = type(exc).__name__
            _LOGGER.error("kafka.producer.enqueue_failed", topic=topic, error=str(exc))
            msg = f"could not enqueue a message on {topic}"
            raise ProviderUnavailable(msg, details={"error": type(exc).__name__}) from exc
        self._pending.append(future)
        self._pending_topics.append((topic, len(value)))

    async def flush(self) -> None:
        """Wait for every enqueued message to be acknowledged and update the counters.

        Failures are counted and logged, never raised: the caller is a simulator tick loop, and
        aborting a simulation because one telemetry point did not reach a broker would be a
        worse outcome than the gap it leaves in a chart. A persistent outage still surfaces —
        through ``messages_failed`` on ``/metrics`` and an ``error`` line per failed batch.
        """
        if not self._pending:
            return
        futures = self._pending
        topics = self._pending_topics
        self._pending = []
        self._pending_topics = []

        results = await asyncio.gather(*futures, return_exceptions=True)
        failures: dict[str, int] = {}
        for result, (topic, size) in zip(results, topics, strict=True):
            if isinstance(result, BaseException):
                self._stats.messages_failed += 1
                self._stats.last_error = type(result).__name__
                failures[type(result).__name__] = failures.get(type(result).__name__, 0) + 1
                continue
            self._stats.messages_sent += 1
            self._stats.bytes_sent += size
            self._stats.by_topic[topic] = self._stats.by_topic.get(topic, 0) + 1
        self._stats.batches += 1
        if failures:
            _LOGGER.error(
                "kafka.producer.delivery_failed",
                failed=sum(failures.values()),
                delivered=len(results) - sum(failures.values()),
                errors=failures,
            )

    def _require_producer(self) -> AIOKafkaProducer:
        """Return the started client or explain, once, that nobody called :meth:`start`."""
        if self._producer is None:
            msg = "TelemetryProducer.start() must be awaited before publishing"
            raise ProviderUnavailable(msg)
        return self._producer
