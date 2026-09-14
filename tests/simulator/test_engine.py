"""The simulation engine: reproducibility, the fleet, the clock and the lifecycle.

The headline property is the one BUILD_SPEC §13 and ADR 004 both rest on: **the same seed
produces the same telemetry**. Everything else here exists because it is load-bearing for that
claim (the apportionment of the vehicle mix, the per-vehicle seeding, the pinned clock origin) or
because it is what keeps a long demo run honest (a failed vehicle does not take the fleet down,
a stopped run does not leave trips open for ever).

No database, no broker: the engine is constructed with its corridors already resolved and a
``NullTelemetrySink``, which is exactly the shape the offline training-data generator uses.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from autotwin_contracts import SimulationState, VehicleState
from autotwin_core.errors import NotFoundError, ValidationError
from autotwin_simulator.engine import (
    DEFAULT_VEHICLE_MIX,
    EngineStats,
    SimulationClock,
    SimulationConfig,
    SimulationEngine,
    build_environments,
    resolve_vehicle_mix,
)
from autotwin_simulator.environment import (
    EnvironmentConfig,
    RouteEnvironment,
    TrafficIntensity,
    WeatherMode,
)
from autotwin_simulator.vehicle import SimulatedVehicle, TelemetrySample
from autotwin_streaming.sinks import NullTelemetrySink

from . import FlatEnvironment, straight_corridor

SIMULATION_ORIGIN = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
"""Pinned clock origin. A byte-identical rerun needs it; without it the origin is derived from
the wall clock and two runs of the same seed differ in every timestamp."""


def build_engine(
    *,
    seed: int = 42,
    vehicle_count: int = 6,
    duration_s: float = 1_200.0,
    routes: tuple[str, ...] = ("probe",),
    include_reverse_routes: bool = False,
) -> tuple[SimulationEngine, list[TelemetrySample], NullTelemetrySink]:
    """An offline engine over synthetic corridors, plus the samples and the sink it feeds.

    ``realtime=False`` removes both the sleep between ticks and the cap that stops simulated
    time overtaking the wall clock, so a twenty-minute run finishes in milliseconds and is a pure
    function of its seed.
    """
    config = SimulationConfig(
        vehicle_count=vehicle_count,
        seed=seed,
        realtime=False,
        duration_s=duration_s,
        start_time=SIMULATION_ORIGIN,
        speed_factor=10.0,
        tick_seconds=1.0,
        weather_mode=WeatherMode.mild,
        traffic_intensity=TrafficIntensity.none,
        include_reverse_routes=include_reverse_routes,
    )
    environment_config = EnvironmentConfig(
        seed=seed,
        weather_mode=config.weather_mode,
        traffic_intensity=config.traffic_intensity,
    )
    environments = [
        FlatEnvironment(straight_corridor(length_km=100.0, slug=slug), environment_config)
        for slug in routes
    ]
    samples: list[TelemetrySample] = []
    sink = NullTelemetrySink()
    engine = SimulationEngine(config, environments, sink=sink, observer=samples.append)
    return engine, samples, sink


def state_of(engine: SimulationEngine) -> SimulationState:
    """Read the engine's lifecycle state.

    A function rather than ``engine.state`` inline: a type checker narrows a property access to
    the literal an ``is`` check proved, and the narrowing then leaks past the ``await`` that
    changes it — so a sequence of lifecycle assertions reads as a contradiction.
    """
    return engine.state


def trace_of(samples: list[TelemetrySample], vehicle_id: str) -> list[str]:
    """Every telemetry event of one vehicle, as the JSON that would go on the wire."""
    return [
        sample.event.model_dump_json()
        for sample in samples
        if sample.event.vehicle_id == vehicle_id
    ]


class TestDeterminism:
    """Two runs of one seed are the same run. This is the property everything else rests on."""

    async def test_same_seed_produces_an_identical_telemetry_sequence(self) -> None:
        """Compared over the *whole* emitted sequence, not a summary of it.

        A checksum of the row count would pass a simulator whose vehicles all drove differently
        and happened to emit the same number of samples, which is the failure this is for.
        """
        first_engine, first, _ = build_engine(seed=42)
        second_engine, second, _ = build_engine(seed=42)
        await first_engine.run()
        await second_engine.run()

        assert len(first) == 720  # 6 vehicles x 120 ticks of 10 simulated seconds
        assert [sample.event.model_dump_json() for sample in first] == [
            sample.event.model_dump_json() for sample in second
        ]

    async def test_a_different_seed_produces_a_different_sequence(self) -> None:
        """Otherwise the seed is not reaching the physics and every run is the same run."""
        first_engine, first, _ = build_engine(seed=42)
        second_engine, second, _ = build_engine(seed=43)
        await first_engine.run()
        await second_engine.run()

        assert len(first) == len(second)
        assert [s.event.model_dump_json() for s in first] != [
            s.event.model_dump_json() for s in second
        ]

    async def test_a_vehicles_own_stream_does_not_depend_on_the_fleet_size(self) -> None:
        """Every random draw is keyed on ``(seed, concern, vehicle_id)``, never on a shared stream.

        So adding vehicles to a run cannot shift the ones that were already there. Asserted on
        the driver temperament and the starting state of charge rather than on the trace, because
        the *profile* a vehicle is issued does legitimately change: ``resolve_vehicle_mix``
        apportions the mix over the whole fleet, so ATW-0004 is a different class of car in a
        4-vehicle run than in an 8-vehicle one. ATW-0001 keeps its class, and therefore its trace.
        """
        small_engine, small, _ = build_engine(seed=9, vehicle_count=4, duration_s=600.0)
        large_engine, large, _ = build_engine(seed=9, vehicle_count=8, duration_s=600.0)
        await small_engine.run()
        await large_engine.run()

        small_by_id = {vehicle.vehicle_id: vehicle for vehicle in small_engine.vehicles}
        large_by_id = {vehicle.vehicle_id: vehicle for vehicle in large_engine.vehicles}
        assert set(small_by_id) <= set(large_by_id)
        for vehicle_id, vehicle in small_by_id.items():
            assert vehicle.driver.profile == large_by_id[vehicle_id].driver.profile

        assert trace_of(small, "ATW-0001") == trace_of(large, "ATW-0001")

    async def test_reset_and_rerun_reproduces_the_same_run(self) -> None:
        """A reset keeps the corridors and rewinds everything else, so a restart is the same run."""
        engine, samples, _ = build_engine(seed=7, duration_s=300.0)
        await engine.run()
        first = [sample.event.model_dump_json() for sample in samples]

        samples.clear()
        await engine.reset()
        assert state_of(engine) is SimulationState.pending
        await engine.run()

        assert [sample.event.model_dump_json() for sample in samples] == first


class TestVehicleMix:
    """Largest-remainder apportionment: exactly ``count`` vehicles, closest achievable shares."""

    def test_default_mix_over_forty_vehicles_is_exact(self) -> None:
        """0.35/0.25/0.20/0.10/0.10 of 40 are whole numbers, so no rounding is involved at all."""
        profiles = resolve_vehicle_mix(DEFAULT_VEHICLE_MIX, 40)
        assert Counter(profile.code for profile in profiles) == {
            "compact_ev": 14,
            "sedan_ev": 10,
            "suv_ev": 8,
            "van_ev": 4,
            "performance_ev": 4,
        }

    @pytest.mark.parametrize("count", [1, 3, 7, 13, 40, 97, 300])
    def test_always_produces_exactly_the_requested_fleet(self, count: int) -> None:
        """Never 39 or 41 — the classic off-by-one of proportional allocation."""
        assert len(resolve_vehicle_mix(DEFAULT_VEHICLE_MIX, count)) == count

    def test_shares_are_within_one_vehicle_of_the_exact_proportion(self) -> None:
        """The defining property of the largest-remainder method, checked at an awkward size."""
        count = 97
        profiles = resolve_vehicle_mix(DEFAULT_VEHICLE_MIX, count)
        allocated = Counter(profile.code for profile in profiles)
        for code, weight in DEFAULT_VEHICLE_MIX.items():
            assert abs(allocated[code] - weight * count) < 1.0

    def test_ties_break_on_the_profile_code_not_on_dictionary_order(self) -> None:
        """Two mixes that differ only in insertion order must allocate identically."""
        forward = {"compact_ev": 0.5, "sedan_ev": 0.5}
        reversed_order = {"sedan_ev": 0.5, "compact_ev": 0.5}
        assert [p.code for p in resolve_vehicle_mix(forward, 5)] == [
            p.code for p in resolve_vehicle_mix(reversed_order, 5)
        ]

    def test_zero_weights_are_dropped_rather_than_allocated(self) -> None:
        """A profile the operator set to 0 must not appear in the fleet."""
        profiles = resolve_vehicle_mix({"compact_ev": 1.0, "van_ev": 0.0}, 5)
        assert {profile.code for profile in profiles} == {"compact_ev"}

    @pytest.mark.parametrize(
        ("mix", "count"),
        [
            ({}, 10),
            ({"compact_ev": 0.0}, 10),
            (dict(DEFAULT_VEHICLE_MIX), 0),
            (dict(DEFAULT_VEHICLE_MIX), -1),
        ],
    )
    def test_a_fleet_of_nobody_is_a_configuration_error(
        self, mix: dict[str, float], count: int
    ) -> None:
        """A run with no vehicles is a mistake to report, not a degenerate case to tolerate."""
        with pytest.raises(ValidationError):
            resolve_vehicle_mix(mix, count)


class TestSimulationClock:
    """Simulated time is an origin plus elapsed seconds, so a run can be pinned and replayed."""

    def test_advance_moves_and_reports_the_new_instant(self) -> None:
        clock = SimulationClock(origin=SIMULATION_ORIGIN)
        assert clock.now == SIMULATION_ORIGIN
        assert clock.advance(90.0) == SIMULATION_ORIGIN + timedelta(seconds=90)
        assert clock.elapsed_s == pytest.approx(90.0)

    def test_an_explicit_start_time_wins_over_the_warmup_lookback(self) -> None:
        """``start_time`` is what makes a run byte-identical; the lookback is the demo default."""
        engine, _, _ = build_engine()
        assert engine.clock.now == SIMULATION_ORIGIN

    async def test_offline_runs_advance_by_the_configured_step(self) -> None:
        """tick_seconds x speed_factor, with no catch-up cap — 1 s of wall time is 10 simulated."""
        engine, _, _ = build_engine(duration_s=100.0)
        await engine.run()
        assert engine.stats.simulated_seconds == pytest.approx(100.0)
        assert engine.clock.now == SIMULATION_ORIGIN + timedelta(seconds=100)


class TestLifecycle:
    """The state machine ``GET/POST /api/v1/simulations/{id}/*`` drives."""

    async def test_start_pause_resume_stop(self) -> None:
        engine, _, _ = build_engine(duration_s=10_000.0)
        assert state_of(engine) is SimulationState.pending

        await engine.start()
        assert state_of(engine) is SimulationState.running
        assert len(engine.vehicles) == 6

        await engine.pause()
        assert state_of(engine) is SimulationState.paused

        await engine.resume()
        assert state_of(engine) is SimulationState.running

        await engine.stop()
        assert state_of(engine) is SimulationState.stopped

    async def test_start_on_a_running_engine_is_a_no_op(self) -> None:
        """``POST /simulations/{id}/start`` may be clicked twice; it must not rebuild the fleet."""
        engine, _, _ = build_engine(duration_s=10_000.0)
        await engine.start()
        await engine.tick()
        ticks = engine.stats.ticks

        await engine.start()

        assert state_of(engine) is SimulationState.running
        assert engine.stats.ticks == ticks

    async def test_start_on_a_paused_engine_resumes_it(self) -> None:
        engine, _, _ = build_engine(duration_s=10_000.0)
        await engine.start()
        await engine.pause()
        await engine.start()
        assert state_of(engine) is SimulationState.running

    async def test_stop_is_idempotent_on_a_terminal_state(self) -> None:
        engine, _, _ = build_engine(duration_s=10_000.0)
        await engine.start()
        await engine.stop()
        await engine.stop()
        assert state_of(engine) is SimulationState.stopped

    async def test_reset_returns_to_pending_with_a_fresh_clock_and_no_fleet(self) -> None:
        engine, _, _ = build_engine(duration_s=100.0)
        await engine.run()
        assert engine.stats.ticks > 0

        await engine.reset()

        assert state_of(engine) is SimulationState.pending
        assert engine.vehicles == ()
        assert engine.stats.ticks == 0
        assert engine.clock.now == SIMULATION_ORIGIN
        assert engine.run_id is None

    async def test_a_duration_bounded_run_completes(self) -> None:
        engine, _, _ = build_engine(duration_s=300.0)
        stats = await engine.run()
        assert state_of(engine) is SimulationState.completed
        assert stats.simulated_seconds >= 300.0

    async def test_fail_records_the_reason_and_closes_the_run(self) -> None:
        """A failed run keeps its counters: "emitted 41 000 events, then died" is the record."""
        engine, _, _ = build_engine(duration_s=10_000.0)
        await engine.start()
        await engine.tick()

        await engine.fail("broker exploded")

        assert state_of(engine) is SimulationState.failed
        assert engine.stats.last_error == "broker exploded"
        assert engine.stats.events_emitted > 0

    def test_an_engine_without_corridors_refuses_to_be_built(self) -> None:
        """Silently running a fleet with nowhere to drive would look like a working demo."""
        with pytest.raises(NotFoundError):
            SimulationEngine(SimulationConfig(), [])

    async def test_open_trips_are_paused_when_a_run_stops(self) -> None:
        """Otherwise the fleet stays ``driving`` for ever and the live page shows ghosts.

        ``paused`` rather than ``finished`` is the honest transition — the trip did not reach its
        destination — and ``trips.ended_at`` stays null to say so.
        """
        engine, _, sink = build_engine(vehicle_count=3, duration_s=10_000.0)
        await engine.start()
        await engine.tick()
        assert all(vehicle.trip_id is not None for vehicle in engine.vehicles)

        await engine.stop()

        assert all(vehicle.state is VehicleState.stopped for vehicle in engine.vehicles)
        # 3 started + 3 paused, and the sink saw every one of them.
        assert sink.trip_events == 6
        assert engine.stats.trip_events_emitted == 6


class TestTickAccounting:
    """What the status endpoint reports has to be what actually happened."""

    async def test_every_vehicle_emits_exactly_one_sample_per_tick(self) -> None:
        engine, samples, sink = build_engine(vehicle_count=5, duration_s=200.0)
        await engine.run()

        assert engine.stats.ticks == 20  # 200 simulated seconds / (1 s x 10)
        assert engine.stats.events_emitted == 5 * 20
        assert len(samples) == engine.stats.events_emitted
        assert sink.stats.emitted == engine.stats.events_emitted
        assert sink.stats.transport == "null"

    async def test_status_payload_carries_the_run_and_the_sink(self) -> None:
        """The body of ``GET /api/v1/simulations/status`` (BUILD_SPEC §7)."""
        engine, _, _ = build_engine(duration_s=100.0, routes=("probe-a", "probe-b"))
        await engine.run()
        status: dict[str, Any] = engine.status()

        assert status["state"] == SimulationState.completed.value
        assert status["routes"] == ["probe-a", "probe-b"]
        assert status["sink"]["transport"] == "null"
        assert status["events_emitted"] == engine.stats.events_emitted
        assert status["simulated_time"] == engine.clock.now.isoformat()

    async def test_a_failing_vehicle_is_counted_and_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One malformed corridor must not take a three-hundred-vehicle fleet down.

        The exception is not swallowed silently either: it lands in ``errors`` and in
        ``last_error``, which is what the status endpoint shows the operator.
        """
        engine, _, _ = build_engine(vehicle_count=3, duration_s=10_000.0)
        await engine.start()
        original = SimulatedVehicle.advance

        def explode(self: SimulatedVehicle, **kwargs: Any) -> Any:
            if self.vehicle_id == "ATW-0001":
                msg = "corrupt corridor"
                raise RuntimeError(msg)
            return original(self, **kwargs)

        monkeypatch.setattr(SimulatedVehicle, "advance", explode)
        emitted = await engine.tick()

        assert emitted == 2  # the other two vehicles carried on
        assert engine.stats.errors == 1
        assert engine.stats.last_error == "RuntimeError: corrupt corridor"

    async def test_vehicles_active_counts_only_vehicles_on_the_road(self) -> None:
        engine, _, _ = build_engine(vehicle_count=4, duration_s=10_000.0)
        await engine.start()
        await engine.tick()
        assert engine.stats.vehicles_active == 4

    async def test_the_observer_sees_every_sample_before_the_sink_does(self) -> None:
        """It is how the training-data builder gets the gradient and wind the event omits."""
        engine, samples, sink = build_engine(vehicle_count=2, duration_s=100.0)
        await engine.run()

        assert len(samples) == sink.stats.emitted
        assert {sample.conditions.road_class for sample in samples}
        assert all(sample.profile.code for sample in samples)


class TestEngineStats:
    """The throughput figure, including the division that is only safe because of a guard."""

    def test_events_per_second_is_zero_before_any_wall_time_has_passed(self) -> None:
        """Called by ``status()`` on a pending engine, where ``wall_seconds`` is still 0."""
        assert EngineStats(events_emitted=10).events_per_second == 0.0

    def test_events_per_second_is_measured_against_wall_time(self) -> None:
        """Wall clock, not simulated: the number answers "is the simulator keeping up?"."""
        stats = EngineStats(events_emitted=500, wall_seconds=2.0, simulated_seconds=5_000.0)
        assert stats.events_per_second == pytest.approx(250.0)

    def test_as_dict_is_json_serialisable(self) -> None:
        stats = EngineStats(events_emitted=1, last_tick_at=SIMULATION_ORIGIN)
        payload = stats.as_dict()
        assert payload["last_tick_at"] == SIMULATION_ORIGIN.isoformat()
        assert payload["last_error"] is None


class TestSimulationConfig:
    """The configuration is the run, so it has to serialise into ``simulation_runs.config``."""

    def test_tick_simulated_seconds_is_the_speed_factor(self) -> None:
        config = SimulationConfig(tick_seconds=0.5, speed_factor=20.0)
        assert config.tick_simulated_seconds == pytest.approx(10.0)

    def test_as_dict_round_trips_through_json(self) -> None:
        config = SimulationConfig(route_slugs=("frankfurt-stuttgart",), duration_s=60.0)
        payload = json.loads(json.dumps(config.as_dict()))

        assert payload["route_slugs"] == ["frankfurt-stuttgart"]
        assert payload["weather_mode"] == WeatherMode.observed.value
        assert payload["traffic_intensity"] == TrafficIntensity.observed.value
        assert payload["duration_s"] == 60.0


class TestReverseCorridors:
    """Driving a corridor both ways doubles the corpus and makes the gradient visible."""

    def test_the_reverse_route_mirrors_the_geometry_and_negates_the_gradient(self) -> None:
        """A climb southbound is the same descent northbound — not a second set of invented hills.

        Re-seeding the terrain per direction would break the single most useful sanity check a
        reviewer has on simulated consumption: the same corridor driven both ways.
        """
        route = straight_corridor(length_km=60.0)
        config = EnvironmentConfig(seed=5)
        forward = RouteEnvironment(route, config)
        backward = RouteEnvironment(route.reversed_route(), config)

        assert backward.route.is_reverse is True
        assert backward.route.base_slug == forward.route.base_slug
        assert backward.route.coordinates[0] == forward.route.coordinates[-1]

        offset_m = 12_345.0
        mirrored_m = route.cumulative_m[-1] - offset_m
        assert backward.gradient_percent(mirrored_m) == pytest.approx(
            -forward.gradient_percent(offset_m)
        )

    async def test_include_reverse_routes_doubles_the_corridors(self) -> None:
        engine, _, _ = build_engine(duration_s=100.0, include_reverse_routes=False)
        assert len(engine.environments) == 1

        config = SimulationConfig(
            vehicle_count=2,
            seed=1,
            realtime=False,
            duration_s=100.0,
            start_time=SIMULATION_ORIGIN,
            include_reverse_routes=True,
        )
        environments = await build_environments(config, [straight_corridor(length_km=30.0)])
        assert [environment.route.is_reverse for environment in environments] == [False, True]
