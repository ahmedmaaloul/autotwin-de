"""Process-level supervision of a :class:`~autotwin_simulator.engine.SimulationEngine`.

The engine knows how to tick. This module knows how to *own* one: where its corridors come
from, which transport it emits through, what happens when it raises, and how it is shut down.

Two callers, one object.

**In-process, from FastAPI.** ``POST /api/v1/simulations/{id}/start`` calls
:meth:`SimulationRunner.start` on the module-level runner, which spawns an asyncio task and
returns immediately; ``/pause``, ``/resume``, ``/stop`` and ``/reset`` reach the same instance,
and ``GET /api/v1/simulations/status`` reads :meth:`SimulationRunner.status`. The API's
``lifespan`` shutdown calls :func:`shutdown_runner`, which is why the runner is a singleton with
an explicit teardown rather than a task nobody holds a reference to.

**Standalone, from the CLI.** :func:`run_standalone` builds a runner, installs SIGINT/SIGTERM
handlers, and waits for the engine to finish. A ``docker stop`` or a Ctrl-C therefore drains the
current tick, flushes the sink and closes the ``simulation_runs`` row, instead of leaving a run
stuck in ``running`` for ever — which is what a bare ``KeyboardInterrupt`` through the middle of
an ``await`` would do.

The supervision contract is small and deliberate: a failure inside the engine's loop sets the
run to :attr:`~autotwin_contracts.enums.SimulationState.failed`, records the exception on the
run row, and stops. It does not restart. A simulator that silently restarts itself would
produce a telemetry stream with an invisible discontinuity in it, and the whole point of a
seeded simulation is that its history is explicable.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final

from autotwin_contracts import SimulationState, utc_now
from autotwin_core.config import Settings, get_settings
from autotwin_core.db.session import dispose_engine, get_sessionmaker, session_scope
from autotwin_core.logging import get_logger
from autotwin_simulator.engine import (
    DatabaseSimulationStore,
    EngineStats,
    NullSimulationStore,
    SimulationConfig,
    SimulationEngine,
    SimulationStore,
    build_environments,
    load_routes_for,
)
from autotwin_simulator.environment import RouteEnvironment
from autotwin_streaming.sinks import TelemetrySink, create_sink

__all__ = [
    "SimulationRunner",
    "get_runner",
    "run_standalone",
    "set_runner",
    "shutdown_runner",
]

_LOGGER = get_logger(__name__)

SinkFactory = Callable[[], Awaitable[TelemetrySink]]
"""How a runner obtains its transport. Injected so tests can pass a ``NullTelemetrySink``
without a broker, a database or a monkey-patched module."""

_SHUTDOWN_GRACE_S: Final[float] = 15.0
"""How long :meth:`SimulationRunner.stop` waits for the engine task to drain before cancelling.

