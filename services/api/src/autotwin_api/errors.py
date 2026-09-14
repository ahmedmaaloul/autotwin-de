"""Exception handlers — every 4xx and 5xx leaves this service in the same envelope.

BUILD_SPEC §7 fixes one body for all failures::

    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}

FastAPI's own defaults do not produce that shape: a ``HTTPException`` renders as
``{"detail": ...}`` and a validation failure as a bare list. Overriding all four entry points
— our typed errors, request validation, Starlette's ``HTTPException`` and everything
unforeseen — means the frontend has exactly one error path and can switch on ``code`` instead
of parsing prose.

The handlers put ``X-Request-ID`` on their responses themselves rather than relying on
:class:`~autotwin_api.middleware.RequestIdMiddleware`. The generic 500 handler runs in
Starlette's ``ServerErrorMiddleware``, which sits *above* every user middleware, so its
response never passes through that send wrapper — and the correlation id matters most on
precisely that response.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any, Final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from autotwin_api.middleware import request_context_of
from autotwin_contracts import ErrorCode, ErrorResponse
from autotwin_contracts.api import REQUEST_ID_HEADER
from autotwin_core.errors import AutoTwinError
from autotwin_core.logging import get_logger, get_request_id

__all__ = [
    "ERROR_RESPONSES",
    "register_exception_handlers",
]

_LOGGER = get_logger(__name__)

_STATUS_CODES: Final[dict[int, str]] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: ErrorCode.not_found.value,
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: ErrorCode.validation_error.value,
    429: ErrorCode.rate_limited.value,
    500: ErrorCode.internal_error.value,
    502: ErrorCode.invalid_source_data.value,
    503: ErrorCode.provider_unavailable.value,
    504: ErrorCode.provider_timeout.value,
}
"""Stable codes for the HTTP statuses Starlette raises on its own behalf (404, 405, …).

Without this table a 404 from the router would answer with a different code from a 404 raised
by :class:`~autotwin_core.errors.NotFoundError`, and the frontend would need two branches for
one condition.
"""

ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "Request failed validation."},
    500: {"model": ErrorResponse, "description": "Unhandled server error."},
    503: {"model": ErrorResponse, "description": "A dependency is unavailable."},
}
"""Default ``responses=`` for routers, so the OpenAPI schema documents the error envelope.

Without it ``openapi-typescript`` generates ``unknown`` for every failure body and the web app
loses the typed ``error.code`` it is supposed to switch on.
"""


def _request_id(request: Request) -> str | None:
    """Best available correlation id for this request.

    The ASGI scope is consulted first because it survives the teardown of the context
    variables, which has already happened by the time the outermost 500 handler runs.
    """
    context = request_context_of(request.scope)
    if context is not None:
        return context.request_id
    return get_request_id()


def _envelope(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Render the BUILD_SPEC §7 envelope as a JSON response carrying the correlation id."""
    request_id = _request_id(request)
    payload = ErrorResponse.of(code, message, details=details, request_id=request_id)
    response_headers = dict(headers) if headers else {}
    if request_id is not None:
        response_headers.setdefault(REQUEST_ID_HEADER, request_id)
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
        headers=response_headers,
    )


async def _handle_autotwin_error(request: Request, exc: Exception) -> Response:
    """Map a typed AutoTwin error onto its own status and code.

    One handler covers the whole hierarchy — ``NotFoundError``, every ``ProviderError``
    subclass, ``ModelNotAvailable`` — because each class already declares ``code`` and
    ``http_status``. Adding an error type therefore never means editing this module.
    """
    if not isinstance(exc, AutoTwinError):  # pragma: no cover - registration guarantees the type
        raise exc
    log = _LOGGER.error if exc.http_status >= HTTPStatus.INTERNAL_SERVER_ERROR else _LOGGER.warning
    log(
        "api.error",
        code=exc.code,
        status=exc.http_status,
        path=request.url.path,
        detail=exc.message,
    )
    return _envelope(
        request,
        status_code=exc.http_status,
        code=exc.code,
        message=exc.message,
        details=dict(exc.details) or None,
    )


async def _handle_request_validation_error(request: Request, exc: Exception) -> Response:
    """Turn FastAPI's validation failure into ``422 validation_error`` with field detail.

    The per-field list is flattened to plain strings: Pydantic's raw ``errors()`` can carry a
    live exception object in ``ctx``, which is not JSON-serialisable and would turn a client's
    typo into a 500.
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registration guarantees
        raise exc
    fields = [
        {
            "loc": ".".join(str(part) for part in error.get("loc", ())),
            "message": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in exc.errors()
    ]
    _LOGGER.info("api.validation_error", path=request.url.path, errors=len(fields))
    return _envelope(
        request,
        status_code=422,
        code=ErrorCode.validation_error.value,
        message="Request validation failed",
        details={"errors": fields},
    )


async def _handle_http_exception(request: Request, exc: Exception) -> Response:
    """Re-dress Starlette's ``{"detail": ...}`` as the project envelope.

    ``exc.headers`` is preserved because dropping it would silently break the responses that
    depend on it — a 405 without ``Allow`` and a 401 without ``WWW-Authenticate`` are both
    protocol violations.
    """
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - registration guarantees
        raise exc
    status = exc.status_code
    code = _STATUS_CODES.get(status, "http_error")
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else HTTPStatus(status).phrase
    details = None if isinstance(exc.detail, str) or exc.detail is None else {"detail": exc.detail}
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        _LOGGER.error("api.http_error", status=status, path=request.url.path, detail=detail)
    return _envelope(
        request,
        status_code=status,
        code=code,
        message=detail,
        details=details,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """Last resort: log the traceback, tell the client nothing but the correlation id.

    The message is deliberately generic. A stack trace or a psycopg error string in a response
    body leaks table names, file paths and query fragments; the request id is what lets an
    operator find the full traceback in the logs instead.
    """
    _LOGGER.exception(
        "api.unhandled_error",
        path=request.url.path,
        method=request.method,
        error=type(exc).__name__,
    )
    return _envelope(
        request,
        status_code=500,
        code=ErrorCode.internal_error.value,
        message="An unexpected error occurred. Quote the request id when reporting it.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler on ``app``. Called once, from ``create_app``."""
    app.add_exception_handler(AutoTwinError, _handle_autotwin_error)
    app.add_exception_handler(RequestValidationError, _handle_request_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_error)
