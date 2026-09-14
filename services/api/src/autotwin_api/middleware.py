"""ASGI middleware: correlation ids, access logging, data-mode honesty and a timeout guard.

All four are written as **pure ASGI middleware** rather than
``starlette.middleware.base.BaseHTTPMiddleware``. That is not a stylistic preference:
``BaseHTTPMiddleware`` runs the downstream application in a separate anyio task, so a
:class:`~contextvars.ContextVar` written by a route handler is invisible to the middleware once
``call_next`` returns. The entire point of :func:`set_data_mode` is that a handler writes a
value which the middleware then turns into a response header, so that layering would break the
feature it exists for.

For the same reason the per-request state is a *mutable object* reached through a contextvar,
not a contextvar holding an immutable value: the object is also stashed on the ASGI scope, so
the exception handlers (which run in a different middleware layer) can still recover the
request id after the context has been torn down.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autotwin_api.metrics import UNMATCHED_PATH, observe_request, route_template
from autotwin_contracts import DATA_MODE_HEADER, REQUEST_ID_HEADER, ErrorResponse, ProviderMode
from autotwin_core.logging import bind_request_id, clear_request_id, get_logger

__all__ = [
    "REQUEST_CONTEXT_SCOPE_KEY",
    "AccessLogMiddleware",
    "DataModeMiddleware",
    "RequestContext",
    "RequestIdMiddleware",
    "TimeoutMiddleware",
    "current_request_context",
    "get_data_mode",
    "request_context_of",
    "set_data_mode",
]

_LOGGER = get_logger(__name__)

REQUEST_CONTEXT_SCOPE_KEY: Final[str] = "autotwin.request_context"
"""ASGI scope key under which the per-request state is published.

Custom scope keys are the ASGI-native way to pass state between middleware layers, and unlike
a contextvar the scope is still reachable from a handler that only received a ``Request``.
"""

_QUIET_PATHS: Final[frozenset[str]] = frozenset({"/health", "/ready", "/metrics"})
"""Probe endpoints excluded from the access log.

