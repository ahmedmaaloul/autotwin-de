"""Application factory — the skeleton every router plugs into.

``create_app()`` is a factory rather than a module-level ``app`` so that a test can build an
instance with its own settings, and so that importing anything from this package does not open
a database pool as a side effect. :mod:`autotwin_api.main` holds the single instance uvicorn
serves.

The factory owns three things the routers deliberately do not:

* **The URL layout.** Every router module exposes a bare ``router`` with no prefix and no tags;
  :data:`_ROUTER_MOUNTS` is therefore the one place where the whole API's shape is readable,
  and a router cannot quietly relocate itself.
* **The middleware order.** Stated once, outermost first, with the reasoning attached — that
  order decides whether an error response carries a correlation id and whether a browser can
  read the data-mode header.
* **The process lifecycle.** Logging configuration, engine warm-up and an orderly shutdown of
  the simulator, the provider HTTP clients and the connection pools.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, FastAPI
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from autotwin_api import __version__
from autotwin_api.errors import ERROR_RESPONSES, register_exception_handlers
from autotwin_api.middleware import (
    AccessLogMiddleware,
    DataModeMiddleware,
    RequestIdMiddleware,
    TimeoutMiddleware,
)
from autotwin_contracts import DATA_MODE_HEADER, REQUEST_ID_HEADER
from autotwin_core.config import Settings, get_settings
from autotwin_core.db.session import check_database, dispose_engine, get_engine
from autotwin_core.logging import configure_logging, get_logger
from autotwin_ingestion.providers.registry import close_providers
from autotwin_ml.registry import get_active_model
from autotwin_simulator.runner import shutdown_runner

__all__ = ["create_app"]

_LOGGER = get_logger(__name__)

_API_PREFIX: Final[str] = "/api/v1"
"""Base path of every domain endpoint (BUILD_SPEC §7). ``/health`` and friends sit outside it,
because an orchestrator probing liveness should not have to know the API's version."""

_ROUTERS_PACKAGE: Final[str] = "autotwin_api.routers"

_ROUTER_MOUNTS: Final[tuple[tuple[str, str, str], ...]] = (
    # (module name, prefix, OpenAPI tag)
    ("health", "", "health"),
    ("dashboard", f"{_API_PREFIX}/dashboard", "dashboard"),
    ("charging", f"{_API_PREFIX}/charging", "charging"),
    ("weather", f"{_API_PREFIX}/weather", "weather"),
    ("traffic", f"{_API_PREFIX}/traffic", "traffic"),
    ("analytics", f"{_API_PREFIX}/analytics", "analytics"),
    ("routes", f"{_API_PREFIX}/routes", "routes"),
    ("vehicles", f"{_API_PREFIX}/vehicles", "vehicles"),
    ("trips", f"{_API_PREFIX}/trips", "trips"),
    ("simulations", f"{_API_PREFIX}/simulations", "simulations"),
    ("ml", f"{_API_PREFIX}/ml", "ml"),
    ("data", f"{_API_PREFIX}/data", "data"),
    ("stream", f"{_API_PREFIX}/stream", "stream"),
    ("copilot", f"{_API_PREFIX}/copilot", "copilot"),
)
"""Every router, in the order it appears in the OpenAPI document.

Health first because it is what an operator looks for; then the dashboard, then the data
domains roughly in the order the UI's navigation presents them.
"""

OPENAPI_TAGS: Final[list[dict[str, Any]]] = [
    {
        "name": "health",
        "description": "**Betrieb** — liveness, readiness and Prometheus metrics.",
    },
    {
        "name": "dashboard",
        "description": "**Übersicht** — the aggregated headline figures of the landing page.",
    },
    {
        "name": "charging",
        "description": (
            "**Ladeinfrastruktur** — charging sites from the Bundesnetzagentur register, "
            "statistics, corridor coverage and underserved stretches."
        ),
    },
    {
        "name": "weather",
        "description": "**Wetter** — DWD observations near a point, a bounding box or a route.",
    },
    {
        "name": "traffic",
        "description": (
            "**Verkehrslage** — roadworks, closures and incidents from the Autobahn GmbH API."
        ),
    },
    {
        "name": "analytics",
        "description": (
            "**Auswertungen** — energy against temperature, speed and traffic, and the "
            "per-Bundesland infrastructure comparison."
        ),
    },
    {
        "name": "routes",
        "description": (
            "**Routen & Analyse** — corridor planning, the flagship energy analysis and "
            "charging-stop optimisation."
        ),
    },
    {
        "name": "vehicles",
        "description": (
            "**Fahrzeuge** — the simulated fleet, its live positions and its raw telemetry. "
            "Every row here is `simulated`."
        ),
    },
    {
        "name": "trips",
        "description": "**Fahrten** — completed and running trips with their SOC and speed series.",
    },
    {
        "name": "simulations",
        "description": "**Simulation** — lifecycle of a simulation run and its live statistics.",
    },
    {
        "name": "ml",
        "description": (
            "**Modelle** — the model registry, its metrics against the physical baseline, "
            "predictions and SHAP attributions."
        ),
    },
    {
        "name": "data",
        "description": (
            "**Datenqualität** — ingestion runs, per-source quality reports, licences and "
            "attribution."
        ),
    },
    {
        "name": "stream",
        "description": "**Echtzeit-Stream** — server-sent telemetry deltas (SSE).",
    },
    {
        "name": "copilot",
        "description": (
            "**Copilot** — optional question answering through a local LLM; answers 503 when "
            "`AUTOTWIN_LLM_ENABLED=false`."
        ),
    },
]
"""One bilingual line per tag. German names the domain the way the UI does; English explains it.

These strings end up in the generated TypeScript and in ``/docs``, so they are read far more
often than any other documentation in this service.
"""

