"""The seam that makes Kafka optional (BUILD_SPEC §8).

The simulator never imports ``aiokafka``. It emits into a :class:`TelemetrySink`, and this
module decides at startup whether that sink is a Kafka producer or a direct PostGIS writer.
"*The architecture is identical; the transport is swapped*" is the spec's phrasing, and the
thing that makes it true rather than aspirational is that both implementations end at the same
rows in the same ``telemetry`` table, written by the same two functions —
:func:`write_telemetry_batch` and :func:`apply_trip_event` — that the streaming consumer uses.
There is exactly one description in this codebase of what a telemetry row means, and both paths
go through it.

The fallback is *loud*. :func:`create_sink` logs a warning whenever it wanted Kafka and could
not have it, and stamps the reason on the sink so ``/api/v1/simulations/status`` can show the
operator that they are running degraded. A silent fallback would violate BUILD_SPEC §0.2 just
as much as mislabelled data does: "the demo works without a broker" must never become "the
demo quietly stopped using the broker you started".
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_contracts import (
    DataOrigin,
    TelemetryEvent,
    TripEvent,
    TripEventType,
    VehicleState,
)
from autotwin_core.config import Settings, get_settings
from autotwin_core.db.models import Telemetry
from autotwin_core.db.session import get_engine, session_scope
from autotwin_core.db.types import to_shape_point
from autotwin_core.errors import ProviderError
from autotwin_core.logging import get_logger
from autotwin_streaming.admin import check_broker
from autotwin_streaming.config import StreamingConfig
from autotwin_streaming.producer import TelemetryProducer

__all__ = [
    "BatchPolicy",
    "DatabaseTelemetrySink",
    "KafkaTelemetrySink",
    "NullTelemetrySink",
    "SinkStats",
    "TelemetryBatchWriter",
    "TelemetrySink",
    "apply_trip_event",
    "create_sink",
    "write_telemetry_batch",
]

_LOGGER = get_logger(__name__)

# ---------------------------------------------------------------------------------------------
# Batching policy — pure, and therefore testable without a clock or a database
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BatchPolicy:
    """When a buffer of telemetry rows should be written out.

    Kept as a value object with a pure predicate so that the rule "500 rows or one second"
    (BUILD_SPEC §8) can be unit-tested by passing numbers, with no broker, no database and — the
    part that matters for determinism — no clock inside the function being tested.
    """

    max_rows: int
    """Row count that triggers a flush regardless of age."""

    max_interval_s: float
    """Age in seconds at which a partial batch is flushed anyway."""

    def is_due(self, *, pending: int, age_s: float) -> bool:
        """Whether a buffer holding ``pending`` rows for ``age_s`` seconds must be flushed."""
        if pending <= 0:
            return False
        return pending >= self.max_rows or age_s >= self.max_interval_s

    @classmethod
    def from_config(cls, config: StreamingConfig) -> BatchPolicy:
        """Build the policy from the transport configuration."""
        return cls(max_rows=config.batch_size, max_interval_s=config.batch_interval_s)


# ---------------------------------------------------------------------------------------------
# The one description of how a telemetry row reaches PostGIS
# ---------------------------------------------------------------------------------------------


def telemetry_row(event: TelemetryEvent) -> dict[str, Any]:
    """Project a validated event onto the columns of the ``telemetry`` table.

    ``vehicle_id`` and ``trip_id`` stay the *human* identifiers rather than being resolved to
    foreign keys: they are the Kafka message keys, and forcing a UUID lookup per row would turn
    one bulk INSERT into 500 SELECTs (BUILD_SPEC §3.2 makes the same point from the schema side).
    """
    return {
        "vehicle_id": event.vehicle_id,
        "trip_id": event.trip_id,
        "route_id": event.route_id,
        "recorded_at": event.recorded_at,
        "location": to_shape_point(event.latitude, event.longitude),
        "speed_kmh": event.speed_kmh,
        "acceleration_ms2": event.acceleration_ms2,
        "heading_deg": event.heading_deg,
        "battery_soc_percent": event.battery_soc_percent,
        "battery_temperature_c": event.battery_temperature_c,
        "outside_temperature_c": event.outside_temperature_c,
        "instantaneous_power_kw": event.instantaneous_power_kw,
        "energy_consumption_kwh_100km": event.energy_consumption_kwh_100km,
        "cumulative_energy_kwh": event.cumulative_energy_kwh,
        "estimated_range_km": event.estimated_range_km,
        "road_class": event.road_class,
        "speed_limit_kmh": event.speed_limit_kmh,
        "odometer_m": event.odometer_m,
        "state": event.state,
        "data_origin": DataOrigin.simulated,
    }


def latest_per_key(
    events: Sequence[TelemetryEvent],
    key: Callable[[TelemetryEvent], str | None],
) -> dict[str, TelemetryEvent]:
    """Reduce a batch to the newest event per ``vehicle_id`` or per ``trip_id``.

    Pure and order-independent: it compares ``recorded_at`` rather than trusting the position in
    the batch, because a partition rebalance or a replay can hand the consumer an older message
    after a newer one, and "last one wins" would then walk a vehicle's state backwards.

    Events whose key is ``None`` — telemetry from an idle vehicle on no trip — are skipped
    rather than grouped under a sentinel, because there is no row for them to update.
    """
    newest: dict[str, TelemetryEvent] = {}
    for event in events:
        value = key(event)
        if value is None:
            continue
        current = newest.get(value)
        if current is None or event.recorded_at > current.recorded_at:
            newest[value] = event
    return newest


_UPDATE_VEHICLE_STATE = sa.text(
    """
    UPDATE vehicles AS v
       SET state = incoming.state::vehicle_state,
           updated_at = now()
      FROM unnest(CAST(:vehicle_ids AS text[]), CAST(:states AS text[]))
           AS incoming(vehicle_id, state)
     WHERE v.vehicle_id = incoming.vehicle_id
       AND v.state IS DISTINCT FROM incoming.state::vehicle_state
    """
)
"""One statement for the whole batch, joining against an unnested pair of arrays.