Kubernetes and Prometheus poll these every few seconds; logging them would bury the request
that someone is actually trying to debug. They are still counted in the metrics, because a
readiness probe that starts failing is exactly the sort of thing a dashboard should show.
"""

_MAX_REQUEST_ID_LENGTH: Final[int] = 128
"""Upper bound on an id adopted from a client - it is echoed into a header and into the logs."""

_MODE_SEVERITY: Final[dict[ProviderMode, int]] = {
    ProviderMode.live: 0,
    ProviderMode.cache: 1,
    ProviderMode.fixture: 2,
}
"""How far each mode is from the truth "this is fresh data from the German source"."""


@dataclass(slots=True)
class RequestContext:
    """Mutable state that lives for exactly one HTTP request.

    Shared by reference between the middleware stack and the route handler, which is what lets
    a handler deep inside a provider call influence a response header it never sees.
    """

    request_id: str
    """Correlation id echoed in ``X-Request-ID`` and in every log line of this request."""

    started_at: float = field(default_factory=time.perf_counter)
    """Monotonic start stamp; the access log turns it into ``duration_ms``."""

    data_mode: ProviderMode | None = None
    """Worst provider mode reported while answering, or ``None`` if no provider was consulted."""

    def record_data_mode(self, mode: ProviderMode) -> None:
        """Merge ``mode`` into the request's data mode, keeping the *least* fresh one.

        A route analysis consults routing, weather and traffic. If two answered live and one
        fell back to a fixture, the honest header is ``fixture`` — claiming ``live`` because
        the majority was live is exactly the invisible mixing BUILD_SPEC §0.2 forbids.
        """
        current = self.data_mode
        if current is None or _MODE_SEVERITY[mode] > _MODE_SEVERITY[current]:
            self.data_mode = mode


_REQUEST_CONTEXT: ContextVar[RequestContext | None] = ContextVar(
    "autotwin_request_context",
    default=None,
)


def current_request_context() -> RequestContext | None:
    """The context of the request being handled here, or ``None`` outside a request."""
    return _REQUEST_CONTEXT.get()


def request_context_of(scope: Scope) -> RequestContext | None:
    """Recover the context from an ASGI scope — the path that survives context teardown."""
    context = scope.get(REQUEST_CONTEXT_SCOPE_KEY)
    return context if isinstance(context, RequestContext) else None


def set_data_mode(mode: ProviderMode) -> None:
    """Declare how the data in this response was obtained (BUILD_SPEC §4, §7).

    Every handler that consults a provider calls this with ``result.mode``; the value surfaces
    as the ``X-AutoTwin-Data-Mode`` response header, which is what makes the frontend's
    *"Live-Quelle nicht verfügbar"* banner possible. Calling it several times keeps the least
    fresh mode (see :meth:`RequestContext.record_data_mode`). Outside a request — in a test, a
    CLI, a background task — it is a no-op rather than an error, so provider-calling helpers
    stay reusable off the HTTP path.
    """
    context = _REQUEST_CONTEXT.get()
    if context is not None:
        context.record_data_mode(mode)


def get_data_mode() -> ProviderMode | None:
    """The data mode recorded so far for this request, if any."""
    context = _REQUEST_CONTEXT.get()
    return context.data_mode if context is not None else None


def _append_header(message: Message, name: str, value: str) -> None:
    """Add a header to an ``http.response.start`` message unless it is already set.

    "Unless already set" matters for ``X-Request-ID``: the exception handlers put it on their
    responses directly, because the generic 500 handler runs *above* this middleware and its
    response never passes through the send wrapper below.
    """
    headers: list[tuple[bytes, bytes]] = message.setdefault("headers", [])
    encoded = name.lower().encode("latin-1")
    if any(existing == encoded for existing, _ in headers):
        return
    headers.append((encoded, value.encode("latin-1")))


def _new_request_id() -> str:
    """Mint a correlation id: a bare uuid4 hex, short enough to paste into a support ticket."""
    return uuid.uuid4().hex


def _client_request_id(scope: Scope) -> str | None:
    """Read an inbound ``X-Request-ID``, if the caller supplied a sane one.

    Honouring the client's id is what makes a trace span the web app and the API. It is length
    limited and stripped of non-printable characters, because the value is echoed into a
    response header and into the logs: an unbounded or newline-carrying value from an untrusted
    caller is a header-injection and log-forging vector.
    """
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    wanted = REQUEST_ID_HEADER.lower().encode("latin-1")
    for name, value in headers:
        if name == wanted:
            candidate = value.decode("latin-1", errors="replace").strip()
            if candidate and len(candidate) <= _MAX_REQUEST_ID_LENGTH and candidate.isprintable():
                return candidate
            return None
    return None


class RequestIdMiddleware:
    """Establish the per-request context, bind it to the logger, echo it as a header.

    Outermost of AutoTwin's own middleware, so that every response — including one produced by
    a middleware below it — carries a correlation id.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Mint or adopt a request id and make it visible to logs, scope and response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        context = RequestContext(request_id=_client_request_id(scope) or _new_request_id())
        scope[REQUEST_CONTEXT_SCOPE_KEY] = context
        token = _REQUEST_CONTEXT.set(context)
        bind_request_id(context.request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                _append_header(message, REQUEST_ID_HEADER, context.request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            clear_request_id()
            _REQUEST_CONTEXT.reset(token)


class DataModeMiddleware:
    """Turn whatever :func:`set_data_mode` recorded into the ``X-AutoTwin-Data-Mode`` header.

    Innermost of AutoTwin's middleware, so the handler has already run — and already called
    :func:`set_data_mode` — by the time ``http.response.start`` passes through here.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Copy the recorded provider mode onto the outgoing response headers."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        context = request_context_of(scope)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and context is not None:
                mode = context.data_mode
                if mode is not None:
                    _append_header(message, DATA_MODE_HEADER, mode.value)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class AccessLogMiddleware:
    """One structured log line and one metrics observation per request.

    Logging and instrumentation share a layer on purpose: both need the response status, the
    elapsed time and the matched route template, and measuring the same request twice in two
    middlewares produces two slightly different durations for one event.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Time the request, record the outcome, and re-raise anything that went wrong."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # The generic exception handler above us will turn this into a 500; record the
            # request here anyway, because a request that never reaches the send wrapper is
            # otherwise missing from both the log and the metrics.
            self._record(scope, status=500, started=started)
            raise
        else:
            self._record(scope, status=status_holder["status"], started=started)

    def _record(self, scope: Scope, *, status: int, started: float) -> None:
        """Emit the metrics sample always, and the log line for everything but the probes."""
        duration_s = time.perf_counter() - started
        method = str(scope.get("method", "GET"))
        template = route_template(scope)
        observe_request(method=method, path=template, status=status, duration_s=duration_s)

        raw_path = str(scope.get("path", ""))
        if raw_path in _QUIET_PATHS:
            return
        context = request_context_of(scope)
        _LOGGER.info(
            "api.request",
            method=method,
            path=raw_path,
            route=template if template != UNMATCHED_PATH else None,
            status=status,
            duration_ms=round(duration_s * 1000.0, 3),
            request_id=context.request_id if context is not None else None,
        )


class TimeoutMiddleware:
    """Refuse to hold a connection open forever when something downstream is wedged.

    The budget covers the time until the response *starts*, not its whole lifetime: the SSE
    telemetry stream of BUILD_SPEC §8 is supposed to stay open for minutes, and a naive
    whole-request timeout would sever it. Once headers are on the wire the deadline is
    cancelled, so a slow *body* is fine while a slow *upstream* is not.
    """

    def __init__(self, app: ASGIApp, *, timeout_s: float) -> None:
        """Wrap ``app``, answering 504 if the handler has not started responding in time."""
        self.app = app
        self.timeout_s = timeout_s

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Run the request under a deadline that is lifted as soon as it starts responding."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_response = False

        try:
            async with asyncio.timeout(self.timeout_s) as deadline:

                async def send_wrapper(message: Message) -> None:
                    nonlocal started_response
                    if message["type"] == "http.response.start":
                        started_response = True
                        deadline.reschedule(None)
                    await send(message)

                await self.app(scope, receive, send_wrapper)
        except TimeoutError:
            if started_response:
                # Headers are already out; there is no way to turn this into a 504 and
                # re-raising would only corrupt the response body.
                raise
            await self._send_timeout(scope, send)

    async def _send_timeout(self, scope: Scope, send: Send) -> None:
        """Answer with the BUILD_SPEC §7 error envelope at status 504."""
        context = request_context_of(scope)
        request_id = context.request_id if context is not None else None
        _LOGGER.warning(
            "api.request.timeout",
            path=str(scope.get("path", "")),
            method=str(scope.get("method", "GET")),
            timeout_s=self.timeout_s,
            request_id=request_id,
        )
        body = (
            ErrorResponse.of(
                "request_timeout",
                f"The request exceeded the server's {self.timeout_s:.0f}s processing budget",
                details={"timeout_s": self.timeout_s},
                request_id=request_id,
            )
            .model_dump_json()
            .encode("utf-8")
        )
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin-1")),
        ]
        if request_id is not None:
            headers.append((REQUEST_ID_HEADER.lower().encode("latin-1"), request_id.encode()))
        await send({"type": "http.response.start", "status": 504, "headers": headers})
        await send({"type": "http.response.body", "body": body})
