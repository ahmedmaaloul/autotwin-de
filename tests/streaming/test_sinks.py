"""The telemetry sinks: counting, batching, key reduction and the loud Kafka fallback.

Three properties are worth more than the rest and shape this module:

1. **The fallback is observable.** ``create_sink`` must never quietly stop using a broker the
   operator started. The assertions are therefore on the *log record*, captured with structlog's
   own testing helper rather than on a side effect that happens to be visible.
2. **The batch policy is pure.** "500 rows or one second" is a predicate over two numbers, so it
   is tested by passing numbers — no clock inside the function under test, no database, and a
   boundary case on each side of both thresholds.
3. **``latest_per_key`` is order-independent.** At-least-once delivery and partition rebalances
   hand a consumer an older message after a newer one, and "last one wins" would then walk a
   vehicle's state backwards.

Nothing here needs PostgreSQL or a broker: the sinks' database path is reached only by
``flush()``, which these tests never call on a real buffer. The one case that genuinely needs
Redpanda is marked ``@pytest.mark.kafka``.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from geoalchemy2.shape import to_shape
from shapely import Point
from structlog.testing import capture_logs

from autotwin_contracts import (
    DataOrigin,
    RoadClass,
    TelemetryEvent,
    TripEvent,
    TripEventType,
    VehicleState,
)
from autotwin_core.config import get_settings
from autotwin_core.errors import ProviderError
from autotwin_streaming import sinks as sinks_module
from autotwin_streaming.config import (
    DEFAULT_BATCH_INTERVAL_S,
    DEFAULT_BATCH_SIZE,
    StreamingConfig,
)
from autotwin_streaming.sinks import (
    BatchPolicy,
    DatabaseTelemetrySink,
    KafkaTelemetrySink,
    NullTelemetrySink,
    SinkStats,
    TelemetryBatchWriter,
    create_sink,
    latest_per_key,
    telemetry_row,
)

RECORDED_AT = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)


def make_event(
    *,
    vehicle_id: str = "ATW-0001",
    trip_id: str | None = "ATW-0001-trip-0001",
    seconds: float = 0.0,
    state: VehicleState = VehicleState.driving,
    odometer_m: float = 1_000.0,
) -> TelemetryEvent:
    """A valid telemetry sample; only the fields a test varies are parameters."""
    return TelemetryEvent(
        vehicle_id=vehicle_id,
        trip_id=trip_id,
        recorded_at=RECORDED_AT + timedelta(seconds=seconds),
        latitude=50.0,
        longitude=8.6,
        speed_kmh=120.0,
        acceleration_ms2=0.1,
        heading_deg=180.0,
        battery_soc_percent=72.0,
        battery_temperature_c=24.0,
        outside_temperature_c=11.0,
        instantaneous_power_kw=28.0,
        energy_consumption_kwh_100km=18.0,
        cumulative_energy_kwh=5.0,
        estimated_range_km=280.0,
        road_class=RoadClass.motorway,
        speed_limit_kmh=130.0,
        odometer_m=odometer_m,
        state=state,
    )


def make_trip_event(event_type: TripEventType = TripEventType.started) -> TripEvent:
    """One lifecycle transition."""
    return TripEvent(
        trip_id="ATW-0001-trip-0001",
        vehicle_id="ATW-0001",
        event_type=event_type,
        occurred_at=RECORDED_AT,
        latitude=50.0,
        longitude=8.6,
        soc_percent=72.0,
        distance_m=1_000.0,
        energy_kwh=5.0,
    )


@pytest.fixture
def streaming_config() -> StreamingConfig:
    """The transport configuration the suite runs with: Kafka switched off (see conftest)."""
    return StreamingConfig.from_settings(get_settings())


class TestNullTelemetrySink:
    """It throws everything away, but it counts what it threw away."""

    async def test_counts_single_emissions(self) -> None:
        sink = NullTelemetrySink()
        await sink.emit(make_event())
        await sink.emit(make_event(seconds=1.0))

        assert sink.stats == SinkStats(emitted=2, failed=0, batches=0, transport="null")

    async def test_counts_a_batch_as_one_flush_cycle(self) -> None:
        """``emit_many`` is one tick's worth of samples, which is one batch by definition."""
        sink = NullTelemetrySink()
        await sink.emit_many([make_event(seconds=index) for index in range(40)])

        assert sink.stats.emitted == 40
        assert sink.stats.batches == 1

    async def test_counts_trip_events_separately(self) -> None:
        """``SinkStats`` has no slot for them, so the null sink exposes its own counter.

        A test asserting "the simulator emitted 400 samples and 12 trip transitions" needs both,
        and a trip event counted as telemetry would inflate the throughput figure.
        """
        sink = NullTelemetrySink()
        await sink.emit_trip_event(make_trip_event())
        await sink.emit_trip_event(make_trip_event(TripEventType.finished))

        assert sink.trip_events == 2
        assert sink.stats.emitted == 0

    async def test_an_empty_batch_is_still_a_batch_and_emits_nothing(self) -> None:
        """The degenerate input: a tick in which every vehicle failed."""
        sink = NullTelemetrySink()
        await sink.emit_many([])

        assert sink.stats.emitted == 0
        assert sink.stats.batches == 1

    async def test_flush_and_close_are_no_ops_and_repeatable(self) -> None:
        sink = NullTelemetrySink()
        await sink.emit(make_event())
        await sink.flush()
        await sink.aclose()
        await sink.aclose()

        assert sink.stats.emitted == 1

    async def test_works_as_an_async_context_manager(self) -> None:
        """Every sink is startable and closable the same way, whatever it wraps."""
        async with NullTelemetrySink() as sink:
            await sink.emit(make_event())
        assert sink.stats.emitted == 1