Generous compared with a tick, because the final flush of a full batch has to reach PostGIS.
Exceeding it means something is genuinely wedged, and cancelling is then the better outcome."""


class SimulationRunner:
    """One engine, its background task, and the resources it owns.

    Not an ``asyncio.Task`` subclass and not a context manager by default: the FastAPI service
    holds this object across many requests, so its lifetime is explicitly managed rather than
    scoped to a block.
    """

    __slots__ = (
        "_config",
        "_engine",
        "_environments",
        "_settings",
        "_sink",
        "_sink_factory",
        "_stop_task",
        "_task",
        "_use_database",
    )

    def __init__(
        self,
        config: SimulationConfig,
        *,
        settings: Settings | None = None,
        sink_factory: SinkFactory | None = None,
        environments: Sequence[RouteEnvironment] | None = None,
        use_database: bool = True,
    ) -> None:
        """Configure a runner without starting anything.

        ``environments`` short-circuits corridor loading for callers that already hold resolved
        corridors — the offline training-data generator and the tests. ``use_database=False``
        additionally turns off persistence, so the run leaves no ``simulation_runs``,
        ``vehicles`` or ``trips`` rows behind; the two are independent because a run against
        pre-built corridors may still legitimately want to be recorded.
        """
        self._config = config
        self._settings = settings if settings is not None else get_settings()
        self._sink_factory = sink_factory
        self._environments = tuple(environments) if environments is not None else None
        self._use_database = use_database
        self._engine: SimulationEngine | None = None
        self._sink: TelemetrySink | None = None
        self._task: asyncio.Task[EngineStats] | None = None
        self._stop_task: asyncio.Task[EngineStats | None] | None = None

    # -- introspection ------------------------------------------------------------------

    @property
    def config(self) -> SimulationConfig:
        """The configuration this runner was built with."""
        return self._config

    @property
    def engine(self) -> SimulationEngine | None:
        """The engine, once :meth:`start` has built it."""
        return self._engine

    @property
    def state(self) -> SimulationState:
        """Lifecycle state of the run; ``pending`` before the engine exists."""
        return self._engine.state if self._engine is not None else SimulationState.pending

    @property
    def is_running(self) -> bool:
        """True while the background task is alive."""
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        """Serialisable snapshot for ``GET /api/v1/simulations/status``.

        Answers even before a run exists, because a status endpoint that 404s when nothing is
        running tells an operator far less than one that says ``pending``.
        """
        if self._engine is None:
            return {
                "state": SimulationState.pending.value,
                "run_id": None,
                "vehicles_active": 0,
                "events_emitted": 0,
                "events_per_second": 0.0,
                "errors": 0,
                "sink": None,
            }
        return {**self._engine.status(), "task_alive": self.is_running}

    # -- lifecycle ----------------------------------------------------------------------

    async def start(self) -> SimulationEngine:
        """Resolve corridors, open the transport, build the engine and spawn its task.

        Returns as soon as the task is scheduled — the caller is an HTTP handler that must not
        block for the length of a simulation. :meth:`wait` is there for the callers that do want
        to block.
        """
        if self.is_running and self._engine is not None:
            await self._engine.resume()
            return self._engine
        engine = await self._build_engine()
        self._engine = engine
        await engine.start()
        self._task = asyncio.create_task(self._supervise(engine), name="autotwin-simulator")
        return engine

    async def _build_engine(self) -> SimulationEngine:
        """Load the corridors, build the environments, open the sink and assemble the engine."""
        environments = self._environments
        if environments is None:
            async with session_scope() as session:
                routes = await load_routes_for(session, self._config)
                environments = tuple(
                    await build_environments(
                        self._config,
                        routes,
                        session=session,
                        now=utc_now(),
                    )
                )
        store: SimulationStore = (
            DatabaseSimulationStore(get_sessionmaker())
            if self._use_database
            else NullSimulationStore()
        )
        self._sink = await self._open_sink()
        return SimulationEngine(self._config, environments, sink=self._sink, store=store)

    async def _open_sink(self) -> TelemetrySink:
        """Obtain the transport — Kafka when it answers, PostGIS otherwise (BUILD_SPEC §8)."""
        if self._sink_factory is not None:
            return await self._sink_factory()
        return await create_sink(self._settings)

    async def _supervise(self, engine: SimulationEngine) -> EngineStats:
        """Run the engine to completion, mapping an unexpected failure onto ``failed``.

        The engine already tolerates a single vehicle raising; anything that escapes its loop is
        a defect in the simulator itself, and the honest response is to stop, mark the run
        failed and say why — not to retry into an unexplainable telemetry stream.
        """
        try:
            return await engine.run()
        except asyncio.CancelledError:
            _LOGGER.info("simulator.runner.cancelled", ticks=engine.stats.ticks)
            raise
        except Exception as exc:
            engine.stats.errors += 1
            engine.stats.last_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.exception(
                "simulator.runner.failed",
                error=type(exc).__name__,
                detail=str(exc),
            )
            await engine.fail(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            await self._close_sink()

    async def pause(self) -> None:
        """Hold the clock; the task stays alive and the fleet keeps its state."""
        if self._engine is not None:
            await self._engine.pause()

    async def resume(self) -> None:
        """Continue a paused run."""
        if self._engine is not None:
            await self._engine.resume()

    async def stop(self) -> EngineStats | None:
        """Ask the engine to finish, then wait for the task to drain.

        Waits rather than cancelling, so the final batch reaches the database and the
        ``simulation_runs`` row is closed. Cancellation is the fallback after
        :data:`_SHUTDOWN_GRACE_S`, and it is logged as the exceptional path it is.
        """
        if self._engine is None:
            return None
        await self._engine.stop()
        stats = await self.wait(timeout_s=_SHUTDOWN_GRACE_S)
        await self._close_sink()
        return stats if stats is not None else self._engine.stats

    def request_stop(self) -> None:
        """Schedule a graceful stop from a synchronous context, such as a signal handler.

        A signal callback cannot await, and :func:`signal.signal` is deliberately not used: its
        handler runs between bytecodes and can interrupt an ``await`` in the middle of a
        database write, whereas a loop-level handler is delivered between awaits and lets the
        current tick finish.
        """
        self._stop_task = asyncio.get_running_loop().create_task(
            self.stop(), name="autotwin-simulator-stop"
        )

    async def reset(self) -> None:
        """Stop the run if it is alive, then return the engine to ``pending``."""
        if self._engine is None:
            return
        if self.is_running:
            await self.stop()
        await self._engine.reset()

    async def wait(self, *, timeout_s: float | None = None) -> EngineStats | None:
        """Block until the engine's task finishes; cancel it if ``timeout_s`` elapses."""
        task = self._task
        if task is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            _LOGGER.warning("simulator.runner.drain_timeout", timeout_s=timeout_s)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return None
        except Exception:
            # Already logged and recorded on the run row by _supervise; the caller asked how the
            # run ended, and "it failed" is answered by state, not by re-raising here.
            return None
        finally:
            if task.done():
                self._task = None

    async def _close_sink(self) -> None:
        """Flush and close the transport exactly once."""
        sink = self._sink
        if sink is None:
            return
        self._sink = None
        await sink.aclose()


