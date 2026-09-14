"""The simulation engine: a fleet, a clock, a sink, and a lifecycle the API can drive.

Three design decisions are worth stating up front, because everything else follows from them.

**Nothing in the tick loop touches the database.** Corridors, their weather, their traffic and
their charging sites are all resolved once, at :meth:`SimulationEngine.start`, into
:class:`~autotwin_simulator.environment.RouteEnvironment` objects backed by precomputed arrays.
After that a tick is pure arithmetic plus one batched write through the
:class:`~autotwin_streaming.sinks.TelemetrySink`. That is what lets several hundred vehicles run
on a laptop; a per-vehicle query would cap the fleet at a few dozen.

**The clock catches up with the present and then tracks it.** Simulated time starts
:attr:`SimulationConfig.warmup_lookback_s` in the past and advances ``speed_factor`` simulated
seconds per wall second *until it reaches now*, after which it advances in real time. A digital
twin that fast-forwards an hour of history and then follows the present is both the useful
demo behaviour and the only one that never writes a telemetry row dated in the future — which a
naive ``speed_factor`` of 10 does within minutes, breaking every downstream freshness check.
Offline runs (``realtime=False``) skip the cap entirely and run as fast as the CPU allows.

**The same seed produces the same telemetry.** Every random draw goes through
:func:`~autotwin_simulator.environment.seeded_rng` with a key that names the vehicle and the
concern, so neither the number of vehicles nor the order they are stepped in can shift another
vehicle's trace. With a pinned ``start_time`` two runs are byte-identical; the engine's own
reproducibility test asserts exactly that.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Self
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotwin_contracts import (
    DataOrigin,
    SimulationState,
    SourceSystem,
    TripEvent,
    VehicleProfile,
    VehicleState,
    get_vehicle_profile,
    utc_now,
)
from autotwin_core.config import Settings, get_settings
from autotwin_core.errors import ConfigurationMissing, NotFoundError, ValidationError
from autotwin_core.logging import get_logger
from autotwin_simulator.driver import Driver, sample_driver_profile
from autotwin_simulator.environment import (
    EnvironmentConfig,
    RouteEnvironment,
    SimulationRoute,
    TrafficIntensity,
    WeatherMode,
    build_route_environment,
    load_simulation_routes,
    seeded_rng,
)
from autotwin_simulator.vehicle import SimulatedVehicle, TelemetrySample
from autotwin_streaming.sinks import NullTelemetrySink, TelemetrySink

__all__ = [
    "DEFAULT_PHYSICS_STEP_S",
    "DEFAULT_VEHICLE_MIX",
    "DEFAULT_WARMUP_LOOKBACK_S",
    "DatabaseSimulationStore",
    "EngineStats",
    "NullSimulationStore",
    "SimulationClock",
    "SimulationConfig",
    "SimulationEngine",
    "SimulationStore",
    "build_environments",
    "load_routes_for",
    "resolve_vehicle_mix",
]

_LOGGER = get_logger(__name__)

DEFAULT_VEHICLE_MIX: Final[Mapping[str, float]] = {
    "compact_ev": 0.35,
    "sedan_ev": 0.25,
    "suv_ev": 0.20,
    "van_ev": 0.10,
    "performance_ev": 0.10,
}
"""Share of each generic profile (BUILD_SPEC §9) in a default simulated fleet.

Weighted toward the compact and sedan classes because that is what the German EV fleet is: the
registration statistics are dominated by compact hatchbacks and mid-size saloons, with vans a
commercial minority and the performance class a rounding error that is nonetheless worth
simulating because its consumption is so different."""

DEFAULT_PHYSICS_STEP_S: Final[float] = 1.0
"""Integration step of the vehicle physics, in simulated seconds.

Independent of the tick length and of the speed factor: a tick covering 10 simulated seconds is
integrated as ten 1-second steps. One second is short compared with the ~6 s time constant of
the driver's speed response, so the forward-Euler integration is stable and the acceleration
trace is resolved rather than aliased."""

DEFAULT_WARMUP_LOOKBACK_S: Final[float] = 3_600.0
"""How far in the past simulated time starts, in seconds.

One hour: a live demo has a populated history the moment it starts, and at the default speed
factor of 10 the clock catches up with the present after about seven wall-clock minutes."""

_MIN_TICK_SIMULATED_S: Final[float] = 0.1
"""Floor on how much simulated time one tick advances, so a caught-up clock never stalls."""

_COUNTER_PERSIST_TICKS: Final[int] = 10
"""How often the ``simulation_runs`` counters are written back. Every tick would be an UPDATE
per second for a row nobody reads that often."""

_INITIAL_SOC_RANGE: Final[tuple[float, float]] = (35.0, 95.0)
"""Uniform range of a vehicle's state of charge when a run starts.