class TestBatchPolicy:
    """The rule "500 rows or one second" is pure, so its boundaries can be pinned exactly."""

    @pytest.mark.parametrize(
        ("pending", "age_s", "expected"),
        [
            (0, 0.0, False),  # nothing buffered
            (0, 99.0, False),  # an empty buffer never flushes, however old the clock says it is
            (1, 0.0, False),  # one row, fresh: wait for more
            (499, 0.5, False),  # just under both thresholds
            (500, 0.0, True),  # exactly the row threshold
            (501, 0.0, True),  # past it
            (1, 1.0, True),  # exactly the time threshold
            (1, 0.999, False),  # just under it
            (499, 2.0, True),  # a partial batch that aged out
        ],
    )
    def test_is_due_at_the_documented_thresholds(
        self, pending: int, age_s: float, expected: bool
    ) -> None:
        """Both thresholds and both sides of each; ``pending <= 0`` short-circuits first."""
        policy = BatchPolicy(max_rows=500, max_interval_s=1.0)
        assert policy.is_due(pending=pending, age_s=age_s) is expected

    def test_from_config_takes_the_spec_defaults(self, streaming_config: StreamingConfig) -> None:
        """BUILD_SPEC §8 fixes 500 rows / 1 s; the transport config is where they live."""
        policy = BatchPolicy.from_config(streaming_config)
        assert policy.max_rows == DEFAULT_BATCH_SIZE == 500
        assert policy.max_interval_s == DEFAULT_BATCH_INTERVAL_S == 1.0

    def test_a_cli_override_reaches_the_policy(self, streaming_config: StreamingConfig) -> None:
        """``--batch-size`` on the consumer must actually change how it batches."""
        policy = BatchPolicy.from_config(streaming_config.with_overrides(batch_size=7))
        assert policy.max_rows == 7
        assert streaming_config.batch_size == DEFAULT_BATCH_SIZE  # the original is untouched


