"""The telemetry consumer: Redpanda in, PostGIS out (BUILD_SPEC §8).

Consumer group ``autotwin-telemetry-consumer``. It reads ``vehicle.telemetry.v1``, validates
each message against the Pydantic contract, buffers up to 500 rows or one second, writes the
batch in a single transaction and **only then** commits its offsets.

Three properties are the whole design, and each one is a decision someone has to live with:

**At-least-once, never at-most-once.** ``enable_auto_commit`` is off. A crash between the write
and the commit replays the batch, which is safe because ``telemetry`` is append-only and every
aggregate this consumer maintains is written from an absolute value rather than accumulated
(see :func:`~autotwin_streaming.sinks.write_telemetry_batch`). The opposite failure — committing
first and losing the rows — is not recoverable at all.

**One bad message never stops the loop.** A payload that fails validation is counted, logged
with its topic/partition/offset so it can be found in the log, and skipped. The alternative is a
consumer that a single malformed producer can take offline, and the point of a transport is that
it keeps running.

**Shutdown flushes.** SIGINT and SIGTERM set an event; the loop finishes its current fetch,
writes what it has buffered, commits, and closes. ``Ctrl-C`` during a demo therefore costs at
most one batch interval, not a batch of data.
"""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Self

import sqlalchemy as sa
from aiokafka import AIOKafkaConsumer, ConsumerRecord, TopicPartition
from aiokafka.errors import KafkaError, UnsupportedCodecError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from autotwin_contracts import (
    TOPIC_TELEMETRY,
    TOPIC_TRIP_EVENTS,
    TelemetryEnvelope,
    TripEnvelope,
)
from autotwin_core.db.session import get_engine, session_scope
from autotwin_core.errors import ConfigurationMissing, ProviderUnavailable
from autotwin_core.logging import get_logger
from autotwin_streaming.config import StreamingConfig
from autotwin_streaming.sinks import (
    BatchPolicy,
    TelemetryBatchWriter,
    apply_trip_event,
    write_telemetry_batch,
)

__all__ = ["ConsumerStats", "TelemetryConsumer", "run_consumer"]

_LOGGER = get_logger(__name__)

_SHUTDOWN_SIGNALS: Final[tuple[signal.Signals, ...]] = (signal.SIGINT, signal.SIGTERM)
"""Signals that mean "stop cleanly": ``Ctrl-C`` and what Docker/Kubernetes send on stop."""

_MAX_LOGGED_VALIDATION_ERRORS: Final[int] = 5
"""Validation errors written to the log per bad message.

A message that is wrong in forty ways is wrong for one reason, and printing all forty turns one
bad producer into an unreadable log.
"""

_CONSUMABLE_TOPICS: Final[frozenset[str]] = frozenset({TOPIC_TELEMETRY, TOPIC_TRIP_EVENTS})
"""Topics this consumer knows how to persist.

``traffic.events.v1`` and ``charging.events.v1`` are published for downstream consumers and for
the Redpanda Console; nothing in AutoTwin persists them from the log today, and pretending to
handle them here would be exactly the stub theatre BUILD_SPEC §0.4 rules out.
"""


def _describe_validation_errors(error: PydanticValidationError) -> list[str]:
    """Render Pydantic's error list as plain strings, e.g. ``payload.speed_kmh: ...``.

    ``ValidationError.errors()`` nests the offending input and, for some error types, live
    objects. Those serialise unpredictably into a JSON log line, so the log gets a location and
    a message and nothing else.
    """
    return [
        f"{'.'.join(str(part) for part in entry['loc'])}: {entry['msg']}"
        for entry in error.errors()[:_MAX_LOGGED_VALIDATION_ERRORS]
    ]


