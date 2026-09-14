"""ASGI entry point: ``uvicorn autotwin_api.main:app``.

One module-level instance and nothing else. Uvicorn's reloader and its multi-worker mode both
import this path in a fresh interpreter per worker, so keeping the module free of any other
work means a worker start is exactly one ``create_app()`` and no surprises.
"""

from __future__ import annotations

from fastapi import FastAPI

from autotwin_api.app import create_app

__all__ = ["app"]

app: FastAPI = create_app()
