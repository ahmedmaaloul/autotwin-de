"""``/api/v1/simulations`` — the lifecycle of a simulation run, driven in this process.

A run exists in two places and the split is deliberate. The **row** in ``simulation_runs`` is
the operator's artefact: it is created by ``POST /api/v1/simulations``, survives restarts, and
is what the run table lists. The **engine** is the live object; it exists only while this
process is driving it, and :mod:`autotwin_simulator.runner` owns it.

Joining the two is the only real problem this module solves.
:class:`~autotwin_simulator.engine.DatabaseSimulationStore` opens a *new* ``simulation_runs``
row when an engine starts, which is right for the standalone CLI and wrong here — the API has
already created the row the operator is looking at. :class:`_AdoptedRunStore` therefore
overrides ``create_run`` to adopt that row instead of inserting another, and everything else
(vehicle registration, trip opening, counter updates, closing the run out) is inherited
unchanged.

One run at a time: the runner is process-wide (``get_runner``/``set_runner``), so starting a
second run while one is live answers ``409 conflict`` rather than silently replacing it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Final
from uuid import UUID, uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Path, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from autotwin_api.deps import AppSettings, DbSession, Pagination
from autotwin_api.metrics import (
    ACTIVE_SIMULATED_VEHICLES,
    TELEMETRY_EVENTS_FAILED_TOTAL,
    TELEMETRY_EVENTS_RECEIVED_TOTAL,
)
from autotwin_api.pagination import paginate_rows
from autotwin_api.schemas.simulation import (
    KafkaStatus,
    SimulationCreate,
    SimulationRunDetail,
    SimulationRunSummary,
    SimulatorStatus,
    resolve_traffic_intensity,
    resolve_weather_mode,
)
from autotwin_contracts import ALL_TOPICS, Page, SimulationState, utc_now
from autotwin_core.config import Settings
from autotwin_core.db.models import SimulationRun
from autotwin_core.db.session import get_sessionmaker
from autotwin_core.errors import ConflictError, NotFoundError
from autotwin_core.logging import get_logger
from autotwin_simulator.engine import (
    DEFAULT_VEHICLE_MIX,
    DatabaseSimulationStore,
    SimulationConfig,
    SimulationEngine,
)
from autotwin_simulator.runner import SimulationRunner, get_runner, set_runner
from autotwin_streaming.admin import check_broker

__all__ = ["router"]

_LOGGER = get_logger(__name__)

router = APIRouter()

_BROKER_PROBE_TTL_S: Final[float] = 15.0
"""How long a broker probe result is reused.

The status endpoint is polled every three seconds by the simulation page. Bootstrapping a
metadata request that often would put more load on the broker than the telemetry does, and the
answer to "is the broker up?" does not change meaningfully within fifteen seconds.
"""

_BROKER_PROBE_TIMEOUT_S: Final[float] = 2.0
"""Budget for one probe — short, because the endpoint has a caller waiting on it."""

_START_GRACE_S: Final[float] = 8.0
"""How long ``POST /{id}/start`` waits for the engine to come up before answering anyway.

Starting a run resolves its corridors first, and building the environment for every seeded
corridor — elevation profile, weather, traffic, charging candidates per 500 m bin — takes tens
of seconds for the full set. Blocking an HTTP request for that long is not acceptable, and
neither is answering "started" before anything can fail.

Eight seconds is the compromise: long enough that the failures which happen *immediately* (no
corridors seeded, no vehicle_models rows, an unreachable database) come back as a proper HTTP
error, short enough that a browser never waits on corridor building. If the grace expires the
run keeps starting in the background and the response says `pending`; the client is already
polling `/api/v1/simulations/status`, which reports the engine coming up.
"""

_PENDING_STARTS: Final[set[asyncio.Task[Any]]] = set()
"""Strong references to in-flight start tasks.