The alternative — a parameterised UPDATE per vehicle — is forty statements per tick, and a
``VALUES`` list would have to be rebuilt (and re-planned) for every distinct batch size.
``unnest`` keeps the SQL text constant, so PostgreSQL plans it once and psycopg adapts the two
Python lists straight to ``text[]``. The ``IS DISTINCT FROM`` guard means an idle fleet costs
zero row writes and zero dead tuples for the autovacuumer to clean up.
"""

_UPDATE_TRIP_AGGREGATES = sa.text(
    """
    UPDATE trips AS t
       SET distance_m = incoming.distance_m,
           energy_kwh = incoming.energy_kwh,
           avg_consumption_kwh_100km =
               CASE WHEN incoming.distance_m > 0
                    THEN incoming.energy_kwh / (incoming.distance_m / 100000.0)
                    ELSE NULL END,
           state = incoming.state::vehicle_state,
           updated_at = now()
      FROM unnest(
               CAST(:trip_ids AS text[]),
               CAST(:distances AS double precision[]),
               CAST(:energies AS double precision[]),
               CAST(:states AS text[])
           ) AS incoming(trip_id, distance_m, energy_kwh, state)
     WHERE t.trip_id = incoming.trip_id
       AND t.ended_at IS NULL
       AND incoming.distance_m >= t.distance_m
    """
)
"""Trip aggregates, maintained from the newest sample rather than by accumulation.

