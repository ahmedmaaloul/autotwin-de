"""Prometheus instrumentation for the API process (BUILD_SPEC §7 ``GET /metrics``).

Three decisions are worth stating, because each of them is the kind of thing that is painful
to change once a Grafana dashboard queries it:

**A dedicated registry, not the global default.** ``prometheus_client.REGISTRY`` is module
state shared with every library in the process, and registering the same metric name twice
raises. The API is imported by tests, by ``uvicorn --reload`` and by ``create_app()`` calls
that build several app instances in one interpreter; a private registry makes all of those
safe and keeps the scrape output to exactly the series this service promises.

**The names are the contract.** ``prometheus_client`` appends ``_total`` to every ``Counter``,
so ``Counter("ingestion_rows_processed")`` would be scraped as ``ingestion_rows_processed_total``
— a different series from the one the spec names. The two ingestion metrics are therefore
``Gauge`` objects used monotonically (``.inc()`` only): the sample name then matches the spec
exactly. The metrics that *do* end in ``_total`` in the spec are real ``Counter`` objects,
because ``prometheus_client`` strips the suffix from the constructor argument and puts it back
on the sample — so those names also come out exactly as written.

Each counter is additionally exposed with a ``_created`` companion series carrying its
creation timestamp. That is ``prometheus_client``'s standard counter metadata, not part of this
contract; suppressing it would mean calling an unannotated library function, which ``mypy
--strict`` rightly refuses.

**Labels carry the route template, never the raw path.** ``/api/v1/charging/stations/{id}``
is one series; ``/api/v1/charging/stations/<any of 116 440 uuids>`` would be 116 440 series and
would take the scrape target down with it.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate_latest
from starlette.types import Scope

__all__ = [
    "ACTIVE_SIMULATED_VEHICLES",
    "API_REQUESTS_TOTAL",
    "API_REQUEST_DURATION_SECONDS",
    "INGESTION_ROWS_PROCESSED",
    "INGESTION_ROWS_REJECTED",
    "REGISTRY",
    "TELEMETRY_EVENTS_FAILED_TOTAL",
    "TELEMETRY_EVENTS_RECEIVED_TOTAL",
    "UNMATCHED_PATH",
    "observe_request",
    "render_metrics",
    "route_template",
]

REGISTRY: Final[CollectorRegistry] = CollectorRegistry(auto_describe=True)
"""The API's own collector registry — see the module docstring for why it is not the default."""

UNMATCHED_PATH: Final[str] = "unmatched"
"""Label value for requests that matched no route: one series for every 404, not one each."""

_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)
"""Latency buckets spanning a cached dashboard tile (~5 ms) to a full route analysis (~seconds).

The default buckets stop at 10 s, which would collapse every slow upstream call into ``+Inf``
and make the p99 of ``POST /api/v1/routes/analyze`` unreadable exactly when it matters.
"""

API_REQUESTS_TOTAL: Final[Counter] = Counter(
    "api_requests_total",
    "HTTP requests handled, by method, route template and response status.",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

API_REQUEST_DURATION_SECONDS: Final[Histogram] = Histogram(
    "api_request_duration_seconds",
    "Wall-clock duration of HTTP request handling, by method and route template.",
    labelnames=("method", "path"),
    buckets=_DURATION_BUCKETS,
    registry=REGISTRY,
)

TELEMETRY_EVENTS_RECEIVED_TOTAL: Final[Counter] = Counter(
    "telemetry_events_received_total",
    "Telemetry events accepted from the simulator or the Kafka consumer.",
    registry=REGISTRY,
)

TELEMETRY_EVENTS_FAILED_TOTAL: Final[Counter] = Counter(
    "telemetry_events_failed_total",
    "Telemetry events that could not be validated or persisted.",
    registry=REGISTRY,
)

ACTIVE_SIMULATED_VEHICLES: Final[Gauge] = Gauge(
    "active_simulated_vehicles",
    "Vehicles currently being simulated by this process.",
    registry=REGISTRY,
)

INGESTION_ROWS_PROCESSED: Final[Gauge] = Gauge(
    "ingestion_rows_processed",
    "Rows accepted by ingestion pipelines since process start (monotonic).",
    registry=REGISTRY,
)

INGESTION_ROWS_REJECTED: Final[Gauge] = Gauge(
    "ingestion_rows_rejected",
    "Rows rejected by data-quality rules since process start (monotonic).",
    registry=REGISTRY,
)


def route_template(scope: Scope) -> str:
    """Return the matched route's *template* for use as a metric label.

    Starlette stores the matched route on the ASGI scope during routing, which is the only
    place the un-substituted path (``/api/v1/charging/stations/{station_id}``) is available.
    Anything that did not match a route — a 404, or a request rejected before routing — is
    folded into :data:`UNMATCHED_PATH` so that a crawler probing random URLs cannot create an
    unbounded number of time series.
    """
    route = scope.get("route")
    if route is None:
        return UNMATCHED_PATH
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return str(template) if template else UNMATCHED_PATH


def observe_request(*, method: str, path: str, status: int, duration_s: float) -> None:
    """Record one finished HTTP request.

    Called from :class:`~autotwin_api.middleware.AccessLogMiddleware`, which already measures
    the duration and resolves the route template — timing the request a second time here would
    only add a second, slightly different number for the same event.
    """
    API_REQUESTS_TOTAL.labels(method=method, path=path, status=str(status)).inc()
    API_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(duration_s)


def render_metrics() -> tuple[bytes, str]:
    """Serialise the registry into the Prometheus text exposition format.

    Returns:
        The encoded payload and the ``Content-Type`` it must be served with. Returning the
        content type rather than hardcoding it in the route keeps the endpoint correct if
        ``prometheus_client`` ever switches its default exposition format.
    """
    return _generate_latest(REGISTRY), CONTENT_TYPE_LATEST
