"""Helpers for driving the API in-process, with no server and no socket.

``httpx.ASGITransport`` speaks ASGI straight to the application object, so a request goes
through the real middleware stack, the real routers and the real exception handlers without a
port being opened. Two consequences are worth knowing, because tests here depend on both:

* **The lifespan does not run.** ``ASGITransport`` sends no ``lifespan`` events, so no engine is
  warmed and no model is loaded when the app is built. That is what lets ``/health`` be tested
  with no database in the room at all.
* **Application exceptions are re-raised by default.** Starlette's ``ServerErrorMiddleware``
  sends its 500 response *and* re-raises, so the server's own logs carry the traceback. A test
  that wants to inspect the 500 body rather than the exception has to ask for
  ``raise_app_exceptions=False`` — hence the flag on :func:`api_client`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from autotwin_api.app import create_app

__all__ = ["api_client", "build_app"]


def build_app() -> FastAPI:
    """The real application, built from the fixture-mode settings the suite forces."""
    return create_app()


@asynccontextmanager
async def api_client(
    app: FastAPI | None = None,
    *,
    raise_app_exceptions: bool = True,
) -> AsyncIterator[httpx.AsyncClient]:
    """An ``AsyncClient`` wired straight to the ASGI app.

    Args:
        app: The application to drive; a fresh one by default.
        raise_app_exceptions: Leave ``True`` so an unexpected exception fails the test loudly.
            Set ``False`` only when the response body of a 500 is the thing under test.
    """
    transport = httpx.ASGITransport(
        app=app if app is not None else build_app(),
        raise_app_exceptions=raise_app_exceptions,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