Accumulating (``distance_m = distance_m + delta``) would be wrong here, because delivery is
at-least-once: a redelivered batch would add the same kilometres twice. The telemetry payload
already carries *absolute* trip totals — ``odometer_m`` and ``cumulative_energy_kwh`` — so
assigning them is naturally idempotent, and ``incoming.distance_m >= t.distance_m`` makes a
replay of older samples a no-op instead of a regression. ``ended_at IS NULL`` keeps a late
straggler from re-opening a trip that a ``finished`` event has already closed.
"""


async def write_telemetry_batch(
    session: AsyncSession,
    events: Sequence[TelemetryEvent],
) -> int:
    """Persist a batch of validated telemetry and refresh the aggregates it implies.

    Three statements, whatever the batch size: one bulk INSERT into ``telemetry``, one UPDATE of
    ``vehicles.state`` and one UPDATE of the ``trips`` aggregates. Called inside the caller's
    transaction so that "rows are durable" and "offsets may be committed" are the same event
    (BUILD_SPEC §8).

    Returns the number of telemetry rows inserted.
    """
    if not events:
        return 0

    await session.execute(sa.insert(Telemetry), [telemetry_row(event) for event in events])
    await _update_vehicle_states(session, events)
    await _update_trip_aggregates(session, events)
    return len(events)


async def _update_vehicle_states(session: AsyncSession, events: Sequence[TelemetryEvent]) -> None:
    """Set ``vehicles.state`` from the newest sample of each vehicle in the batch.

    Vehicles the simulator has not registered simply match no row; that is deliberate rather
    than tolerated, because ``telemetry.vehicle_id`` is free text by design and the consumer
    must not be the component that refuses data for a vehicle it has never heard of.
    """
    newest = latest_per_key(events, lambda event: event.vehicle_id)
    if not newest:
        return
    await session.execute(
        _UPDATE_VEHICLE_STATE,
        {
            "vehicle_ids": list(newest.keys()),
            "states": [event.state.value for event in newest.values()],
        },
    )


async def _update_trip_aggregates(session: AsyncSession, events: Sequence[TelemetryEvent]) -> None:
    """Refresh ``trips.distance_m``/``energy_kwh``/``avg_consumption`` from the newest sample."""
    newest = latest_per_key(events, lambda event: event.trip_id)
    if not newest:
        return
    await session.execute(
        _UPDATE_TRIP_AGGREGATES,
        {
            "trip_ids": list(newest.keys()),
            "distances": [event.odometer_m for event in newest.values()],
            "energies": [event.cumulative_energy_kwh for event in newest.values()],
            "states": [event.state.value for event in newest.values()],
        },
    )


_TRIP_EVENT_STATES: Final[dict[TripEventType, tuple[VehicleState, VehicleState]]] = {
    TripEventType.started: (VehicleState.driving, VehicleState.driving),
    TripEventType.finished: (VehicleState.completed, VehicleState.idle),
    TripEventType.charging_started: (VehicleState.charging, VehicleState.charging),
    TripEventType.charging_finished: (VehicleState.driving, VehicleState.driving),
    TripEventType.paused: (VehicleState.stopped, VehicleState.stopped),
}
"""``(trip state, vehicle state)`` implied by each lifecycle transition.