A fleet that all starts at 100 % would not charge for hours and the demo would show no charging
at all; one that all starts at 20 % would stampede to the same chargers. The spread is what
makes the charging behaviour look like a fleet."""


# --------------------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class SimulationClock:
    """Simulated time: an origin plus the seconds elapsed since it.

    A value object rather than calls to ``datetime.now()`` scattered through the engine, because
    the reproducibility guarantee depends on every timestamp in a run deriving from one origin
    that the caller can pin.
    """

    origin: datetime
    """Instant simulated time starts at (UTC)."""

    elapsed_s: float = 0.0
    """Simulated seconds since :attr:`origin`."""

    @property
    def now(self) -> datetime:
        """Current simulated instant."""
        return self.origin + timedelta(seconds=self.elapsed_s)

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward and return the instant it now reads."""
        self.elapsed_s += seconds
        return self.now


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Everything that defines a run — and therefore everything that has to be reproducible."""

    name: str = "AutoTwin Demo"
    vehicle_count: int = 40
    speed_factor: float = 10.0
    tick_seconds: float = 1.0
    seed: int = 20_260_214
    route_slugs: tuple[str, ...] = ()
    """Corridors to drive. Empty means "every corridor in ``routes``, demo ones first"."""

    vehicle_mix: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_VEHICLE_MIX))
    weather_mode: WeatherMode = WeatherMode.observed
    traffic_intensity: TrafficIntensity = TrafficIntensity.observed
    duration_s: float | None = None
    """Simulated seconds to run for; ``None`` runs until stopped."""

    physics_step_s: float = DEFAULT_PHYSICS_STEP_S
    realtime: bool = True
    """Sleep between ticks and never let simulated time overtake the wall clock.

    ``False`` is the offline mode used by ``generate-training-data`` and by the reproducibility
    test: no sleeping, no clock cap, and therefore a simulated hour in a couple of seconds."""

    warmup_lookback_s: float = DEFAULT_WARMUP_LOOKBACK_S
    start_time: datetime | None = None
    """Pin simulated time's origin. Required for a byte-identical rerun; ``None`` derives it
    from the wall clock and :attr:`warmup_lookback_s`."""

    include_reverse_routes: bool = True
    """Drive each corridor in both directions, which doubles the corpus and — because the
    synthetic terrain is mirrored, not re-seeded — makes the gradient's effect visible."""

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **overrides: Any) -> Self:
        """Build a configuration from ``AUTOTWIN_SIM_*``, with explicit overrides on top."""
        resolved = settings if settings is not None else get_settings()
        base: dict[str, Any] = {
            "vehicle_count": resolved.sim_default_vehicles,
            "tick_seconds": resolved.sim_tick_seconds,
            "speed_factor": resolved.sim_speed_factor,
            "seed": resolved.sim_seed,
        }
        base.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**base)

    @property
    def tick_simulated_seconds(self) -> float:
        """Simulated seconds one tick covers before the catch-up cap is applied."""
        return self.tick_seconds * self.speed_factor

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, stored in ``simulation_runs.config``."""
        return {
            "name": self.name,
            "vehicle_count": self.vehicle_count,
            "speed_factor": self.speed_factor,
            "tick_seconds": self.tick_seconds,
            "physics_step_s": self.physics_step_s,
            "seed": self.seed,
            "route_slugs": list(self.route_slugs),
            "vehicle_mix": dict(self.vehicle_mix),
            "weather_mode": self.weather_mode.value,
            "traffic_intensity": self.traffic_intensity.value,
            "duration_s": self.duration_s,
            "realtime": self.realtime,
            "warmup_lookback_s": self.warmup_lookback_s,
            "include_reverse_routes": self.include_reverse_routes,
        }


# --------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class EngineStats:
    """Live counters of a run — the body of ``GET /api/v1/simulations/status``."""

    ticks: int = 0
    events_emitted: int = 0
    trip_events_emitted: int = 0
    errors: int = 0
    vehicles_active: int = 0
    simulated_seconds: float = 0.0
    wall_seconds: float = 0.0
    last_tick_at: datetime | None = None
    last_error: str | None = None

    @property
    def events_per_second(self) -> float:
        """Telemetry events per **wall-clock** second.

        Wall clock rather than simulated: this is a throughput measure that answers "is the
        simulator keeping up?", and normalising it by simulated time would hide exactly the
        slowdown it exists to reveal.
        """
        if self.wall_seconds <= 0.0:
            return 0.0
        return self.events_emitted / self.wall_seconds

    def as_dict(self) -> dict[str, Any]:
        """Serialisable snapshot."""
        return {
            "ticks": self.ticks,
            "events_emitted": self.events_emitted,
            "trip_events_emitted": self.trip_events_emitted,
            "errors": self.errors,
            "vehicles_active": self.vehicles_active,
            "simulated_seconds": round(self.simulated_seconds, 3),
            "wall_seconds": round(self.wall_seconds, 3),
            "events_per_second": round(self.events_per_second, 2),
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_error": self.last_error,
        }


# --------------------------------------------------------------------------------------
# Persistence of the run itself (the telemetry goes through the sink)
# --------------------------------------------------------------------------------------


class SimulationStore:
    """Where a run's ``simulation_runs``, ``vehicles`` and ``trips`` rows go.

    Deliberately *not* the telemetry path — that is the sink's job, and it is shared with the
    Kafka consumer. This interface exists because those three tables are the simulator's own
    bookkeeping: the consumer explicitly refuses to create a ``trips`` row
    (:func:`~autotwin_streaming.sinks.apply_trip_event`) because only the simulator knows the
    ``vehicles.id`` and the provenance block it needs.

    The null implementation is the base class rather than a subclass, so an engine constructed
    without a database is not a special case anywhere in the tick loop.
    """

    async def create_run(self, config: SimulationConfig, *, started_at: datetime) -> UUID | None:
        """Open a ``simulation_runs`` row and return its id.

        The base implementation persists nothing and returns ``None``, which the engine carries
        through as "this run has no database identity".
        """
        return None

    async def register_vehicles(
        self,
        run_id: UUID | None,
        vehicles: Sequence[SimulatedVehicle],
    ) -> None:
        """Ensure a ``vehicles`` row exists for every simulated vehicle. Base: does nothing."""

    async def open_trips(self, events: Sequence[tuple[TripEvent, SimulatedVehicle]]) -> None:
        """Insert the ``trips`` rows for trips that just started. Base: does nothing."""

    async def update_counters(self, run_id: UUID | None, stats: EngineStats) -> None:
        """Write the live counters back onto the run row. Base: does nothing."""

    async def finish_run(
        self,
        run_id: UUID | None,
        state: SimulationState,
        stats: EngineStats,
        *,
        stopped_at: datetime,
    ) -> None:
        """Close the run row out with its final state and counters. Base: does nothing."""


class NullSimulationStore(SimulationStore):
    """Explicit no-op store, for offline runs and tests. Inherits every method unchanged."""


class DatabaseSimulationStore(SimulationStore):
    """PostGIS-backed store using the process-wide async session factory.

    Every method opens its own short transaction rather than holding one across the run: a
    simulation is a long-lived process, and a transaction open for an hour would pin the
    autovacuumer's horizon and block every schema change in the meantime.
    """

    __slots__ = ("_sessionmaker", "_vehicle_model_ids", "_vehicle_row_ids")

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        """Bind the session factory and prepare the id caches."""
        self._sessionmaker = sessionmaker
        self._vehicle_model_ids: dict[str, UUID] = {}
        self._vehicle_row_ids: dict[str, UUID] = {}

    async def create_run(self, config: SimulationConfig, *, started_at: datetime) -> UUID | None:
        """Insert the run row in ``running`` state and return its id."""
        run_id = uuid4()
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    INSERT INTO simulation_runs (
                        id, name, state, vehicle_count, speed_factor, weather_mode,
                        traffic_intensity, vehicle_mix, route_slugs, seed, started_at,
                        events_emitted, errors, config
                    ) VALUES (
                        :id, :name, CAST(:state AS simulation_state), :vehicle_count,
                        :speed_factor, :weather_mode, :traffic_intensity,
                        CAST(:vehicle_mix AS jsonb), CAST(:route_slugs AS jsonb), :seed,
                        :started_at, 0, 0, CAST(:config AS jsonb)
                    )
                    """
                ),
                {
                    "id": run_id,
                    "name": config.name,
                    "state": SimulationState.running.value,
                    "vehicle_count": config.vehicle_count,
                    "speed_factor": config.speed_factor,
                    "weather_mode": config.weather_mode.value,
                    "traffic_intensity": config.traffic_intensity.value,
                    "vehicle_mix": _json(dict(config.vehicle_mix)),
                    "route_slugs": _json(list(config.route_slugs)),
                    "seed": config.seed,
                    "started_at": started_at,
                    "config": _json(config.as_dict()),
                },
            )
        return run_id

    async def register_vehicles(
        self,
        run_id: UUID | None,
        vehicles: Sequence[SimulatedVehicle],
    ) -> None:
        """Upsert one ``vehicles`` row per simulated vehicle, in a single statement.

        An upsert rather than an insert because ``vehicles.vehicle_id`` is a stable fleet
        identifier: ``ATW-0042`` is the same car in every run, which is what makes its trip
        history worth looking at. A new run re-points it at itself and resets its state.
        """
        if not vehicles:
            return
        async with self._sessionmaker() as session, session.begin():
            await self._load_vehicle_model_ids(session)
            missing = sorted(
                {
                    vehicle.profile.code
                    for vehicle in vehicles
                    if vehicle.profile.code not in self._vehicle_model_ids
                }
            )
            if missing:
                msg = (
                    f"vehicle_models has no row for {', '.join(missing)}; "
                    "run the 0002_seed_vehicle_models migration"
                )
                raise ConfigurationMissing(msg, details={"codes": missing})
            rows = [
                {
                    "id": uuid4(),
                    "vehicle_id": vehicle.vehicle_id,
                    "vehicle_model_id": self._vehicle_model_ids[vehicle.profile.code],
                    "simulation_run_id": run_id,
                    "state": vehicle.state.value,
                    "source": SourceSystem.simulator.value,
                    "data_origin": DataOrigin.simulated.value,
                }
                for vehicle in vehicles
            ]
            await session.execute(
                sa.text(
                    """
                    INSERT INTO vehicles (
                        id, vehicle_id, vehicle_model_id, simulation_run_id, state,
                        source, data_origin, ingested_at
                    ) VALUES (
                        :id, :vehicle_id, :vehicle_model_id, :simulation_run_id,
                        CAST(:state AS vehicle_state), CAST(:source AS source_system),
                        CAST(:data_origin AS data_origin), now()
                    )
                    ON CONFLICT (vehicle_id) DO UPDATE
                       SET vehicle_model_id = EXCLUDED.vehicle_model_id,
                           simulation_run_id = EXCLUDED.simulation_run_id,
                           state = EXCLUDED.state,
                           updated_at = now()
                    """
                ),
                rows,
            )
            # Read the surrogate keys back in a second statement rather than with RETURNING:
            # SQLAlchemy dispatches a list of parameter dictionaries as an executemany, which
            # discards result rows. The trips table needs vehicles.id, so it is fetched here.
            identities = await session.execute(
                sa.text("SELECT id, vehicle_id FROM vehicles WHERE vehicle_id = ANY(:ids)"),
                {"ids": [vehicle.vehicle_id for vehicle in vehicles]},
            )
            for row in identities.mappings():
                self._vehicle_row_ids[row["vehicle_id"]] = row["id"]

    async def _load_vehicle_model_ids(self, session: AsyncSession) -> None:
        """Cache the ``vehicle_models.code → id`` mapping; it never changes during a run."""
        if self._vehicle_model_ids:
            return
        result = await session.execute(sa.text("SELECT code, id FROM vehicle_models"))
        self._vehicle_model_ids = {row["code"]: row["id"] for row in result.mappings()}

    async def open_trips(self, events: Sequence[tuple[TripEvent, SimulatedVehicle]]) -> None:
        """Insert the ``trips`` rows for a tick's worth of newly started trips."""
        if not events:
            return
        rows = [
            {
                "id": uuid4(),
                "trip_id": event.trip_id,
                "vehicle_id": self._vehicle_row_ids[event.vehicle_id],
                "route_id": event.route_id,
                "started_at": event.occurred_at,
                "start_soc_percent": event.soc_percent if event.soc_percent is not None else 0.0,
                "state": VehicleState.driving.value,
                "source": SourceSystem.simulator.value,
                "data_origin": DataOrigin.simulated.value,
            }
            for event, _vehicle in events
            if event.vehicle_id in self._vehicle_row_ids
        ]
        if not rows:
            return
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    INSERT INTO trips (
                        id, trip_id, vehicle_id, route_id, started_at, start_soc_percent,
                        distance_m, energy_kwh, state, source, data_origin, ingested_at
                    ) VALUES (
                        :id, :trip_id, :vehicle_id, :route_id, :started_at,
                        :start_soc_percent, 0, 0, CAST(:state AS vehicle_state),
                        CAST(:source AS source_system), CAST(:data_origin AS data_origin), now()
                    )
                    ON CONFLICT (trip_id) DO NOTHING
                    """
                ),
                rows,
            )

    async def update_counters(self, run_id: UUID | None, stats: EngineStats) -> None:
        """Refresh ``events_emitted`` and ``errors`` on the run row."""
        if run_id is None:
            return
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    UPDATE simulation_runs
                       SET events_emitted = :events_emitted,
                           errors = :errors,
                           updated_at = now()
                     WHERE id = :id
                    """
                ),
                {
                    "id": run_id,
                    "events_emitted": stats.events_emitted,
                    "errors": stats.errors,
                },
            )

    async def finish_run(
        self,
        run_id: UUID | None,
        state: SimulationState,
        stats: EngineStats,
        *,
        stopped_at: datetime,
    ) -> None:
        """Write the terminal state, the stop time and the final counters."""
        if run_id is None:
            return
        async with self._sessionmaker() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    UPDATE simulation_runs
                       SET state = CAST(:state AS simulation_state),
                           stopped_at = :stopped_at,
                           events_emitted = :events_emitted,
                           errors = :errors,
                           updated_at = now()
                     WHERE id = :id
                    """
                ),
                {
                    "id": run_id,
                    "state": state.value,
                    "stopped_at": stopped_at,
                    "events_emitted": stats.events_emitted,
                    "errors": stats.errors,
                },
            )


def _json(value: Any) -> str:
    """Serialise a value for a ``jsonb`` bind parameter.

    Explicit rather than relying on psycopg's automatic ``dict`` adaptation, so that the SQL
    casts and the Python types are visible in the same place and a nested value that is not
    JSON-serialisable fails here rather than inside the driver.
    """
    return json.dumps(value, default=str)


# --------------------------------------------------------------------------------------
# Fleet construction
# --------------------------------------------------------------------------------------


def resolve_vehicle_mix(mix: Mapping[str, float], count: int) -> list[VehicleProfile]:
    """Expand a proportional vehicle mix into exactly ``count`` profiles.

    Largest-remainder apportionment (the Hare-Niemeyer method German electoral law uses), so
    that a mix of ``{compact: 0.35, ...}`` over 40 vehicles produces exactly 40 vehicles with
    the closest achievable proportions, and never 39 or 41. Ties break on the profile code, so
    the result depends only on the mix and the count — not on dictionary ordering.

    Raises :class:`~autotwin_core.errors.ConfigurationMissing` for an empty or non-positive mix:
    a fleet of no vehicles is a configuration mistake, not a degenerate run to tolerate.
    """
    weights = {code: weight for code, weight in mix.items() if weight > 0.0}
    if not weights or count <= 0:
        msg = f"vehicle mix {dict(mix)!r} and count {count} cannot produce a fleet"
        raise ValidationError(msg, details={"mix": dict(mix), "count": count})
    total = sum(weights.values())
    exact = {code: weight / total * count for code, weight in weights.items()}
    allocated = {code: int(value) for code, value in exact.items()}
    remaining = count - sum(allocated.values())
    order = sorted(exact, key=lambda code: (-(exact[code] - allocated[code]), code))
    for code in order[:remaining]:
        allocated[code] += 1
    profiles: list[VehicleProfile] = []
    for code in sorted(allocated):
        profiles.extend([get_vehicle_profile(code)] * allocated[code])
    return profiles


async def build_environments(
    config: SimulationConfig,
    routes: Sequence[SimulationRoute],
    *,
    session: AsyncSession | None = None,
    now: datetime | None = None,
) -> list[RouteEnvironment]:
    """Resolve every corridor (and its reverse) into a ready-to-read environment."""
    environment_config = EnvironmentConfig(
        seed=config.seed,
        weather_mode=config.weather_mode,
        traffic_intensity=config.traffic_intensity,
    )
    directed: list[SimulationRoute] = []
    for route in routes:
        directed.append(route)
        if config.include_reverse_routes:
            directed.append(route.reversed_route())
    return [
        await build_route_environment(route, environment_config, session=session, now=now)
        for route in directed
    ]


# --------------------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------------------


class SimulationEngine:
    """Owns a fleet, a clock and a sink, and drives them through the ``SimulationState`` cycle.

    Constructed with its corridors already resolved so that the object is usable in three very
    different contexts without change: the CLI, the FastAPI service (through
    :mod:`autotwin_simulator.runner`) and the offline training-data generator.
    """

    __slots__ = (
        "_config",
        "_environments",
        "_observer",
        "_run_id",
        "_sink",
        "_started_wall",
        "_state",
        "_stop_requested",
        "_store",
        "_vehicles",
        "clock",
        "stats",
    )

    def __init__(
        self,
        config: SimulationConfig,
        environments: Sequence[RouteEnvironment],
        *,
        sink: TelemetrySink | None = None,
        store: SimulationStore | None = None,
        observer: Callable[[TelemetrySample], None] | None = None,
    ) -> None:
        """Bind the configuration, corridors, transport and bookkeeping of one run.

        ``observer`` receives every sample before it is handed to the sink. It is how
        :mod:`autotwin_simulator.training_data` gets at the gradient, wind and vehicle
        parameters that the telemetry event deliberately does not carry.
        """
        if not environments:
            msg = (
                "a simulation needs at least one corridor; seed the demo routes first "
                "(python -m autotwin_ingestion.cli seed routes)"
            )
            raise NotFoundError(msg, details={"route_slugs": list(config.route_slugs)})
        self._config = config
        self._environments = tuple(environments)
        self._sink = sink if sink is not None else NullTelemetrySink()
        self._store = store if store is not None else NullSimulationStore()
        self._observer = observer
        self._state = SimulationState.pending
        self._stop_requested = False
        self._run_id: UUID | None = None
        self._started_wall = 0.0
        self.clock = SimulationClock(origin=_resolve_origin(config))
        self.stats = EngineStats()
        self._vehicles: list[SimulatedVehicle] = []

    # -- introspection ------------------------------------------------------------------

    @property
    def config(self) -> SimulationConfig:
        """The configuration this engine was built with."""
        return self._config

    @property
    def state(self) -> SimulationState:
        """Current lifecycle state (BUILD_SPEC §2)."""
        return self._state

    @property
    def run_id(self) -> UUID | None:
        """``simulation_runs.id`` of this run, once started and persisted."""
        return self._run_id

    @property
    def vehicles(self) -> tuple[SimulatedVehicle, ...]:
        """The fleet, in a stable order."""
        return tuple(self._vehicles)

    @property
    def sink(self) -> TelemetrySink:
        """The transport telemetry is emitted through."""
        return self._sink

    @property
    def environments(self) -> tuple[RouteEnvironment, ...]:
        """The resolved corridors, forward and reverse."""
        return self._environments

    def status(self) -> dict[str, Any]:
        """A serialisable snapshot for ``GET /api/v1/simulations/status``."""
        return {
            "state": self._state.value,
            "run_id": str(self._run_id) if self._run_id else None,
            "simulated_time": self.clock.now.isoformat(),
            "routes": [environment.route.slug for environment in self._environments],
            "sink": self._sink.stats.as_dict(),
            **self.stats.as_dict(),
        }

    # -- lifecycle ----------------------------------------------------------------------

    async def start(self) -> None:
        """Build the fleet, persist the run, and move to ``running``.

        Idempotent for a run already in flight: calling ``start`` on a running engine is a
        no-op rather than an error, because the API's ``POST /simulations/{id}/start`` may well
        be clicked twice.
        """
        if self._state is SimulationState.running:
            return
        if self._state is SimulationState.paused:
            await self.resume()
            return
        self._build_fleet()
        self._started_wall = time.monotonic()
        self._stop_requested = False
        self._run_id = await self._store.create_run(self._config, started_at=utc_now())
        await self._store.register_vehicles(self._run_id, self._vehicles)
        self._state = SimulationState.running
        _LOGGER.info(
            "simulator.started",
            run_id=str(self._run_id) if self._run_id else None,
            vehicles=len(self._vehicles),
            routes=[environment.route.slug for environment in self._environments],
            speed_factor=self._config.speed_factor,
            seed=self._config.seed,
            transport=self._sink.transport,
            simulated_time=self.clock.now.isoformat(),
        )

    async def pause(self) -> None:
        """Hold the clock where it is; the fleet keeps its state."""
        if self._state is SimulationState.running:
            self._state = SimulationState.paused
            await self._sink.flush()
            _LOGGER.info("simulator.paused", ticks=self.stats.ticks)

    async def resume(self) -> None:
        """Continue a paused run from exactly where it stopped."""
        if self._state is SimulationState.paused:
            self._state = SimulationState.running
            _LOGGER.info("simulator.resumed", ticks=self.stats.ticks)

    async def stop(self) -> None:
        """Ask the run to finish: drain the current tick, flush, and close the run row."""
        if self._state in _TERMINAL_STATES:
            return
        self._stop_requested = True
        self._state = SimulationState.stopping
        await self._finalise(SimulationState.stopped)

    async def reset(self) -> None:
        """Return to ``pending`` with a fresh clock and an empty fleet.

        The corridors are kept — they are expensive to resolve and did not change — so a reset
        is cheap and a re-``start`` with the same seed reproduces the run exactly.
        """
        if self._state is SimulationState.running:
            await self.stop()
        self._vehicles = []
        self._run_id = None
        self._stop_requested = False
        self.clock = SimulationClock(origin=_resolve_origin(self._config))
        self.stats = EngineStats()
        self._state = SimulationState.pending
        _LOGGER.info("simulator.reset")

    # -- running ------------------------------------------------------------------------

    async def run(self) -> EngineStats:
        """Tick until the configured duration elapses or the run is stopped.

        In real-time mode the loop sleeps for whatever is left of ``tick_seconds`` after the
        tick's work, so a slow tick shortens the sleep rather than accumulating drift.
        """
        if self._state is SimulationState.pending:
            await self.start()
        while self._state in (SimulationState.running, SimulationState.paused):
            started = time.monotonic()
            if self._state is SimulationState.paused:
                await asyncio.sleep(self._config.tick_seconds)
                continue
            await self.tick()
            if self._duration_reached():
                await self._finalise(SimulationState.completed)
                break
            if self._stop_requested:
                break
            if self._config.realtime:
                await asyncio.sleep(
                    max(0.0, self._config.tick_seconds - (time.monotonic() - started))
                )
        return self.stats

    async def tick(self) -> int:
        """Advance every vehicle by one tick and emit what they produced.

        Returns the number of telemetry events emitted. A vehicle that raises is logged,
        counted in :attr:`EngineStats.errors` and skipped — one malformed corridor must not take
        a three-hundred-vehicle fleet down with it — but the exception is never swallowed
        silently.
        """
        step_s = self._tick_simulated_seconds()
        start_time = self.clock.now
        end_time = self.clock.advance(step_s)

        samples: list[TelemetrySample] = []
        trip_events: list[TripEvent] = []
        started_trips: list[tuple[TripEvent, SimulatedVehicle]] = []

        for vehicle in self._vehicles:
            try:
                if vehicle.needs_route():
                    event = vehicle.begin_trip(self._next_environment(vehicle), start_time)
                    trip_events.append(event)
                    started_trips.append((event, vehicle))
                step = vehicle.advance(
                    start_time=start_time,
                    duration_s=step_s,
                    physics_step_s=self._config.physics_step_s,
                )
            except Exception as exc:
                self.stats.errors += 1
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                _LOGGER.error(
                    "simulator.vehicle_failed",
                    vehicle_id=vehicle.vehicle_id,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                continue
            samples.append(step.sample)
            trip_events.extend(step.trip_events)

        await self._store.open_trips(started_trips)
        if self._observer is not None:
            for sample in samples:
                self._observer(sample)
        if samples:
            await self._sink.emit_many([sample.event for sample in samples])
        for event in trip_events:
            await self._sink.emit_trip_event(event)

        self.stats.ticks += 1
        self.stats.events_emitted += len(samples)
        self.stats.trip_events_emitted += len(trip_events)
        self.stats.simulated_seconds += step_s
        self.stats.wall_seconds = time.monotonic() - self._started_wall
        self.stats.last_tick_at = end_time
        self.stats.vehicles_active = sum(
            1
            for vehicle in self._vehicles
            if vehicle.state in (VehicleState.driving, VehicleState.charging, VehicleState.stopped)
        )
        if self.stats.ticks % _COUNTER_PERSIST_TICKS == 0:
            await self._store.update_counters(self._run_id, self.stats)
        return len(samples)

    # -- internals ----------------------------------------------------------------------

    def _tick_simulated_seconds(self) -> float:
        """How far this tick advances simulated time, after the catch-up cap.

        Offline runs take the configured step unchanged. Real-time runs take the smaller of the
        configured step and the gap to the wall clock, so simulated time converges on the
        present from below and then tracks it, never overtaking it.
        """
        step_s = self._config.tick_simulated_seconds
        if not self._config.realtime:
            return step_s
        gap_s = (utc_now() - self.clock.now).total_seconds()
        return max(_MIN_TICK_SIMULATED_S, min(step_s, gap_s))

    def _duration_reached(self) -> bool:
        """True once the run has simulated as much time as it was asked to."""
        duration = self._config.duration_s
        return duration is not None and self.stats.simulated_seconds >= duration

    def _build_fleet(self) -> None:
        """Create the vehicles, their drivers and their starting state, reproducibly."""
        profiles = resolve_vehicle_mix(self._config.vehicle_mix, self._config.vehicle_count)
        trip_token = f"{self._config.seed:x}-{int(self.clock.origin.timestamp()):x}"
        self._vehicles = []
        for index, profile in enumerate(profiles, start=1):
            vehicle_id = f"ATW-{index:04d}"
            rng = seeded_rng(self._config.seed, "fleet", vehicle_id)
            environment = self._environments[index % len(self._environments)]
            ambient_c = environment.weather_at(
                environment.route.coordinate_at(0.0),
                self.clock.origin,
            )[0]
            self._vehicles.append(
                SimulatedVehicle(
                    vehicle_id=vehicle_id,
                    profile=profile,
                    environment=environment,
                    driver=Driver(
                        sample_driver_profile(self._config.seed, vehicle_id),
                        seed=self._config.seed,
                        vehicle_id=vehicle_id,
                    ),
                    seed=self._config.seed,
                    trip_token=trip_token,
                    soc_percent=rng.uniform(*_INITIAL_SOC_RANGE),
                    ambient_temperature_c=ambient_c,
                )
            )

    def _next_environment(self, vehicle: SimulatedVehicle) -> RouteEnvironment:
        """Choose the corridor for a vehicle's next trip.

        Seeded per vehicle rather than round-robin over a shared counter: a shared counter would
        make a vehicle's route depend on how many *other* vehicles happened to finish first,
        which is exactly the kind of coupling that breaks reproducibility when the fleet size
        changes.
        """
        rng = seeded_rng(self._config.seed, "route-choice", vehicle.vehicle_id, vehicle.trip_count)
        return self._environments[rng.randrange(len(self._environments))]

    async def fail(self, reason: str) -> None:
        """Mark the run failed and close it out.

        The supervisor calls this when an exception escapes :meth:`run`. A failed run keeps its
        counters and its ``stopped_at``, because "it emitted 41 000 events and then died" is a
        far more useful record than a row stuck in ``running`` for ever.
        """
        self.stats.last_error = reason
        await self._finalise(SimulationState.failed)

    async def _finalise(self, state: SimulationState) -> None:
        """Close every open trip, flush the sink, and close the run row out in ``state``."""
        await self._pause_open_trips(state)
        await self._sink.flush()
        self._state = state
        self.stats.wall_seconds = time.monotonic() - self._started_wall
        await self._store.finish_run(self._run_id, state, self.stats, stopped_at=utc_now())
        _LOGGER.info(
            "simulator.finished",
            state=state.value,
            ticks=self.stats.ticks,
            events=self.stats.events_emitted,
            trip_events=self.stats.trip_events_emitted,
            errors=self.stats.errors,
            events_per_second=round(self.stats.events_per_second, 1),
        )

    async def _pause_open_trips(self, state: SimulationState) -> None:
        """Emit a ``paused`` transition for every trip still in flight when a run ends.

        Without it a stopped run leaves its trips ``driving`` and its vehicles ``driving`` for
        ever, and ``GET /api/v1/vehicles`` would show a fleet that has been on the road since
        last Tuesday. ``paused`` rather than ``finished`` is the honest transition: the trip did
        not reach its destination, it was interrupted, and ``trips.ended_at`` stays null to say
        so (:func:`~autotwin_streaming.sinks.apply_trip_event` encodes that distinction).
        """
        reason = f"simulation_{state.value}"
        for vehicle in self._vehicles:
            if vehicle.trip_id is None or vehicle.state in (
                VehicleState.idle,
                VehicleState.completed,
            ):
                continue
            await self._sink.emit_trip_event(vehicle.pause_trip(self.clock.now, reason=reason))
            self.stats.trip_events_emitted += 1


_TERMINAL_STATES: Final[frozenset[SimulationState]] = frozenset(
    {
        SimulationState.stopped,
        SimulationState.completed,
        SimulationState.failed,
    }
)
"""States a run cannot leave; ``stop()`` on one of them is a no-op rather than an error."""


def _resolve_origin(config: SimulationConfig) -> datetime:
    """Where simulated time starts.

    An explicit :attr:`SimulationConfig.start_time` wins and is what a reproducible run pins.
    Otherwise the origin is ``now - warmup_lookback_s``, which is what gives a live demo an hour
    of history to fast-forward through before it settles into real time.
    """
    if config.start_time is not None:
        return config.start_time.astimezone(UTC)
    return utc_now() - timedelta(seconds=config.warmup_lookback_s)


async def load_routes_for(
    session: AsyncSession,
    config: SimulationConfig,
) -> list[SimulationRoute]:
    """Load the corridors a configuration names, failing loudly when they are missing.

    A run configured for ``frankfurt-stuttgart`` that silently drove some other corridor would
    be worse than a run that refused to start, so a requested slug that is not in ``routes`` is
    a :class:`~autotwin_core.errors.NotFoundError` naming both what was asked for and what is
    available.
    """
    routes = await load_simulation_routes(session, slugs=config.route_slugs or None)
    if not routes:
        available = [route.slug for route in await load_simulation_routes(session)]
        msg = (
            "no corridors found in the routes table"
            if not available
            else f"none of {list(config.route_slugs)} is a known corridor"
        )
        raise NotFoundError(
            msg, details={"requested": list(config.route_slugs), "available": available}
        )
    return routes
