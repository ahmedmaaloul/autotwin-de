"""Operational endpoints: liveness, readiness and the Prometheus scrape (BUILD_SPEC §7).

The split between ``/health`` and ``/ready`` is the whole point of having two endpoints, and it
is routinely got wrong:

* ``/health`` answers *"is this process alive?"* and performs **no I/O at all**. If it queried
  the database, a database outage would make every replica fail its liveness probe, and the
  orchestrator would respond by restarting all of them — turning a recoverable dependency
  failure into a full outage.
* ``/ready`` answers *"can this process serve traffic?"* and therefore does check the
  dependencies. A failing readiness probe removes the instance from the load balancer; it does
  not kill it.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

from fastapi import APIRouter, Response, status

from autotwin_api import __version__
from autotwin_api.deps import AppSettings
from autotwin_api.errors import ERROR_RESPONSES
from autotwin_api.metrics import render_metrics
from autotwin_contracts import HealthResponse, ReadinessChecks, ReadyResponse
from autotwin_core.db.session import check_database
from autotwin_core.logging import get_logger
from autotwin_ml.registry import get_active_model
from autotwin_streaming.admin import check_broker

__all__ = ["router"]

_LOGGER = get_logger(__name__)

_STARTED_MONOTONIC: Final[float] = time.monotonic()
"""Process start, captured when this module is first imported — a few milliseconds after the
interpreter starts and long before the first request. A monotonic clock rather than a wall
clock, so an NTP step cannot make the reported uptime jump or go negative."""

_PROBE_TIMEOUT_S: Final[float] = 3.0
"""Budget for the network dependencies. Both probes are a single round trip against a socket
that is either there or not; waiting longer only delays the answer ``false``."""

_MODEL_PROBE_TIMEOUT_S: Final[float] = 10.0
"""Budget for the model check, which is generous because the *first* call is not a probe: it
imports LightGBM and deserialises the booster, which takes seconds on a cold process. Every
later call hits the registry's cache and returns in microseconds. The application warms this at
startup (see ``autotwin_api.app``), so in practice the probe never pays the cold cost — the
headroom is here so that a probe which runs before warming finishes reports the truth instead of
a timeout."""

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Reports that the process is running. Performs **no** I/O: it never touches the "
        "database, the broker or the model artefact, so a dependency outage cannot trigger a "
        "restart loop. Use `/ready` to decide whether to send traffic."
    ),
)
async def health() -> HealthResponse:
    """Answer immediately with the version and how long this process has been up."""
    return HealthResponse(
        status="ok",
        version=__version__,
        uptime_s=round(time.monotonic() - _STARTED_MONOTONIC, 3),
    )


async def _check_model() -> bool:
    """Whether the active ML artefact can be loaded right now.

    Runs in a worker thread because loading a booster is blocking filesystem work, and never
    raises: a readiness endpoint that returns 500 tells an orchestrator far less than one that
    returns ``model: false``.
    """
    try:
        async with asyncio.timeout(_MODEL_PROBE_TIMEOUT_S):
            await asyncio.to_thread(get_active_model)
    except Exception as exc:
        # Deliberately broad: joblib, LightGBM and the filesystem can all fail in their own
        # way, and every one of those means the same thing here — not ready to predict.
        _LOGGER.warning("ready.model.unavailable", error=type(exc).__name__, detail=str(exc))
        return False
    else:
        return True


async def _check_kafka(*, enabled: bool) -> bool:
    """Whether the broker is usable, or irrelevant.

    With ``AUTOTWIN_KAFKA_ENABLED=false`` the simulator writes straight to PostgreSQL through
    the same sink interface (BUILD_SPEC §8), so there is no broker to be down. The check is
    *skipped*, and reporting ``false`` would wrongly hold the whole service out of the load
    balancer for a transport it is not using.
    """
    if not enabled:
        _LOGGER.debug("ready.kafka.skipped", reason="kafka_disabled")
        return True
    return await check_broker(_PROBE_TIMEOUT_S)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadyResponse,
            "description": "At least one dependency is not ready; the body lists which.",
        }
    },
    summary="Readiness probe",
    description=(
        "Checks the dependencies this process needs to serve traffic and returns **200** when "
        "all of them passed, **503** otherwise — with the same body either way, so a probe "
        "failure is diagnosable from the response alone.\n\n"
        "* `database` — PostgreSQL answered *and* the PostGIS extension is installed. Without "
        "PostGIS every spatial query fails, so a plain connection check would report ready "
        "for a database that cannot serve the map.\n"
        "* `kafka` — the broker answered a metadata request. Reported `true` (skipped) when "
        "`AUTOTWIN_KAFKA_ENABLED=false`, because the simulator then writes directly to "
        "PostgreSQL and no broker is required.\n"
        "* `model` — the active ML artefact loaded successfully."
    ),
)
async def ready(settings: AppSettings, response: Response) -> ReadyResponse:
    """Probe every dependency concurrently and report the combined verdict.

    Concurrently, because three sequential probes with a 3 s budget each would let a readiness
    check take nine seconds — longer than the probe interval that calls it.
    """
    database_ok, kafka_ok, model_ok = await asyncio.gather(
        check_database(_PROBE_TIMEOUT_S),
        _check_kafka(enabled=settings.kafka_enabled),
        _check_model(),
    )
    checks = ReadinessChecks(database=database_ok, kafka=kafka_ok, model=model_ok)
    is_ready = database_ok and kafka_ok and model_ok
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        _LOGGER.warning(
            "ready.degraded",
            database=database_ok,
            kafka=kafka_ok,
            model=model_ok,
        )
    return ReadyResponse(status="ready" if is_ready else "degraded", checks=checks)


@router.get(
    "/metrics",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {
            "content": {"text/plain": {}},
            "description": "Prometheus text exposition format.",
        },
        **ERROR_RESPONSES,
    },
    summary="Prometheus metrics",
    description=(
        "The service's own collector registry in the Prometheus text exposition format: "
        "request counts and latencies by route template, telemetry throughput, active "
        "simulated vehicles and ingestion row counts (BUILD_SPEC §7)."
    ),
)
async def metrics() -> Response:
    """Render the registry. Cheap enough to scrape every few seconds."""
    payload, content_type = render_metrics()
    return Response(content=payload, media_type=content_type)