The two differ in exactly one place, and that place is the reason this is a table rather than
one expression: a *trip* that finished is ``completed`` forever, while the *vehicle* that drove
it becomes ``idle`` and available for the next one.
"""


async def apply_trip_event(session: AsyncSession, event: TripEvent) -> None:
    """Apply one trip lifecycle transition to ``trips`` and ``vehicles``.

    Deliberately does **not** create a ``trips`` row on ``started``: the row needs
    ``vehicles.id`` and the provenance block, both of which the simulator holds when it opens
    the trip. Inventing a row here would mean guessing a foreign key, and a consumer that
    guesses is a consumer that writes wrong data on a replay.

    Closing a trip is idempotent for the same reason the aggregates are: ``ended_at IS NULL``
    means a redelivered ``finished`` event finds nothing left to close.

    Enum casts are spelled ``CAST(:param AS vehicle_state)`` rather than the shorter
    ``:param::vehicle_state``. SQLAlchemy's ``text()`` scans for ``:name`` bind parameters and
    stops at the first non-identifier character, so ``:trip_state::vehicle_state`` leaves the
    whole token in the SQL and PostgreSQL rejects it at ``:``. The failure is silent until the
    statement runs, which is why the verbose spelling is the correct one here.
    """
    trip_state, vehicle_state = _TRIP_EVENT_STATES[event.event_type]

    if event.event_type is TripEventType.finished:
        await session.execute(
            sa.text(
                """
                UPDATE trips
                   SET ended_at = :occurred_at,
                       end_soc_percent = COALESCE(:soc_percent, end_soc_percent),
                       distance_m = COALESCE(:distance_m, distance_m),
                       energy_kwh = COALESCE(:energy_kwh, energy_kwh),
                       avg_consumption_kwh_100km =
                           CASE WHEN COALESCE(:distance_m, distance_m) > 0
                                THEN COALESCE(:energy_kwh, energy_kwh)
                                     / (COALESCE(:distance_m, distance_m) / 100000.0)
                                ELSE avg_consumption_kwh_100km END,
                       state = CAST(:trip_state AS vehicle_state),
                       updated_at = now()
                 WHERE trip_id = :trip_id
                   AND ended_at IS NULL
                """
            ),
            {
                "occurred_at": event.occurred_at,
                "soc_percent": event.soc_percent,
                "distance_m": event.distance_m,
                "energy_kwh": event.energy_kwh,
                "trip_state": trip_state.value,
                "trip_id": event.trip_id,
            },
        )
    else:
        await session.execute(
            sa.text(
                """
                UPDATE trips
                   SET state = CAST(:trip_state AS vehicle_state),
                       updated_at = now()
                 WHERE trip_id = :trip_id
                   AND ended_at IS NULL
                   AND state IS DISTINCT FROM CAST(:trip_state AS vehicle_state)
                """
            ),
            {"trip_state": trip_state.value, "trip_id": event.trip_id},
        )

    await session.execute(
        sa.text(
            """
            UPDATE vehicles
               SET state = CAST(:vehicle_state AS vehicle_state),
                   updated_at = now()
             WHERE vehicle_id = :vehicle_id
               AND state IS DISTINCT FROM CAST(:vehicle_state AS vehicle_state)
            """
        ),
        {"vehicle_state": vehicle_state.value, "vehicle_id": event.vehicle_id},
    )


class TelemetryBatchWriter:
    """Buffers validated telemetry and writes it out when :class:`BatchPolicy` says so.

    Shared by :class:`DatabaseTelemetrySink` and the Kafka consumer so that "batches exactly
    like the consumer does" is a fact about the code rather than a promise in a docstring. The
    writer owns the buffer and the transaction; *when* to ask it to flush is the caller's
    decision, because the consumer knows about Kafka offsets and the sink does not.
    """

    def __init__(self, policy: BatchPolicy) -> None:
        """Start with an empty buffer whose age clock begins now."""
        self._policy = policy
        self._buffer: list[TelemetryEvent] = []
        self._opened_at = time.monotonic()

    @property
    def pending(self) -> int:
        """Rows waiting to be written."""
        return len(self._buffer)

    @property
    def policy(self) -> BatchPolicy:
        """The flush rule this writer applies."""
        return self._policy

    def add(self, event: TelemetryEvent) -> None:
        """Buffer one event; the first event of a batch restarts the age clock."""
        if not self._buffer:
            self._opened_at = time.monotonic()
        self._buffer.append(event)

    def add_many(self, events: Sequence[TelemetryEvent]) -> None:
        """Buffer several events at once."""
        for event in events:
            self.add(event)

    @property
    def age_s(self) -> float:
        """Seconds since the oldest buffered event was added. Zero when the buffer is empty."""
        if not self._buffer:
            return 0.0
        return time.monotonic() - self._opened_at

    def is_due(self) -> bool:
        """Whether the policy wants this buffer written out now."""
        return self._policy.is_due(pending=self.pending, age_s=self.age_s)

    def take(self) -> list[TelemetryEvent]:
        """Detach and return the buffered events, leaving an empty buffer behind."""
        batch = self._buffer
        self._buffer = []
        self._opened_at = time.monotonic()
        return batch

    async def flush(self) -> int:
        """Write the buffer in one transaction and return the number of rows inserted.

        The buffer is detached *before* the write so that a failure cannot leave the same rows
        queued for a second attempt in a buffer the caller keeps appending to; the exception
        propagates and the caller decides whether the batch is retried (the consumer, which has
        not committed its offsets) or dropped (the direct-to-database sink, whose source is an
        in-process simulator tick that no longer exists).
        """
        batch = self.take()
        if not batch:
            return 0
        async with session_scope() as session:
            return await write_telemetry_batch(session, batch)


# ---------------------------------------------------------------------------------------------
# The sink interface the simulator depends on
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SinkStats:
    """What a sink has done so far — surfaced by ``GET /api/v1/simulations/status``."""

    emitted: int
    """Events accepted and successfully handed on (produced, or written as rows)."""

    failed: int
    """Events that could not be handed on."""

    batches: int
    """Flush cycles completed."""

    transport: str
    """``kafka``, ``database`` or ``null`` — which implementation answered."""

    degraded_reason: str | None = None
    """Why Kafka was not used, when the operator asked for it. ``None`` on the happy path."""

    def as_dict(self) -> dict[str, Any]:
        """Flat mapping for the simulation-status payload."""
        return {
            "emitted": self.emitted,
            "failed": self.failed,
            "batches": self.batches,
            "transport": self.transport,
            "degraded_reason": self.degraded_reason,
        }


class TelemetrySink(ABC):
    """Where the simulator sends what it produces.

    The whole point of the abstraction is that the simulator cannot tell which implementation it
    holds. Every method is ``async`` even where an implementation does no I/O, because a sink
    that was synchronous for the null case and asynchronous for the real ones would push a
    conditional ``await`` into the caller.
    """

    transport: str = "abstract"
    """Short name of the transport, reported in :attr:`stats`."""

    @abstractmethod
    async def emit(self, event: TelemetryEvent) -> None:
        """Accept one telemetry sample."""

    @abstractmethod
    async def emit_many(self, events: Sequence[TelemetryEvent]) -> None:
        """Accept a tick's worth of samples, then make them durable."""

    @abstractmethod
    async def emit_trip_event(self, event: TripEvent) -> None:
        """Accept one trip lifecycle transition."""

    @abstractmethod
    async def flush(self) -> None:
        """Make everything accepted so far durable (or delivered)."""

    @abstractmethod
    async def aclose(self) -> None:
        """Flush and release the underlying resources. Idempotent."""

    @property
    @abstractmethod
    def stats(self) -> SinkStats:
        """Counters for the simulation-status endpoint."""

    async def astart(self) -> None:
        """Open the sink's resources. The default does nothing.

        Not abstract: two of the three implementations have nothing to open, and forcing an
        empty override on them would be ceremony rather than design.
        """
        return None

    async def __aenter__(self) -> Self:
        """Start the sink for use in an ``async with`` block."""
        await self.astart()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always close the sink, even when the block raised."""
        await self.aclose()


class KafkaTelemetrySink(TelemetrySink):
    """Publishes to Redpanda through :class:`~autotwin_streaming.producer.TelemetryProducer`."""

    transport = "kafka"

    def __init__(
        self,
        producer: TelemetryProducer | None = None,
        *,
        config: StreamingConfig | None = None,
    ) -> None:
        """Wrap an existing producer, or build one from ``config``."""
        resolved = config if config is not None else StreamingConfig.from_settings()
        self._producer = producer if producer is not None else TelemetryProducer(resolved)
        self._closed = False

    @property
    def producer(self) -> TelemetryProducer:
        """The underlying producer — exposed so ``/metrics`` can read its own counters."""
        return self._producer

    async def astart(self) -> None:
        """Connect the producer to the broker."""
        await self._producer.start()

    async def emit(self, event: TelemetryEvent) -> None:
        """Enqueue one telemetry sample on ``vehicle.telemetry.v1``."""
        await self._producer.send_telemetry(event)

    async def emit_many(self, events: Sequence[TelemetryEvent]) -> None:
        """Enqueue a batch and wait for the broker to acknowledge all of it."""
        await self._producer.send_telemetry_many(events)

    async def emit_trip_event(self, event: TripEvent) -> None:
        """Enqueue one trip transition on ``vehicle.trip-events.v1``."""
        await self._producer.send_trip_event(event)

    async def flush(self) -> None:
        """Resolve every outstanding send."""
        await self._producer.flush()

    async def aclose(self) -> None:
        """Flush and disconnect. Idempotent."""
        if self._closed:
            return
        self._closed = True
        await self._producer.stop()

    @property
    def stats(self) -> SinkStats:
        """Producer counters, projected onto the common sink shape."""
        producer_stats = self._producer.stats
        return SinkStats(
            emitted=producer_stats.messages_sent,
            failed=producer_stats.messages_failed,
            batches=producer_stats.batches,
            transport=self.transport,
        )


class DatabaseTelemetrySink(TelemetrySink):
    """Writes straight to PostGIS, batching exactly the way the consumer does.

    This is what ``make demo`` runs when no broker is available. It is not a lesser mode: the
    rows, the aggregates and the batching rule are the same, and the only thing missing is the
    log in the middle — which means no replay, no second consumer, and no buffer between a
    simulator burst and the database.
    """

    transport = "database"

    def __init__(
        self,
        *,
        config: StreamingConfig | None = None,
        degraded_reason: str | None = None,
    ) -> None:
        """Build the sink.

        ``degraded_reason`` records why Kafka was not used, when the operator did ask for it.
        """
        resolved = config if config is not None else StreamingConfig.from_settings()
        self._writer = TelemetryBatchWriter(BatchPolicy.from_config(resolved))
        self._degraded_reason = degraded_reason
        self._emitted = 0
        self._failed = 0
        self._batches = 0
        self._closed = False

    async def astart(self) -> None:
        """Create the database engine up front so a misconfigured DSN fails at startup.

        Without this the first engine build happens inside :meth:`emit`, where a wrong DSN
        surfaces as a failed flush of samples already accepted from the caller.
        """
        get_engine()

    async def emit(self, event: TelemetryEvent) -> None:
        """Buffer one sample, flushing if the batch policy says it is due."""
        self._writer.add(event)
        if self._writer.is_due():
            await self._flush_buffer()

    async def emit_many(self, events: Sequence[TelemetryEvent]) -> None:
        """Buffer a tick's worth of samples and flush if that filled or aged the batch."""
        self._writer.add_many(events)
        if self._writer.is_due():
            await self._flush_buffer()

    async def emit_trip_event(self, event: TripEvent) -> None:
        """Apply a lifecycle transition immediately, after flushing the telemetry ahead of it.

        Ordering matters: a ``finished`` event closes the trip, and the aggregate update in
        :func:`write_telemetry_batch` skips trips with an ``ended_at``. Flushing first means the
        last few samples of a trip are still counted.
        """
        await self._flush_buffer()
        async with session_scope() as session:
            await apply_trip_event(session, event)

    async def flush(self) -> None:
        """Write whatever is buffered."""
        await self._flush_buffer()

    async def aclose(self) -> None:
        """Flush the final partial batch and stop accepting work. Idempotent."""
        if self._closed:
            return
        self._closed = True
        await self._flush_buffer()

    async def _flush_buffer(self) -> int:
        """Write the buffer, counting the outcome instead of propagating a database failure.

        A simulator tick has no way to retry — the samples it produced are gone once the tick
        returns — so raising here would only turn a recoverable gap in a chart into a crashed
        simulation. The loss is visible in ``failed`` and in an ``error`` log line.
        """
        pending = self._writer.pending
        if pending == 0:
            return 0
        try:
            written = await self._writer.flush()
        except sa.exc.SQLAlchemyError as exc:
            self._failed += pending
            _LOGGER.error(
                "sink.database.write_failed",
                rows=pending,
                error=type(exc).__name__,
                detail=str(exc),
            )
            return 0
        self._emitted += written
        self._batches += 1
        return written

    @property
    def stats(self) -> SinkStats:
        """Rows written, rows lost, and why this sink was chosen."""
        return SinkStats(
            emitted=self._emitted,
            failed=self._failed,
            batches=self._batches,
            transport=self.transport,
            degraded_reason=self._degraded_reason,
        )