class TestTelemetryBatchWriter:
    """The buffer the direct sink and the Kafka consumer share."""

    def test_flushes_at_the_row_threshold(self) -> None:
        """Three rows buffered, a policy of three: due. Two: not."""
        writer = TelemetryBatchWriter(BatchPolicy(max_rows=3, max_interval_s=3_600.0))
        writer.add_many([make_event(seconds=0.0), make_event(seconds=1.0)])
        assert writer.pending == 2
        assert writer.is_due() is False

        writer.add(make_event(seconds=2.0))
        assert writer.pending == 3
        assert writer.is_due() is True

    def test_flushes_at_the_time_threshold_with_a_partial_batch(self) -> None:
        """A single row that has waited longer than the interval must not sit there.

        The buffer's age clock is wound back rather than slept through: the property under test
        is the rule, and a test that really waited a second would be a second slower for nothing.
        """
        writer = TelemetryBatchWriter(BatchPolicy(max_rows=500, max_interval_s=1.0))
        writer.add(make_event())
        assert writer.is_due() is False

        writer._opened_at = time.monotonic() - 1.5
        assert writer.age_s >= 1.0
        assert writer.is_due() is True

    def test_an_empty_buffer_reports_no_age_and_is_never_due(self) -> None:
        """Otherwise an idle consumer would open an empty transaction every second."""
        writer = TelemetryBatchWriter(BatchPolicy(max_rows=500, max_interval_s=1.0))
        assert writer.pending == 0
        assert writer.age_s == 0.0
        assert writer.is_due() is False

    def test_the_age_clock_restarts_with_the_first_row_of_a_batch(self) -> None:
        """The age is that of the *oldest buffered row*, not of the writer."""
        writer = TelemetryBatchWriter(BatchPolicy(max_rows=500, max_interval_s=1.0))
        writer._opened_at = time.monotonic() - 10.0
        writer.add(make_event())

        assert writer.age_s < 1.0
        assert writer.is_due() is False

    def test_take_detaches_the_buffer(self) -> None:
        """``flush`` detaches before writing, so a failed write cannot be queued twice."""
        writer = TelemetryBatchWriter(BatchPolicy(max_rows=500, max_interval_s=1.0))
        writer.add_many([make_event(seconds=index) for index in range(5)])

        batch = writer.take()

        assert len(batch) == 5
        assert writer.pending == 0
        assert writer.take() == []


class TestLatestPerKey:
    """Reduce a batch to the newest sample per vehicle or per trip."""

    def test_keeps_the_newest_sample_per_vehicle(self) -> None:
        events = [
            make_event(vehicle_id="ATW-0001", seconds=0.0),
            make_event(vehicle_id="ATW-0002", seconds=5.0),
            make_event(vehicle_id="ATW-0001", seconds=10.0),
        ]
        newest = latest_per_key(events, lambda event: event.vehicle_id)

        assert set(newest) == {"ATW-0001", "ATW-0002"}
        assert newest["ATW-0001"].recorded_at == RECORDED_AT + timedelta(seconds=10)

    def test_an_out_of_order_batch_does_not_walk_the_state_backwards(self) -> None:
        """Delivery is at-least-once: a replay can hand the consumer an older message last.

        "Last one wins" would then set ``vehicles.state`` from a stale sample and the live map
        would show a car that has already arrived still driving.
        """
        newest = latest_per_key(
            [
                make_event(seconds=30.0, state=VehicleState.completed),
                make_event(seconds=10.0, state=VehicleState.driving),
            ],
            lambda event: event.vehicle_id,
        )
        assert newest["ATW-0001"].state is VehicleState.completed

    def test_events_without_a_key_are_skipped_not_grouped_under_a_sentinel(self) -> None:
        """Telemetry from an idle vehicle carries no ``trip_id``, and there is no row to update."""
        newest = latest_per_key(
            [
                make_event(trip_id=None, seconds=0.0),
                make_event(trip_id="trip-a", seconds=1.0),
            ],
            lambda event: event.trip_id,
        )
        assert set(newest) == {"trip-a"}

    def test_an_empty_batch_reduces_to_an_empty_mapping(self) -> None:
        assert latest_per_key([], lambda event: event.vehicle_id) == {}

    def test_a_single_event_is_its_own_newest(self) -> None:
        event = make_event()
        assert latest_per_key([event], lambda item: item.vehicle_id) == {"ATW-0001": event}

    def test_ties_keep_the_first_seen(self) -> None:
        """Equal timestamps are not "newer", so the comparison is strict and the batch is stable."""
        first = make_event(seconds=0.0, odometer_m=1_000.0)
        second = make_event(seconds=0.0, odometer_m=2_000.0)
        newest = latest_per_key([first, second], lambda event: event.vehicle_id)
        assert newest["ATW-0001"].odometer_m == 1_000.0