@dataclass(slots=True)
class ConsumerStats:
    """What the consumer has done — read by ``GET /api/v1/simulations/status`` (BUILD_SPEC §7).

    Mutable and owned by one :class:`TelemetryConsumer`; :meth:`snapshot` hands out a copy.
    """

    messages_consumed: int = 0
    """Records fetched from the broker, valid or not."""

    rows_written: int = 0
    """Telemetry rows successfully inserted."""

    trip_events_applied: int = 0
    """Trip lifecycle transitions applied to ``trips``/``vehicles``."""

    batches: int = 0
    """Batches committed — one transaction and one offset commit each."""

    invalid_messages: int = 0
    """Records rejected by Pydantic or carrying a payload for the wrong topic."""

    errors: int = 0
    """Database or broker failures the loop recovered from by retrying the batch."""

    lag: int | None = None
    """Records behind the end of the log at the last commit, summed over assigned partitions.

    ``None`` until the first commit, and ``None`` again whenever the broker declines to report a
    high-water mark — an honest "unknown" rather than a zero that would read as "caught up".
    """

    last_commit_at: float | None = None
    """``time.monotonic()`` of the last successful commit; used to derive an idle duration."""

    partitions: list[str] = field(default_factory=list)
    """Currently assigned ``topic-partition`` names, for the status page."""

    def snapshot(self) -> ConsumerStats:
        """An independent copy, safe to serialise while the loop keeps running."""
        return ConsumerStats(
            messages_consumed=self.messages_consumed,
            rows_written=self.rows_written,
            trip_events_applied=self.trip_events_applied,
            batches=self.batches,
            invalid_messages=self.invalid_messages,
            errors=self.errors,
            lag=self.lag,
            last_commit_at=self.last_commit_at,
            partitions=list(self.partitions),
        )

    def as_dict(self) -> dict[str, Any]:
        """Flat mapping for the simulation-status payload."""
        return {
            "messages_consumed": self.messages_consumed,
            "rows_written": self.rows_written,
            "trip_events_applied": self.trip_events_applied,
            "batches": self.batches,
            "invalid_messages": self.invalid_messages,
            "errors": self.errors,
            "lag": self.lag,
            "partitions": list(self.partitions),
        }