class NullTelemetrySink(TelemetrySink):
    """Counts events and discards them — for tests and for ``--dry-run`` style invocations.

    Keeps the counters so that a test can still assert "the simulator emitted 400 samples"
    without a broker or a database anywhere near it.
    """

    transport = "null"

    def __init__(self) -> None:
        """Start with empty counters."""
        self._emitted = 0
        self._trip_events = 0
        self._batches = 0

    @property
    def trip_events(self) -> int:
        """Trip transitions seen — the one counter the common :class:`SinkStats` has no slot for."""
        return self._trip_events

    async def emit(self, event: TelemetryEvent) -> None:
        """Count one sample."""
        self._emitted += 1

    async def emit_many(self, events: Sequence[TelemetryEvent]) -> None:
        """Count a batch of samples."""
        self._emitted += len(events)
        self._batches += 1

    async def emit_trip_event(self, event: TripEvent) -> None:
        """Count one trip transition."""
        self._trip_events += 1

    async def flush(self) -> None:
        """Nothing is buffered, so there is nothing to flush."""
        return None

    async def aclose(self) -> None:
        """Nothing is held, so there is nothing to release."""
        return None

    @property
    def stats(self) -> SinkStats:
        """Counters of everything that was thrown away."""
        return SinkStats(
            emitted=self._emitted,
            failed=0,
            batches=self._batches,
            transport=self.transport,
        )