# --------------------------------------------------------------------------------------
# The process-wide runner the API drives
# --------------------------------------------------------------------------------------

_runner: SimulationRunner | None = None


def get_runner() -> SimulationRunner | None:
    """The runner this process owns, or ``None`` when no run has been created."""
    return _runner


def set_runner(runner: SimulationRunner | None) -> None:
    """Install (or clear) the process-wide runner.

    A setter rather than a factory because the API decides what a run *is* — it holds the
    ``simulation_runs`` row the operator created through ``POST /api/v1/simulations`` — while
    this module only knows how to supervise one.
    """
    global _runner
    _runner = runner


async def shutdown_runner() -> None:
    """Stop and forget the process-wide runner. Safe to call when there is none.

    Called from the API's ``lifespan`` shutdown, where it must not raise: a failure to stop a
    simulation cleanly should not prevent the process from exiting.
    """
    runner = _runner
    set_runner(None)
    if runner is None:
        return
    try:
        await runner.stop()
    except Exception as exc:
        _LOGGER.warning(
            "simulator.runner.shutdown_failed",
            error=type(exc).__name__,
            detail=str(exc),
        )


# --------------------------------------------------------------------------------------
# Standalone entry point
# --------------------------------------------------------------------------------------


async def run_standalone(
    config: SimulationConfig,
    *,
    settings: Settings | None = None,
    sink_factory: SinkFactory | None = None,
    install_signal_handlers: bool = True,
) -> EngineStats:
    """Run one simulation in this process until it finishes or is signalled.

    This is what ``python -m autotwin_simulator.cli run`` and the Compose service execute.
    Signal handlers are installed on the running loop rather than with :func:`signal.signal`, so
    the stop request is delivered as a normal callback between awaits and the current tick
    completes instead of unwinding mid-write.
    """
    runner = SimulationRunner(config, settings=settings, sink_factory=sink_factory)
    set_runner(runner)
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    if install_signal_handlers:
        installed = _install_handlers(loop, runner)
    try:
        engine = await runner.start()
        stats = await runner.wait()
        return stats if stats is not None else engine.stats
    finally:
        for number in installed:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(number)
        set_runner(None)
        await dispose_engine()


def _install_handlers(
    loop: asyncio.AbstractEventLoop,
    runner: SimulationRunner,
) -> list[signal.Signals]:
    """Install SIGINT/SIGTERM handlers that ask the engine to stop, and report which took.

    ``add_signal_handler`` is unavailable on Windows and inside a non-main thread; both raise,
    and both are legitimate places to run a simulator without signal handling, so the failure is
    swallowed per signal rather than aborting the run.
    """
    installed: list[signal.Signals] = []
    for number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(number, _request_stop, runner, number)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(number)
    return installed


def _request_stop(runner: SimulationRunner, number: signal.Signals) -> None:
    """Signal callback: log the request and schedule a graceful stop on the loop.

    The task is kept on the runner rather than dropped on the floor, so that a garbage
    collection between the signal and the first await cannot cancel the shutdown.
    """
    _LOGGER.info("simulator.signal", signal=number.name)
    runner.request_stop()
