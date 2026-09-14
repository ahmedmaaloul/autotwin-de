"""Structured logging for every AutoTwin process.

One log configuration serves the API, the ingestion pipelines, the simulator and the streaming
consumer, because they all end up in the same terminal during ``make demo`` and in the same
stdout stream inside Compose. The setup therefore has to satisfy two readers at once:

* a human, who wants aligned colour output while running the stack locally, and
* ``docker logs`` / a log shipper, which wants one JSON object per line.

Both are the same event stream with a different renderer, chosen by
:attr:`~autotwin_core.config.Settings.log_format`.

Third-party libraries (uvicorn, SQLAlchemy, httpx, aiokafka) log through the standard library,
so ``logging``'s root handler is routed through the very same structlog renderer. Without that,
half the lines in a production log would be JSON and half would be ``%s``-formatted prose.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Final

import structlog

from autotwin_core.config import LogFormat, Settings

__all__ = [
    "bind_request_id",
    "clear_request_id",
    "configure_logging",
    "get_logger",
    "get_request_id",
    "log_duration",
]

REQUEST_ID_KEY: Final[str] = "request_id"
"""Log-record key carrying the correlation id; mirrored into the ``X-Request-ID`` header."""

_REQUEST_ID: ContextVar[str | None] = ContextVar("autotwin_request_id", default=None)
"""Correlation id of the request/task currently being handled by this coroutine or thread."""

_NOISY_LOGGERS: Final[tuple[str, ...]] = (
    "httpx",
    "httpcore",
    "asyncio",
    "aiokafka",
    "urllib3",
)
"""Libraries whose INFO stream is per-request chatter that drowns AutoTwin's own events."""


def bind_request_id(request_id: str) -> None:
    """Attach ``request_id`` to every log record emitted from here on in this context.

    Called by the API's middleware once per request. Because the value lives in a
    :class:`~contextvars.ContextVar`, concurrent requests on the same event loop keep their own
    id without any explicit plumbing through call signatures — which is what makes it possible
    to correlate a provider timeout deep in an ingestion helper with the HTTP call that caused
    it.
    """
    _REQUEST_ID.set(request_id)
    structlog.contextvars.bind_contextvars(**{REQUEST_ID_KEY: request_id})


def clear_request_id() -> None:
    """Forget the current correlation id — called when a request finishes."""
    _REQUEST_ID.set(None)
    structlog.contextvars.unbind_contextvars(REQUEST_ID_KEY)


def get_request_id() -> str | None:
    """The correlation id of the current context, if one was bound.

    The API's exception handlers read it to fill ``ErrorDetail.request_id``, so a user can
    quote the id from an error response and have it found in the logs.
    """
    return _REQUEST_ID.get()


def _add_request_id(
    _logger: object,
    _method_name: str,
    event_dict: structlog.typing.EventDict,
) -> structlog.typing.EventDict:
    """Processor that injects the context-local request id into each record.

    Records already carrying the key — bound explicitly by a caller — are left alone.
    """
    if REQUEST_ID_KEY not in event_dict:
        request_id = _REQUEST_ID.get()
        if request_id is not None:
            event_dict[REQUEST_ID_KEY] = request_id
    return event_dict


def _shared_processors() -> list[structlog.typing.Processor]:
    """Processors applied to AutoTwin events *and* to records from third-party libraries."""
    return [
        structlog.contextvars.merge_contextvars,
        _add_request_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]


def _renderer(log_format: LogFormat) -> structlog.typing.Processor:
    """Pick the final processor: machine-readable JSON or human-readable console output."""
    if log_format is LogFormat.console:
        return structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    return structlog.processors.JSONRenderer()


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the standard library root logger from ``settings``.

    Idempotent: the root handler list is replaced rather than appended to, so calling this
    again — uvicorn's reloader does, and so does every test that builds a fresh ``Settings`` —
    cannot produce duplicated lines.

    Logs go to **stderr** so that a pipeline writing machine-readable output to stdout (the
    ingestion CLIs do) stays parseable while still logging.
    """
    level = settings.log_level_number
    shared = _shared_processors()
    final_processors: list[structlog.typing.Processor] = []
    if settings.log_format is LogFormat.json:
        # ConsoleRenderer formats exceptions itself; the JSON renderer needs them flattened
        # into a string field first, or the traceback is lost.
        final_processors.append(structlog.processors.format_exc_info)
    final_processors.append(_renderer(settings.log_format))

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # Caching binds the resolved level into the logger; in debug sessions the level is
        # changed at runtime often enough that the speed-up is not worth the confusion.
        cache_logger_on_first_use=not settings.debug,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *final_processors,
        ],
    )
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    # SQLAlchemy echoes every statement at INFO when echo=True; keep that under DEBUG control
    # so that ``AUTOTWIN_DEBUG=true`` is the single switch for "show me the SQL".
    sql_level = logging.INFO if settings.debug else logging.WARNING
    logging.getLogger("sqlalchemy.engine").setLevel(sql_level)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for ``name`` — conventionally the module's ``__name__``.

    Safe to call at import time and before :func:`configure_logging`: structlog resolves its
    configuration lazily on the first log call.
    """
    return structlog.stdlib.get_logger(name)


@contextmanager
def log_duration(
    logger: structlog.stdlib.BoundLogger,
    event: str,
    **initial: Any,
) -> Iterator[dict[str, Any]]:
    """Time a block and log it once, with ``duration_ms`` attached.

    Used by the ingestion pipelines and the ML training loop, where "how long did the
    Bundesnetzagentur download take, and how many rows came out of it?" is the question asked
    of the logs most often. The yielded dictionary is a scratchpad: mutate it inside the block
    to add outcome fields (row counts, provider mode, bytes) that are only known at the end.

    A failing block still logs — at ``error`` level, with the elapsed time and the exception —
    and the exception then propagates untouched.

        with log_duration(log, "ingest.bnetza") as ctx:
            rows = await pipeline.run()
            ctx["rows_accepted"] = len(rows)
    """
    context: dict[str, Any] = dict(initial)
    started = time.perf_counter()
    try:
        yield context
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.error(
            event,
            duration_ms=round(elapsed_ms, 3),
            outcome="error",
            error=type(exc).__name__,
            **context,
        )
        raise
    else:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        logger.info(event, duration_ms=round(elapsed_ms, 3), outcome="ok", **context)