class TestTelemetryRow:
    """The projection onto the ``telemetry`` table — one description, two transports."""

    def test_stamps_the_simulated_origin(self) -> None:
        """ADR 004: every database record carries ``data_origin``, and telemetry is simulated."""
        assert telemetry_row(make_event())["data_origin"] is DataOrigin.simulated

    def test_keeps_the_human_identifiers_rather_than_resolving_foreign_keys(self) -> None:
        """They are the Kafka message keys; a UUID lookup per row would be 500 SELECTs per batch."""
        row = telemetry_row(make_event())
        assert row["vehicle_id"] == "ATW-0001"
        assert row["trip_id"] == "ATW-0001-trip-0001"

    def test_carries_a_point_geometry_in_lon_lat_order(self) -> None:
        """The axis flip happens once, in ``to_shape_point``; a swap here would put the fleet
        in the Indian Ocean, which is what decoding the stored WKB back checks."""
        # `to_shape` is typed as returning the geometry base class; the column is a Point.
        point = cast(Point, to_shape(telemetry_row(make_event())["location"]))
        assert (point.x, point.y) == (8.6, 50.0)  # PostGIS is (x=longitude, y=latitude)


class TestCreateSink:
    """Choosing the transport, and saying so."""

    async def test_kafka_disabled_selects_the_database_sink_and_says_why(
        self, streaming_config: StreamingConfig
    ) -> None:
        """The choice is logged with its reason, so ``make demo`` is never mysteriously broker-less.

        It is logged at **info**, not warning: an operator who set ``AUTOTWIN_KAFKA_ENABLED=false``
        is not running degraded, they are running the documented Kafka-optional mode, and
        ``degraded_reason`` is therefore null. The warning is reserved for the case below, where
        Kafka *was* asked for and could not be had.
        """
        assert streaming_config.enabled is False

        with capture_logs() as logs:
            sink = await create_sink(config=streaming_config)

        assert isinstance(sink, DatabaseTelemetrySink)
        assert sink.stats.transport == "database"
        assert sink.stats.degraded_reason is None

        selected = [entry for entry in logs if entry["event"] == "sink.selected"]
        assert len(selected) == 1
        assert selected[0]["log_level"] == "info"
        assert selected[0]["transport"] == "database"
        assert selected[0]["reason"] == "kafka_disabled_by_configuration"

    async def test_an_unreachable_broker_falls_back_loudly(
        self, streaming_config: StreamingConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kafka was asked for and is not there: warning, reason, and a degraded sink.

        A silent fallback would violate BUILD_SPEC §0.2 exactly as much as mislabelled data does:
        "the demo works without a broker" must never become "the demo quietly stopped using the
        broker you started". The probe itself is stubbed out — asserting on the fallback must not
        require a socket.
        """

        async def broker_is_down(**_: object) -> bool:
            return False

        monkeypatch.setattr(sinks_module, "check_broker", broker_is_down)

        with capture_logs() as logs:
            sink = await create_sink(config=streaming_config.with_overrides(enabled=True))

        assert isinstance(sink, DatabaseTelemetrySink)
        warnings = [entry for entry in logs if entry["event"] == "sink.kafka_unavailable"]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["fallback"] == "database"
        assert streaming_config.bootstrap_servers in warnings[0]["reason"]
        # The operator can also see it on the status endpoint, not only in the log.
        assert sink.stats.degraded_reason == warnings[0]["reason"]

    async def test_a_broker_that_answers_then_refuses_the_producer_also_falls_back(
        self, streaming_config: StreamingConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rebalancing or just-starting cluster passes the probe and rejects the producer.

        Same fallback, different reason — and the reason has to name the producer, because
        "the broker did not answer" would send an operator to look at a broker that did.
        """

        async def broker_is_up(**_: object) -> bool:
            return True

        async def producer_refuses(self: KafkaTelemetrySink) -> None:
            raise ProviderError("coordinator not available", details={})

        monkeypatch.setattr(sinks_module, "check_broker", broker_is_up)
        monkeypatch.setattr(KafkaTelemetrySink, "astart", producer_refuses)

        with capture_logs() as logs:
            sink = await create_sink(config=streaming_config.with_overrides(enabled=True))

        assert isinstance(sink, DatabaseTelemetrySink)
        reason = sink.stats.degraded_reason or ""
        assert reason.startswith("producer could not start")
        assert any(entry["event"] == "sink.kafka_unavailable" for entry in logs)

    @pytest.mark.kafka
    async def test_a_reachable_broker_gives_a_kafka_sink(
        self, streaming_config: StreamingConfig
    ) -> None:
        """The happy path, which is the one case that genuinely needs Redpanda running."""
        sink = await create_sink(config=streaming_config.with_overrides(enabled=True))
        try:
            assert isinstance(sink, KafkaTelemetrySink)
            assert sink.stats.transport == "kafka"
            assert sink.stats.degraded_reason is None
        finally:
            await sink.aclose()


class TestDatabaseSinkBuffering:
    """The direct-to-PostGIS sink batches exactly the way the consumer does."""

    def test_uses_the_configured_batch_policy(self, streaming_config: StreamingConfig) -> None:
        """Same rule, same object: "batches like the consumer" is a fact about the code."""
        sink = DatabaseTelemetrySink(config=streaming_config.with_overrides(batch_size=9))
        assert sink._writer.policy.max_rows == 9

    async def test_buffers_below_the_threshold_without_touching_the_database(
        self, streaming_config: StreamingConfig
    ) -> None:
        """A tick's worth of samples under the row threshold must not open a transaction.

        The proof is that this test passes with no database: the first thing a flush does is open
        a session, so a sink that flushed here would fail to connect.
        """
        sink = DatabaseTelemetrySink(config=streaming_config.with_overrides(batch_size=500))
        await sink.emit_many([make_event(seconds=index) for index in range(10)])

        assert sink._writer.pending == 10
        assert sink.stats.emitted == 0
        assert sink.stats.batches == 0

    async def test_a_failed_write_is_counted_rather_than_raised(
        self, streaming_config: StreamingConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A simulator tick cannot retry — the samples are gone once the tick returns.

        So a database failure has to become a visible gap in a chart (``failed``) instead of a
        crashed simulation. The exception is swallowed *and counted*, never swallowed silently.
        """
        import sqlalchemy as sa

        sink = DatabaseTelemetrySink(config=streaming_config.with_overrides(batch_size=2))

        async def explode() -> int:
            raise sa.exc.OperationalError("INSERT", {}, Exception("connection refused"))

        monkeypatch.setattr(sink._writer, "flush", explode)
        await sink.emit_many([make_event(seconds=0.0), make_event(seconds=1.0)])

        assert sink.stats.failed == 2
        assert sink.stats.emitted == 0
        assert sink.stats.batches == 0

    async def test_closing_twice_is_safe(self, streaming_config: StreamingConfig) -> None:
        """``aclose`` is documented idempotent; the engine's shutdown path relies on it."""
        sink = DatabaseTelemetrySink(config=streaming_config)
        await sink.aclose()
        await sink.aclose()
        assert sink.stats.emitted == 0


class TestSinkStats:
    """The shape ``GET /api/v1/simulations/status`` renders."""

    def test_as_dict_names_every_field_the_status_endpoint_shows(self) -> None:
        payload = SinkStats(
            emitted=10,
            failed=1,
            batches=2,
            transport="database",
            degraded_reason="broker at localhost:19092 did not answer",
        ).as_dict()

        assert payload == {
            "emitted": 10,
            "failed": 1,
            "batches": 2,
            "transport": "database",
            "degraded_reason": "broker at localhost:19092 did not answer",
        }


class TestTopicContract:
    """Topic names and retention are the wire contract of BUILD_SPEC §8."""

    def test_retention_matches_the_specification_table(self) -> None:
        """Telemetry is kept 6 h; everything else 24 h. PostgreSQL is the system of record."""
        from autotwin_contracts import (
            TOPIC_CHARGING_EVENTS,
            TOPIC_TELEMETRY,
            TOPIC_TRAFFIC_EVENTS,
            TOPIC_TRIP_EVENTS,
        )
        from autotwin_streaming.config import TOPIC_RETENTION_MS

        assert TOPIC_RETENTION_MS[TOPIC_TELEMETRY] == 6 * 60 * 60 * 1000
        for topic in (TOPIC_TRIP_EVENTS, TOPIC_TRAFFIC_EVENTS, TOPIC_CHARGING_EVENTS):
            assert TOPIC_RETENTION_MS[topic] == 24 * 60 * 60 * 1000

    def test_component_client_ids_are_distinguishable_in_the_broker_s_own_logs(
        self, streaming_config: StreamingConfig
    ) -> None:
        roles: Sequence[str] = ("producer", "consumer", "admin")
        ids = {streaming_config.component_client_id(role) for role in roles}
        assert len(ids) == len(roles)
        assert all(client_id.startswith(streaming_config.client_id) for client_id in ids)