_DESCRIPTION: Final[str] = f"""
Digital twin of German electric mobility: charging infrastructure, weather, traffic, route
energy analysis and a simulated EV fleet.

### Conventions

* **List responses** are paged: `{{"items": [...], "total": N, "page": 1, "page_size": 50,
  "has_next": true}}`, driven by `?page=` (≥1) and `?page_size=` (1-500).
* **Errors** — every 4xx and 5xx shares one envelope:
  `{{"error": {{"code": "provider_unavailable", "message": "...", "details": {{...}},
  "request_id": "..."}}}}`. Switch on `code`, never on the message.
* **Correlation** — `{REQUEST_ID_HEADER}` is echoed on every response, and repeated inside the
  error envelope. Send your own to trace a call across the web app and this API.
* **Data honesty** — endpoints backed by an external source set
  `{DATA_MODE_HEADER}: live | cache | fixture`. Anything but `live` means the upstream German
  source was unreachable and the answer came from cache or from a bundled fixture. Simulated
  rows always carry `data_origin: "simulated"` in their provenance block.

### Distances and units

SI throughout. Distances are metres in the database; a field a human reads is exposed in
kilometres and says so in its name (`distance_km`). Timestamps are ISO-8601 in UTC.
""".strip()


def _redact_dsn(url: str) -> str:
    """Return ``url`` with any password removed, for logging.

    The resolved configuration is logged at startup because "which database is this pointing
    at?" is the first question of every deployment problem — and the DSN is also the one
    setting that carries a credential.
    """
    parts = urlsplit(url)
    if parts.hostname is None:
        return url
    host = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
    netloc = f"{parts.username}@{host}" if parts.username else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _settings_summary(settings: Settings) -> dict[str, Any]:
    """The configuration worth putting in the startup log line — and nothing secret."""
    return {
        "version": __version__,
        "env": settings.env.value,
        "debug": settings.debug,
        "log_level": settings.log_level,
        "data_mode": settings.data_mode.value,
        "data_dir": str(settings.data_dir),
        "database": _redact_dsn(settings.database_url_async),
        "db_pool_size": settings.db_pool_size,
        "kafka_enabled": settings.kafka_enabled,
        "kafka_bootstrap_servers": settings.kafka_bootstrap_servers,
        "cors_origins": list(settings.cors_origins),
        "ml_model_dir": str(settings.ml_model_dir),
        "ml_active_model": settings.ml_active_model,
        "llm_enabled": settings.llm_enabled,
    }


def _include_router(app: FastAPI, module_name: str, *, prefix: str, tag: str) -> bool:
    """Import one router module and mount it, tolerating a module that does not exist yet.

    The tolerance is narrow on purpose. Only a :class:`ModuleNotFoundError` naming *this exact
    module* is swallowed — that is the "the router has not been written yet" case, and skipping
    it keeps the rest of the API serving. A ``ModuleNotFoundError`` for something the router
    itself imports names a different module and is re-raised, as is every other exception: a
    router that exists and is broken must fail loudly rather than vanish from the API and be
    discovered by the frontend at runtime.

    Returns:
        Whether the router was mounted.
    """
    qualified = f"{_ROUTERS_PACKAGE}.{module_name}"
    try:
        module = importlib.import_module(qualified)
    except ModuleNotFoundError as exc:
        if exc.name != qualified:
            raise
        _LOGGER.warning("api.router.not_implemented", router=module_name, module=qualified)
        return False

    router = getattr(module, "router", None)
    if not isinstance(router, APIRouter):
        msg = (
            f"{qualified} must expose `router: APIRouter` created without a prefix or tags; "
            f"found {type(router).__name__}"
        )
        raise TypeError(msg)

    app.include_router(router, prefix=prefix, tags=[tag], responses=ERROR_RESPONSES)
    _LOGGER.debug("api.router.mounted", router=module_name, prefix=prefix or "/")
    return True