class TelemetryConsumer:
    """Reads AutoTwin's event topics and persists them to PostGIS.

    One instance drives one asyncio task. Scaling is horizontal: start a second process in the
    same consumer group and the broker splits the partitions between them, with per-vehicle
    ordering preserved because ``vehicle_id`` is the partition key.
    """

    def __init__(
        self,
        config: StreamingConfig | None = None,
        *,
        topics: Sequence[str] = (TOPIC_TELEMETRY,),
        from_beginning: bool = False,
    ) -> None:
        """Configure the consumer without connecting.

        ``topics`` defaults to telemetry alone, which is what BUILD_SPEC §8 specifies. Adding
        ``vehicle.trip-events.v1`` is supported because without it nothing closes a trip on the
        Kafka path — the direct-to-database sink applies those transitions in process, and the
        two transports must end in the same database state.
        """
        self._config = config if config is not None else StreamingConfig.from_settings()
        if not topics:
            msg = "at least one topic is required"
            raise ValueError(msg)
        unknown = [topic for topic in topics if topic not in _CONSUMABLE_TOPICS]
        if unknown:
            known = ", ".join(sorted(_CONSUMABLE_TOPICS))
            msg = f"no handler for topic(s) {', '.join(unknown)}; this consumer reads: {known}"
            raise ValueError(msg)
        self._topics = tuple(topics)
        self._from_beginning = from_beginning
        self._consumer: AIOKafkaConsumer | None = None
        self._writer = TelemetryBatchWriter(BatchPolicy.from_config(self._config))
        self._stats = ConsumerStats()
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle

    @property
    def stats(self) -> ConsumerStats:
        """Immutable snapshot of the consumer counters."""
        return self._stats.snapshot()

    @property
    def topics(self) -> tuple[str, ...]:
        """Topics this consumer is subscribed to."""
        return self._topics

    @property
    def config(self) -> StreamingConfig:
        """The transport configuration this consumer was built with."""
        return self._config

    async def start(self) -> None:
        """Join the consumer group and open the database engine. Idempotent."""
        if self._consumer is not None:
            return
        # Build the engine before the first session_scope(): it is the point where a wrong DSN
        # should fail, and it must not happen for the first time inside the write path of a
        # batch whose offsets are already fetched.
        get_engine()
        consumer = AIOKafkaConsumer(
            *self._topics,
            bootstrap_servers=self._config.bootstrap_servers,
            client_id=self._config.component_client_id("consumer"),
            group_id=self._config.group_id,
            # Offsets are committed by hand after the database write; see the module docstring.
            enable_auto_commit=False,
            auto_offset_reset="earliest" if self._from_beginning else "latest",
            request_timeout_ms=self._config.request_timeout_ms,
            session_timeout_ms=self._config.session_timeout_ms,
            max_poll_interval_ms=self._config.max_poll_interval_ms,
            # Never fetch more than one batch's worth in a single call: the buffer is bounded by
            # batch_size, and a larger fetch would only sit in memory waiting to be flushed.
            max_poll_records=self._config.batch_size,
        )
        try:
            await consumer.start()
        except (KafkaError, OSError) as exc:
            msg = f"Kafka broker at {self._config.bootstrap_servers} is unreachable"
            raise ProviderUnavailable(msg, details={"error": type(exc).__name__}) from exc
        self._consumer = consumer
        _LOGGER.info(
            "kafka.consumer.started",
            topics=list(self._topics),
            group_id=self._config.group_id,
            batch_size=self._config.batch_size,
            batch_interval_s=self._config.batch_interval_s,
            from_beginning=self._from_beginning,
        )

    async def stop(self) -> None:
        """Flush the current batch, commit, and leave the group. Idempotent."""
        consumer = self._consumer
        if consumer is None:
            return
        self._consumer = None
        try:
            if self._writer.pending:
                await self._flush_and_commit(consumer)
        finally:
            await consumer.stop()
        _LOGGER.info("kafka.consumer.stopped", **self._stats.as_dict())

    def request_stop(self) -> None:
        """Ask the loop to finish its current fetch and shut down.

        Safe to call from a signal handler: setting an :class:`asyncio.Event` is the only
        operation the loop needs, and it is the one thing that is documented as loop-safe from
        ``loop.add_signal_handler``.
        """
        self._stop.set()

    async def __aenter__(self) -> Self:
        """Start the consumer for use in an ``async with`` block."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always stop the consumer, even when the block raised."""
        await self.stop()

    # ----------------------------------------------------------------------- loop

    async def run(self) -> ConsumerStats:
        """Consume until :meth:`request_stop` is called, then flush and return the counters."""
        consumer = self._require_consumer()
        while not self._stop.is_set():
            await self._consume_once(consumer)
        if self._writer.pending:
            await self._flush_and_commit(consumer)
        return self.stats

    async def _consume_once(self, consumer: AIOKafkaConsumer) -> None:
        """Fetch for at most one batch window, then flush if the policy says the batch is due.

        The fetch is *not* raced against the stop event. Cancelling a ``getmany`` in flight
        would leave the consumer's internal position ahead of what this loop actually buffered,
        and the records in that gap would be skipped on the next commit. Waiting out the
        remaining window instead bounds shutdown latency at one batch interval — a second — for
        a guarantee that data is not silently dropped, which is a trade worth making every time.
        """
        timeout_ms = max(1, int(self._remaining_window_s() * 1000))
        try:
            fetched = await consumer.getmany(
                timeout_ms=timeout_ms,
                max_records=max(1, self._config.batch_size - self._writer.pending),
            )
        except UnsupportedCodecError as exc:
            # Not a transient failure and not a bad *message*: the broker served a record batch
            # this build cannot decompress, so the partition cannot advance past it no matter
            # how often the fetch is retried. Retrying would be an infinite loop that looks like
            # a hung consumer, so it is reported as the deployment problem it is and the process
            # exits — a supervisor restart with the codec library present is the actual fix.
            self._stats.errors += 1
            msg = (
                "the broker served a record batch in a compression codec this build cannot "
                "decode; install the optional codec libraries (cramjam provides snappy, lz4 "
                "and zstd) or purge the affected topic. Retrying cannot help."
            )
            _LOGGER.error("kafka.consumer.codec_unsupported", detail=str(exc))
            raise ConfigurationMissing(msg, details={"error": str(exc)}) from exc
        except (KafkaError, OSError) as exc:
            self._stats.errors += 1
            _LOGGER.error("kafka.consumer.fetch_failed", error=type(exc).__name__, detail=str(exc))
            # aiokafka reconnects on its own; backing off keeps a hard-down broker from
            # spinning this loop at the speed of the event loop.
            await asyncio.sleep(min(self._config.batch_interval_s, 1.0))
            return

        for records in fetched.values():
            await self._ingest(records)

        if self._writer.is_due():
            await self._flush_and_commit(consumer)

    def _remaining_window_s(self) -> float:
        """Seconds left before the buffered batch must be flushed.

        A full interval when nothing is buffered — there is no deadline to miss — and whatever
        is left of the interval once the batch has been opened.
        """
        if self._writer.pending == 0:
            return self._config.batch_interval_s
        return max(0.0, self._config.batch_interval_s - self._writer.age_s)

    async def _ingest(self, records: Iterable[ConsumerRecord[bytes, bytes]]) -> None:
        """Validate and route one partition's worth of records."""
        for record in records:
            self._stats.messages_consumed += 1
            if record.topic == TOPIC_TELEMETRY:
                self._ingest_telemetry(record)
            elif record.topic == TOPIC_TRIP_EVENTS:
                await self._ingest_trip_event(record)
            else:  # pragma: no cover - the subscription is validated in __init__
                self._reject(record, "unhandled topic")

    def _ingest_telemetry(self, record: ConsumerRecord[bytes, bytes]) -> None:
        """Validate one telemetry message and buffer its payload."""
        envelope = self._parse(record, TelemetryEnvelope)
        if envelope is not None:
            self._writer.add(envelope.payload)

    async def _ingest_trip_event(self, record: ConsumerRecord[bytes, bytes]) -> None:
        """Validate one trip transition and apply it, after the telemetry that precedes it.

        Flushing the telemetry buffer first is what keeps a trip's final samples inside its
        totals: :func:`~autotwin_streaming.sinks.write_telemetry_batch` will not touch a trip
        that already has an ``ended_at``.
        """
        envelope = self._parse(record, TripEnvelope)
        if envelope is None:
            return
        if self._writer.pending:
            consumer = self._require_consumer()
            await self._flush_and_commit(consumer)
        try:
            async with session_scope() as session:
                await apply_trip_event(session, envelope.payload)
        except sa.exc.SQLAlchemyError as exc:
            self._stats.errors += 1
            _LOGGER.error(
                "kafka.consumer.trip_event_failed",
                trip_id=envelope.payload.trip_id,
                event_type=envelope.payload.event_type.value,
                error=type(exc).__name__,
                detail=str(exc),
            )
            return
        self._stats.trip_events_applied += 1

    def _parse[ModelT: BaseModel](
        self,
        record: ConsumerRecord[bytes, bytes],
        model: type[ModelT],
    ) -> ModelT | None:
        """Validate one record's bytes, returning ``None`` for a message that must be skipped."""
        if record.value is None:
            self._reject(record, "message has no value")
            return None
        try:
            return model.model_validate_json(record.value)
        except PydanticValidationError as exc:
            self._reject(
                record,
                f"{exc.error_count()} validation error(s)",
                errors=_describe_validation_errors(exc),
            )
            return None
        except ValueError as exc:
            # Malformed UTF-8 or invalid JSON — Pydantic raises ValueError, not its own class.
            self._reject(record, f"undecodable payload: {exc}")
            return None

    def _reject(
        self,
        record: ConsumerRecord[bytes, bytes],
        reason: str,
        **context: Any,
    ) -> None:
        """Count and log a message that cannot be persisted, then move on.

        The topic/partition/offset triple is logged deliberately: it is the only way to find the
        exact bytes again with ``rpk topic consume -o <offset>``, and a dead-letter topic would
        be a second thing to operate for a stream whose source is a program in this repository.
        """
        self._stats.invalid_messages += 1
        _LOGGER.warning(
            "kafka.consumer.invalid_message",
            topic=record.topic,
            partition=record.partition,
            offset=record.offset,
            key=record.key.decode("utf-8", errors="replace") if record.key else None,
            reason=reason,
            **context,
        )

    # ---------------------------------------------------------------------- write

    async def _flush_and_commit(self, consumer: AIOKafkaConsumer) -> None:
        """Write the buffered batch in one transaction, then commit the offsets.

        On a database failure nothing is committed, the batch is put *back* into the writer and
        the broker redelivers from the last committed offset on the next poll — which is what
        makes the pipeline at-least-once rather than best-effort.
        """
        batch = self._writer.take()
        if not batch:
            return
        try:
            async with session_scope() as session:
                written = await write_telemetry_batch(session, batch)
        except sa.exc.SQLAlchemyError as exc:
            self._stats.errors += 1
            _LOGGER.error(
                "kafka.consumer.write_failed",
                rows=len(batch),
                error=type(exc).__name__,
                detail=str(exc),
            )
            # Offsets stay where they are; re-buffering keeps the rows for an immediate retry
            # instead of waiting for the broker to notice we never committed.
            self._writer.add_many(batch)
            await asyncio.sleep(min(self._config.batch_interval_s, 1.0))
            return

        try:
            await consumer.commit()
        except (KafkaError, OSError) as exc:
            # The rows are durable; the offsets are not. The batch will be redelivered and
            # re-inserted, which `telemetry` tolerates by design (BUILD_SPEC §8).
            self._stats.errors += 1
            _LOGGER.error("kafka.consumer.commit_failed", error=type(exc).__name__)
            return

        self._stats.rows_written += written
        self._stats.batches += 1
        self._stats.last_commit_at = time.monotonic()
        await self._refresh_lag(consumer)
        _LOGGER.debug(
            "kafka.consumer.batch_committed",
            rows=written,
            total_rows=self._stats.rows_written,
            lag=self._stats.lag,
        )

    async def _refresh_lag(self, consumer: AIOKafkaConsumer) -> None:
        """Recompute how far behind the end of the log this consumer is.

        Best effort: ``highwater`` is only populated for partitions that have been fetched from
        at least once, so the number is reported as ``None`` rather than guessed at until the
        broker has told us where the end of the log is.
        """
        assignment: set[TopicPartition] = set(consumer.assignment())
        self._stats.partitions = sorted(f"{tp.topic}-{tp.partition}" for tp in assignment)
        if not assignment:
            self._stats.lag = None
            return
        total = 0
        known = False
        for partition in assignment:
            highwater = consumer.highwater(partition)
            if highwater is None:
                continue
            try:
                position = await consumer.position(partition)
            except (KafkaError, OSError):
                continue
            known = True
            total += max(0, highwater - position)
        self._stats.lag = total if known else None

    def _require_consumer(self) -> AIOKafkaConsumer:
        """Return the started client or explain, once, that nobody called :meth:`start`."""
        if self._consumer is None:
            msg = "TelemetryConsumer.start() must be awaited before consuming"
            raise ProviderUnavailable(msg)
        return self._consumer