``asyncio`` only holds a weak reference to a running task, so a task nobody keeps can be
garbage-collected mid-await. This set is what stops a simulation from vanishing between two
ticks of the event loop.
"""

_TERMINAL_STATES: Final[frozenset[SimulationState]] = frozenset(
    {
        SimulationState.stopped,
        SimulationState.completed,
        SimulationState.failed,
    }
)


class _BrokerProbe:
    """A time-to-live cache around :func:`~autotwin_streaming.admin.check_broker`."""

    __slots__ = ("_checked_at", "_result")

    def __init__(self) -> None:
        """Start with no cached answer."""
        self._result: bool | None = None
        self._checked_at = 0.0

    async def connected(self) -> bool:
        """Whether the broker answered recently, probing at most once per TTL."""
        now = time.monotonic()
        if self._result is not None and now - self._checked_at < _BROKER_PROBE_TTL_S:
            return self._result
        self._result = await check_broker(_BROKER_PROBE_TIMEOUT_S)
        self._checked_at = now
        return self._result


_BROKER = _BrokerProbe()


class _SinkCounters:
    """Turns the sink's absolute counters into Prometheus counter increments.

    A ``SinkStats`` is a snapshot of one sink object, and a new run builds a new sink — so the
    absolute numbers restart at zero every run while a Prometheus counter must never go
    backwards. This class keeps the last observed values and increments by the difference,
    treating a decrease as "a new sink" and re-basing rather than emitting a negative delta.

    The increments happen when the status is read. That is a real limitation and it is stated
    rather than hidden: in a Kafka deployment the authoritative telemetry counters belong to the
    consumer process, which counts what it actually persisted. What this exposes is what *this*
    process's simulator has produced.
    """

    __slots__ = ("_emitted", "_failed")

    def __init__(self) -> None:
        """Start with nothing observed."""
        self._emitted = 0
        self._failed = 0

    def observe(self, *, emitted: int, failed: int) -> None:
        """Advance the counters by whatever is new since the last observation."""
        if emitted < self._emitted or failed < self._failed:
            self._emitted, self._failed = 0, 0
        if emitted > self._emitted:
            TELEMETRY_EVENTS_RECEIVED_TOTAL.inc(emitted - self._emitted)
            self._emitted = emitted
        if failed > self._failed:
            TELEMETRY_EVENTS_FAILED_TOTAL.inc(failed - self._failed)
            self._failed = failed


_SINK_COUNTERS = _SinkCounters()


class _AdoptedRunStore(DatabaseSimulationStore):
    """A store that writes into a ``simulation_runs`` row the API already created.

    Only ``create_run`` differs from the parent: instead of inserting a row with a fresh id, it
    moves the adopted row into ``running`` and hands its id back to the engine. Everything
    downstream — ``vehicles.simulation_run_id``, the counter updates, ``finish_run`` — then
    refers to the row the operator is watching, which is the whole point.

    The counters are reset as part of the transition, so restarting a run after ``/reset`` does
    not continue counting from the previous attempt.
    """

    __slots__ = ("_adopted_run_id", "_sessions")

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        run_id: UUID,
    ) -> None:
        """Bind the session factory and the row this engine must write into."""
        super().__init__(sessionmaker)
        self._sessions = sessionmaker
        self._adopted_run_id = run_id

    async def create_run(self, config: SimulationConfig, *, started_at: datetime) -> UUID | None:
        """Move the adopted row into ``running`` and return its id.

        ``config`` is not written back: the API stored the engine configuration when it created
        the row, and it is the same object this engine was built from. Rewriting it here would
        only create a second place for the two to disagree.
        """
        async with self._sessions() as session, session.begin():
            await session.execute(
                sa.text(
                    """
                    UPDATE simulation_runs
                       SET state = CAST(:state AS simulation_state),
                           started_at = :started_at,
                           stopped_at = NULL,
                           events_emitted = 0,
                           errors = 0,
                           updated_at = now()
                     WHERE id = :id
                    """
                ),
                {
                    "id": self._adopted_run_id,
                    "state": SimulationState.running.value,
                    "started_at": started_at,
                },
            )
        return self._adopted_run_id


class _AdoptingRunner(SimulationRunner):
    """A :class:`SimulationRunner` whose engine writes into an existing run row.

    The runner builds its engine in ``_build_engine``; there is no public seam for the store, so
    this override rebuilds the engine from the parent's result. It touches nothing private: the
    corridors, the configuration and the opened sink all come off the engine's public
    properties, and the discarded instance was never started, so it owns nothing. The sink stays
    registered with the parent, which is what closes it on shutdown.
    """

    __slots__ = ("_adopted_run_id",)

    def __init__(
        self,
        config: SimulationConfig,
        *,
        run_id: UUID,
        settings: Settings | None = None,
    ) -> None:
        """Configure a runner bound to ``run_id``."""
        super().__init__(config, settings=settings)
        self._adopted_run_id = run_id

    @property
    def adopted_run_id(self) -> UUID:
        """The ``simulation_runs`` row this runner drives."""
        return self._adopted_run_id

    async def _build_engine(self) -> SimulationEngine:
        """Build the engine the parent would, then give it the adopting store."""
        prepared = await super()._build_engine()
        return SimulationEngine(
            prepared.config,
            prepared.environments,
            sink=prepared.sink,
            store=_AdoptedRunStore(get_sessionmaker(), run_id=self._adopted_run_id),
        )


def _forget_start(task: asyncio.Task[Any]) -> None:
    """Drop a finished start task, and release the runner if it failed after the grace period.

    Without this the exception of a task nobody awaited would surface only as asyncio's
    "Task exception was never retrieved" warning at garbage-collection time, and the process
    would keep a dead runner installed — so every later start would be told the process is busy.
    """
    _PENDING_STARTS.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    _LOGGER.error(
        "simulation.start.failed",
        error=type(error).__name__,
        detail=str(error),
    )
    set_runner(None)
    ACTIVE_SIMULATED_VEHICLES.set(0)


def _resolved_mix(mix: Mapping[str, float] | None) -> dict[str, float]:
    """Fall back to the default German fleet composition when no mix was requested.

    The engine refuses an empty mix — rightly, since a fleet of no vehicles is a configuration
    mistake — so "the caller did not care" has to be turned into a real mix *here* rather than
    being passed through as ``{}`` and failing at start time, several requests later.
    """
    weighted = {code: float(share) for code, share in (mix or {}).items() if share > 0.0}
    return weighted or dict(DEFAULT_VEHICLE_MIX)


def _detail(row: SimulationRun, *, current_run_id: UUID | None) -> SimulationRunDetail:
    """Project a run row onto the detail payload."""
    return SimulationRunDetail(
        id=row.id,
        name=row.name,
        state=row.state,
        vehicle_count=row.vehicle_count,
        speed_factor=row.speed_factor,
        seed=row.seed,
        started_at=row.started_at,
        stopped_at=row.stopped_at,
        events_emitted=row.events_emitted,
        errors=row.errors,
        weather_mode=row.weather_mode,
        traffic_intensity=row.traffic_intensity,
        vehicle_mix=dict(row.vehicle_mix),
        route_slugs=list(row.route_slugs),
        config=dict(row.config),
        created_at=row.created_at,
        is_current=current_run_id is not None and current_run_id == row.id,
    )


def _current_run_id() -> UUID | None:
    """The run this process is driving, if any."""
    runner = get_runner()
    if isinstance(runner, _AdoptingRunner):
        return runner.adopted_run_id
    engine = runner.engine if runner is not None else None
    return engine.run_id if engine is not None else None


async def _load_run(session: DbSession, run_id: UUID) -> SimulationRun:
    """Fetch a run row or raise ``404 not_found``."""
    row = (
        await session.execute(sa.select(SimulationRun).where(SimulationRun.id == run_id))
    ).scalar_one_or_none()
    if row is None:
        msg = f"no simulation run with id {run_id}"
        raise NotFoundError(msg, details={"run_id": str(run_id)})
    return row


def _config_of(row: SimulationRun, settings: Settings) -> SimulationConfig:
    """Rebuild the engine configuration a run row describes.

    ``duration_s`` comes out of the persisted ``config`` blob rather than a column because it is
    an engine concept with no column of its own; reading it back from where the engine wrote it
    keeps the two in step.
    """
    duration = row.config.get("duration_s") if isinstance(row.config, dict) else None
    return SimulationConfig(
        name=row.name,
        vehicle_count=row.vehicle_count,
        speed_factor=row.speed_factor,
        tick_seconds=settings.sim_tick_seconds,
        seed=row.seed,
        route_slugs=tuple(row.route_slugs),
        vehicle_mix=_resolved_mix(row.vehicle_mix),
        weather_mode=resolve_weather_mode(row.weather_mode),
        traffic_intensity=resolve_traffic_intensity(row.traffic_intensity),
        duration_s=float(duration) if isinstance(duration, int | float) else None,
    )


@router.get(
    "",
    response_model=Page[SimulationRunSummary],
    summary="List simulation runs",
    description="Every run this deployment has created, newest first.",
)
async def list_simulations(
    session: DbSession,
    page: Pagination,
) -> Page[SimulationRunSummary]:
    """Page the run history."""
    statement = sa.select(
        SimulationRun.id,
        SimulationRun.name,
        SimulationRun.state,
        SimulationRun.vehicle_count,
        SimulationRun.speed_factor,
        SimulationRun.seed,
        SimulationRun.started_at,
        SimulationRun.stopped_at,
        SimulationRun.events_emitted,
        SimulationRun.errors,
    ).order_by(SimulationRun.created_at.desc())
    return await paginate_rows(
        session,
        statement,
        page,
        lambda row: SimulationRunSummary.model_validate(dict(row._mapping)),
    )


@router.get(
    "/status",
    response_model=SimulatorStatus,
    summary="Live simulator status",
    description=(
        "What the simulator in this API process is doing, from the engine's counters and the "
        "sink's — not from the database.\n\n"
        "`transport` reports which path telemetry is **actually** taking. With "
        "`AUTOTWIN_KAFKA_ENABLED=true` but no broker, the simulator falls back to writing "
        "straight to PostgreSQL (BUILD_SPEC §8) and this endpoint says `database` with the "
        "reason in `degraded_reason`. When no run is active the transport is what *would* be "
        "used, probed against the broker."
    ),
)
async def simulator_status(session: DbSession, settings: AppSettings) -> SimulatorStatus:
    """Report the engine's live counters, the transport in use and the broker's state."""
    runner = get_runner()
    snapshot: dict[str, Any] = runner.status() if runner is not None else {}
    sink: dict[str, Any] | None = snapshot.get("sink")

    transport = str(sink["transport"]) if sink else None
    degraded_reason = str(sink["degraded_reason"]) if sink and sink["degraded_reason"] else None
    kafka_connected = await _BROKER.connected() if settings.kafka_enabled else False

    if transport is None:
        # Nothing is running: say what the next run would use rather than guessing "database".
        transport = "kafka" if settings.kafka_enabled and kafka_connected else "database"
    elif transport == "kafka":
        kafka_connected = True
    elif transport == "null":
        # Only reachable if a caller installed a null sink (tests); it persists nothing.
        transport = "database"

    vehicles_active = int(snapshot.get("vehicles_active", 0))
    ACTIVE_SIMULATED_VEHICLES.set(vehicles_active)
    if sink is not None:
        _SINK_COUNTERS.observe(emitted=int(sink["emitted"]), failed=int(sink["failed"]))

    return SimulatorStatus(
        state=SimulationState(snapshot.get("state", SimulationState.pending.value)),
        run_id=_current_run_id(),
        vehicles_active=vehicles_active,
        events_per_second=float(snapshot.get("events_per_second", 0.0)),
        messages_processed=int(snapshot.get("events_emitted", 0)),
        db_writes=await _db_writes(session, sink=sink, transport=transport),
        errors=int(snapshot.get("errors", 0)),
        transport="kafka" if transport == "kafka" else "database",
        kafka=(
            KafkaStatus(
                connected=kafka_connected,
                bootstrap_servers=settings.kafka_bootstrap_servers,
                topics=list(ALL_TOPICS),
            )
            if settings.kafka_enabled
            else None
        ),
        degraded_reason=degraded_reason,
        simulated_time=_parse_time(snapshot.get("simulated_time")),
        last_error=snapshot.get("last_error"),
    )


def _parse_time(value: object) -> datetime | None:
    """Parse an ISO timestamp the engine reported, tolerating its absence."""
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


async def _db_writes(
    session: DbSession,
    *,
    sink: dict[str, Any] | None,
    transport: str,
) -> int:
    """How many telemetry samples have actually reached PostgreSQL.

    With the database sink the sink itself counts them, which is exact and free. With Kafka the
    rows are written by a separate consumer process that this API cannot see, so the honest
    answer is a count from the table — restricted to the current run's window, because the
    question is "is this run being persisted?", not "how much telemetry exists?".
    """
    if sink is not None and transport != "kafka":
        return int(sink["emitted"])
    run_id = _current_run_id()
    if run_id is None:
        return 0
    started_at = (
        await session.execute(sa.select(SimulationRun.started_at).where(SimulationRun.id == run_id))
    ).scalar_one_or_none()
    if started_at is None:
        return 0
    return int(
        (
            await session.execute(
                sa.text("SELECT count(*) FROM telemetry WHERE ingested_at >= :since"),
                {"since": started_at},
            )
        ).scalar_one()
    )


@router.post(
    "",
    response_model=SimulationRunDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Create a simulation run",
    description=(
        "Records a run in `pending` and returns it. Nothing is simulated until "
        "`POST /api/v1/simulations/{id}/start`, so a configuration can be prepared and reviewed "
        "before it produces data.\n\n"
        "The same `seed` and configuration reproduce a run exactly — which is what makes a "
        "simulated training set defensible (BUILD_SPEC §10.2)."
    ),
)
async def create_simulation(session: DbSession, payload: SimulationCreate) -> SimulationRunDetail:
    """Persist the requested configuration as a pending run.

    The weather and traffic vocabularies are resolved to the engine's own values *here* rather
    than at start time, so the stored row says what will actually be simulated instead of what
    the form happened to call it.
    """
    weather = resolve_weather_mode(payload.weather_mode)
    traffic = resolve_traffic_intensity(payload.traffic_intensity)
    config = SimulationConfig(
        name=payload.name,
        vehicle_count=payload.vehicle_count,
        speed_factor=payload.speed_factor,
        seed=payload.seed,
        route_slugs=tuple(payload.route_slugs),
        vehicle_mix=_resolved_mix(payload.vehicle_mix),
        weather_mode=weather,
        traffic_intensity=traffic,
        duration_s=payload.duration_s,
    )
    row = SimulationRun(
        id=uuid4(),
        name=payload.name,
        state=SimulationState.pending,
        vehicle_count=payload.vehicle_count,
        speed_factor=payload.speed_factor,
        weather_mode=weather.value,
        traffic_intensity=traffic.value,
        vehicle_mix=dict(config.vehicle_mix),
        route_slugs=list(payload.route_slugs),
        seed=payload.seed,
        events_emitted=0,
        errors=0,
        config=config.as_dict(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    _LOGGER.info("simulation.created", run_id=str(row.id), vehicles=row.vehicle_count)
    return _detail(row, current_run_id=_current_run_id())


RunIdPath = Annotated[UUID, Path(description="Identifier of the simulation run.")]


@router.get(
    "/{run_id}",
    response_model=SimulationRunDetail,
    summary="One simulation run",
    description="The run's configuration and counters, including whether this process drives it.",
)
async def get_simulation(session: DbSession, run_id: RunIdPath) -> SimulationRunDetail:
    """Read one run row."""
    row = await _load_run(session, run_id)
    return _detail(row, current_run_id=_current_run_id())


@router.post(
    "/{run_id}/start",
    response_model=SimulationRunDetail,
    summary="Start a run",
    description=(
        "Starts the run in this API process. Resolving the corridors takes seconds to tens of "
        "seconds, so the handler waits only briefly for the engine to come up: an immediate "
        "failure (no corridors seeded, no vehicle profiles) is returned as an error, while a "
        "slow start answers with the run still `pending` and continues in the background. Poll "
        "`GET /api/v1/simulations/status` to watch it come up.\n\n"
        "One run at a time: while another run is live this answers **409 conflict**, because "
        "the runner is process-wide and two fleets writing into the same `telemetry` table "
        "would be indistinguishable afterwards. Starting a run that is already running is a "
        "no-op; starting a paused one resumes it."
    ),
)
async def start_simulation(
    session: DbSession,
    settings: AppSettings,
    run_id: RunIdPath,
) -> SimulationRunDetail:
    """Build a runner for this row, install it as the process runner and start the engine.

    Raises:
        ConflictError: A different run is already live in this process — ``409 conflict``.
        NotFoundError: No run with this id, or no corridors are seeded to drive on.
    """
    row = await _load_run(session, run_id)
    runner = get_runner()
    current = _current_run_id()

    if runner is not None and runner.is_running and current != run_id:
        msg = (
            f"simulation {current} is already running in this process; stop it before starting "
            f"{run_id}"
        )
        raise ConflictError(msg, details={"running_run_id": str(current)})

    if runner is not None and current == run_id:
        # Same run: start() resumes a paused engine and is a no-op for a running one.
        await runner.start()
    else:
        runner = _AdoptingRunner(_config_of(row, settings), run_id=run_id, settings=settings)
        set_runner(runner)
        # Move the row to `pending` before the engine is built. A run being restarted still
        # carries the previous attempt's terminal state, and answering "stopped" to a start
        # request that succeeded reads as a failure; the store flips it to `running` as soon as
        # the corridors are resolved.
        row.state = SimulationState.pending
        row.stopped_at = None
        await session.commit()
        await session.refresh(row)
        task = asyncio.create_task(runner.start(), name=f"autotwin-start-{run_id}")
        _PENDING_STARTS.add(task)
        task.add_done_callback(_forget_start)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=_START_GRACE_S)
        except TimeoutError:
            _LOGGER.info("simulation.start.building", run_id=str(run_id), grace_s=_START_GRACE_S)
        except Exception:
            # A failure to start owns no engine and must not leave a dead runner installed,
            # or every later request would be told the process is busy.
            set_runner(None)
            raise

    ACTIVE_SIMULATED_VEHICLES.set(runner.status().get("vehicles_active", 0))
    _LOGGER.info("simulation.started", run_id=str(run_id), vehicles=row.vehicle_count)
    await session.refresh(row)
    return _detail(row, current_run_id=_current_run_id())


@router.post(
    "/{run_id}/pause",
    response_model=SimulationRunDetail,
    summary="Pause a run",
    description=(
        "Holds the simulated clock. The fleet keeps its state and the buffered telemetry is "
        "flushed, so a paused run leaves nothing in memory that a crash would lose."
    ),
)
async def pause_simulation(session: DbSession, run_id: RunIdPath) -> SimulationRunDetail:
    """Pause the engine and record the state on the row."""
    row = await _load_run(session, run_id)
    runner = _require_current(run_id)
    await runner.pause()
    return await _set_state(session, row, SimulationState.paused)


@router.post(
    "/{run_id}/resume",
    response_model=SimulationRunDetail,
    summary="Resume a paused run",
    description="Continues from exactly where the pause left the fleet.",
)
async def resume_simulation(session: DbSession, run_id: RunIdPath) -> SimulationRunDetail:
    """Resume the engine and record the state on the row."""
    row = await _load_run(session, run_id)
    runner = _require_current(run_id)
    await runner.resume()
    return await _set_state(session, row, SimulationState.running)


@router.post(
    "/{run_id}/stop",
    response_model=SimulationRunDetail,
    summary="Stop a run",
    description=(
        "Drains the current tick, flushes the transport and closes the run row out. Waits for "
        "the drain rather than cancelling, so the last batch reaches the database."
    ),
)
async def stop_simulation(session: DbSession, run_id: RunIdPath) -> SimulationRunDetail:
    """Stop the engine, release the process runner and return the closed-out row."""
    row = await _load_run(session, run_id)
    runner = get_runner()
    if runner is not None and _current_run_id() == run_id:
        await runner.stop()
        set_runner(None)
        ACTIVE_SIMULATED_VEHICLES.set(0)
        await session.refresh(row)
        if row.state not in _TERMINAL_STATES:
            return await _set_state(session, row, SimulationState.stopped)
        return _detail(row, current_run_id=_current_run_id())
    # Not the live run: the row is simply marked stopped, which is what an operator means when
    # stopping a run this process never owned (an API restart, say).
    return await _set_state(session, row, SimulationState.stopped)


@router.post(
    "/{run_id}/reset",
    response_model=SimulationRunDetail,
    summary="Reset a run",
    description=(
        "Stops the run if it is live and returns it to `pending` with zeroed counters, so it "
        "can be started again from the same seed. The telemetry it already wrote is **not** "
        "deleted — it is evidence of what happened, and the new attempt is distinguished by "
        "its own `started_at`."
    ),
)
async def reset_simulation(session: DbSession, run_id: RunIdPath) -> SimulationRunDetail:
    """Return the run to pending, stopping the engine first if it is this process's."""
    row = await _load_run(session, run_id)
    runner = get_runner()
    if runner is not None and _current_run_id() == run_id:
        await runner.reset()
        set_runner(None)
        ACTIVE_SIMULATED_VEHICLES.set(0)
    row.state = SimulationState.pending
    row.started_at = None
    row.stopped_at = None
    row.events_emitted = 0
    row.errors = 0
    await session.commit()
    await session.refresh(row)
    _LOGGER.info("simulation.reset", run_id=str(run_id))
    return _detail(row, current_run_id=_current_run_id())


def _require_current(run_id: UUID) -> SimulationRunner:
    """Return the process runner, provided it is driving ``run_id``.

    Raises:
        ConflictError: This process is not running that run, so there is nothing to pause or
            resume. A 409 rather than a 404 because the *run* exists; its engine does not.
    """
    runner = get_runner()
    if runner is None or _current_run_id() != run_id:
        msg = f"simulation {run_id} is not running in this process"
        raise ConflictError(msg, details={"run_id": str(run_id)})
    return runner


async def _set_state(
    session: DbSession,
    row: SimulationRun,
    state: SimulationState,
) -> SimulationRunDetail:
    """Persist a lifecycle transition on the run row and return the refreshed detail."""
    row.state = state
    if state in _TERMINAL_STATES and row.stopped_at is None:
        row.stopped_at = utc_now()
    await session.commit()
    await session.refresh(row)
    _LOGGER.info("simulation.state", run_id=str(row.id), state=state.value)
    return _detail(row, current_run_id=_current_run_id())