async def _warm_model() -> None:
    """Load the active ML artefact once, before the first request needs it.

    Deserialising the booster imports LightGBM and takes seconds on a cold process. Paying that
    here — in a worker thread, concurrently with the database probe — means neither the first
    readiness probe nor the first prediction request pays it, and it turns "no model is trained"
    into a startup log line instead of a surprise 503 later.

    Never fatal: BUILD_SPEC §0.3 is explicit that the platform degrades rather than crashes, and
    every endpoint except ``/api/v1/ml/*`` works perfectly well without a trained model.
    """
    try:
        model = await asyncio.to_thread(get_active_model)
    except Exception as exc:
        # Broad by intention: a missing artefact, an unreadable file and a LightGBM version
        # mismatch are three different exceptions with one operational meaning.
        _LOGGER.warning(
            "api.startup.model_unavailable",
            error=type(exc).__name__,
            detail=str(exc),
            hint="run `python -m autotwin_ml.cli train` to make /api/v1/ml/* available",
        )
    else:
        _LOGGER.info("api.startup.model_ready", model=model.record.label)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down of the API process.

    Start-up warms the connection pool with the same probe ``/ready`` uses, so PostGIS being
    absent is discovered in the startup log rather than in the first map request. It is
    deliberately **not** fatal: BUILD_SPEC §0.3 says degrade rather than crash, and a process
    that refuses to start cannot even serve ``/ready`` to explain why.

    Shut-down releases, in order, the simulation runner, the providers' pooled HTTP clients and
    the database engines. Each is independent; the ordering only reflects that the runner is
    the thing still producing work.
    """
    settings = get_settings()
    configure_logging(settings)
    _LOGGER.info("api.startup", **_settings_summary(settings))

    get_engine(settings)
    database_ok, _ = await asyncio.gather(check_database(), _warm_model())
    if database_ok:
        _LOGGER.info("api.startup.database_ready")
    else:
        _LOGGER.warning("api.startup.database_unavailable", hint="check `make db-up` and PostGIS")

    try:
        yield
    finally:
        await shutdown_runner()
        await close_providers()
        await dispose_engine()
        _LOGGER.info("api.shutdown")


def _middleware(settings: Settings) -> list[Middleware]:
    """The middleware stack, **outermost first**.

    Order is load-bearing:

    1. ``RequestIdMiddleware`` is outermost so every response — including one produced by a
       middleware below it — carries a correlation id.
    2. ``AccessLogMiddleware`` sits just inside it, so its log line and its metrics sample
       cover everything that follows, including CORS preflights and timeouts.
    3. ``CORSMiddleware`` next, so that error responses generated further in still receive
       their CORS headers; without that a browser reports "network error" for what is really a
       503, and the frontend's error state never renders.
    4. ``TimeoutMiddleware`` inside CORS, so a 504 is readable by the browser too.
    5. ``DataModeMiddleware`` innermost, because it must run *after* the handler has called
       ``set_data_mode``.
    """
    return [
        Middleware(RequestIdMiddleware),
        Middleware(AccessLogMiddleware),
        Middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            # Custom response headers are invisible to `fetch()` unless they are exposed. The
            # data-mode banner and request-id correlation in the web app both depend on this.
            expose_headers=[REQUEST_ID_HEADER, DATA_MODE_HEADER],
            max_age=600,
        ),
        Middleware(TimeoutMiddleware, timeout_s=max(settings.http_timeout_s * 3.0, 30.0)),
        Middleware(DataModeMiddleware),
    ]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the AutoTwin API application.

    Args:
        settings: Override the process settings — for tests that need a different data mode or
            CORS list. Defaults to the cached :func:`~autotwin_core.config.get_settings`.

    Returns:
        A fully wired :class:`~fastapi.FastAPI` instance: middleware, exception handlers and
        every router that exists at import time.
    """
    resolved = settings or get_settings()
    configure_logging(resolved)

    app = FastAPI(
        title="AutoTwin DE API",
        summary="Digital twin of German EV mobility — infrastructure, energy and simulation.",
        description=_DESCRIPTION,
        version=__version__,
        # Docs stay on in every environment: this deployment holds no secrets, the frontend
        # links to /docs from its own navigation, and an API whose contract cannot be read is
        # of no use to the engineers it is written for.
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_tags=OPENAPI_TAGS,
        servers=[{"url": "http://localhost:8000", "description": "Local development"}],
        contact={"name": "AutoTwin DE"},
        license_info={"name": "Apache-2.0", "identifier": "Apache-2.0"},
        lifespan=_lifespan,
        middleware=_middleware(resolved),
        debug=resolved.debug,
    )

    register_exception_handlers(app)

    mounted = [
        name
        for name, prefix, tag in _ROUTER_MOUNTS
        if _include_router(app, name, prefix=prefix, tag=tag)
    ]
    _LOGGER.info("api.routers.mounted", count=len(mounted), routers=mounted)

    return app