async def run_consumer(
    config: StreamingConfig | None = None,
    *,
    topics: Sequence[str] = (TOPIC_TELEMETRY,),
    from_beginning: bool = False,
    install_signal_handlers: bool = True,
) -> ConsumerStats:
    """Run a consumer until it is asked to stop, handling SIGINT/SIGTERM.

    The CLI's ``consume`` command is one call to this. ``install_signal_handlers`` exists for
    the tests and for any caller that already owns the process's signal disposition — installing
    handlers from a library is only acceptable when the library *is* the program, which it is
    here and is not there.
    """
    consumer = TelemetryConsumer(config, topics=topics, from_beginning=from_beginning)
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    if install_signal_handlers:
        for sig in _SHUTDOWN_SIGNALS:
            try:
                loop.add_signal_handler(sig, consumer.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows and non-main threads cannot install loop signal handlers; the consumer
                # still stops cleanly through request_stop() from wherever the caller calls it.
                _LOGGER.debug("kafka.consumer.signal_handler_unavailable", signal=sig.name)
            else:
                installed.append(sig)

    try:
        await consumer.start()
        return await consumer.run()
    finally:
        # Runs even when start() raised, so a failed connection does not leave this process's
        # signal disposition rewritten for whatever the caller does next.
        await consumer.stop()
        for sig in installed:
            loop.remove_signal_handler(sig)