async def create_sink(
    settings: Settings | None = None,
    *,
    config: StreamingConfig | None = None,
) -> TelemetrySink:
    """Choose the transport for this process and return a started sink.

    Kafka wins when ``AUTOTWIN_KAFKA_ENABLED`` is true *and* the broker answers within
    :attr:`~autotwin_streaming.config.StreamingConfig.broker_probe_timeout_s`. Anything else
    falls back to :class:`DatabaseTelemetrySink` — and says so at ``warning`` level with the
    reason attached, because BUILD_SPEC §0.3 asks the system to degrade rather than crash, and
    §0.2 asks it never to do so invisibly.

    The broker is probed *before* the producer is built rather than by catching a failure from
    ``producer.start()``: the probe has a three-second budget, while a producer's own bootstrap
    retries for its full request timeout, and a ``make demo`` that stalls twenty seconds on a
    broker nobody started is the exact experience this fallback exists to prevent.
    """
    resolved_settings = settings if settings is not None else get_settings()
    resolved = config if config is not None else StreamingConfig.from_settings(resolved_settings)

    if not resolved.enabled:
        _LOGGER.info(
            "sink.selected",
            transport=DatabaseTelemetrySink.transport,
            reason="kafka_disabled_by_configuration",
        )
        sink: TelemetrySink = DatabaseTelemetrySink(config=resolved)
        await sink.astart()
        return sink

    if not await check_broker(config=resolved):
        reason = f"broker at {resolved.bootstrap_servers} did not answer"
        _LOGGER.warning(
            "sink.kafka_unavailable",
            bootstrap_servers=resolved.bootstrap_servers,
            fallback=DatabaseTelemetrySink.transport,
            reason=reason,
            detail=(
                "AUTOTWIN_KAFKA_ENABLED is true but the broker is unreachable; "
                "telemetry will be written directly to PostGIS instead"
            ),
        )
        fallback = DatabaseTelemetrySink(config=resolved, degraded_reason=reason)
        await fallback.astart()
        return fallback

    kafka_sink = KafkaTelemetrySink(config=resolved)
    try:
        await kafka_sink.astart()
    except ProviderError as exc:
        # The broker answered the probe and then refused the producer — rare, but a rebalancing
        # or just-starting cluster does exactly this. Same fallback, different reason.
        reason = f"producer could not start: {exc.message}"
        _LOGGER.warning(
            "sink.kafka_unavailable",
            bootstrap_servers=resolved.bootstrap_servers,
            fallback=DatabaseTelemetrySink.transport,
            reason=reason,
        )
        fallback = DatabaseTelemetrySink(config=resolved, degraded_reason=reason)
        await fallback.astart()
        return fallback

    _LOGGER.info(
        "sink.selected",
        transport=KafkaTelemetrySink.transport,
        bootstrap_servers=resolved.bootstrap_servers,
    )
    return kafka_sink
