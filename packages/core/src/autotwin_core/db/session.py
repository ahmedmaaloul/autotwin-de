"""Engine and session management for both the async and the synchronous halves of AutoTwin.

The API, the SSE stream and the streaming consumer are asyncio programs and use
:func:`get_engine` / :func:`get_session`. Alembic, the ingestion CLIs and the analytical tooling
are ordinary scripts and use :func:`get_sync_engine`. Both talk to PostgreSQL through psycopg 3,
so the two engines differ only in their execution model — not in their driver, their DSN or
their behaviour.

Engines are created lazily and kept per process. Creating one is expensive (it opens a
connection pool), and a module-level constant would open that pool at import time, which breaks
``alembic`` runs and unit tests that never touch a database.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy import Engine, create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from autotwin_core.config import Settings, get_settings
from autotwin_core.logging import get_logger

__all__ = [
    "check_database",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "get_sync_engine",
    "session_scope",
]

_LOGGER = get_logger(__name__)

_POOL_RECYCLE_SECONDS: int = 1800
"""Recycle connections after 30 minutes so a proxy or a restarted server cannot hand us a dead
socket — cheaper than discovering it inside a request."""

_lock = threading.RLock()
"""Guards the lazy singletons; both factories are callable from synchronous code.

Reentrant by necessity, not by preference: :func:`get_sessionmaker` builds the engine while
holding the lock, so ``get_engine()`` re-enters it on the same thread. With a plain
``threading.Lock`` that is a silent deadlock — any process whose first database call goes
through :func:`session_scope` / :func:`get_session` hangs forever with no error and no log line.
"""

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_sync_engine: Engine | None = None


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the process-wide asyncio engine, creating it on first use.

    ``pool_pre_ping`` costs one round trip per checkout and buys immunity to the most common
    production failure of a long-lived pool: the database restarted, or a firewall dropped an
    idle connection, and the next query fails for reasons that have nothing to do with the
    query.
    """
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                config = settings or get_settings()
                _engine = create_async_engine(
                    config.database_url_async,
                    echo=config.debug,
                    pool_size=config.db_pool_size,
                    max_overflow=config.db_pool_size,
                    pool_pre_ping=True,
                    pool_recycle=_POOL_RECYCLE_SECONDS,
                )
                _LOGGER.debug("db.engine.created", pool_size=config.db_pool_size, mode="async")
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide session factory.

    ``expire_on_commit=False`` is essential rather than cosmetic: FastAPI serialises ORM objects
    *after* the request handler has committed, and the default would expire every attribute and
    emit a fresh SELECT — or fail outright — during response rendering.
    """
    global _sessionmaker
    if _sessionmaker is None:
        with _lock:
            if _sessionmaker is None:
                _sessionmaker = async_sessionmaker(
                    bind=get_engine(),
                    class_=AsyncSession,
                    expire_on_commit=False,
                    autoflush=False,
                )
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Run a unit of work in a transaction: commit on success, roll back on any exception.

    For pipelines, background tasks and scripts — anything that owns its transaction boundary.
    HTTP handlers use :func:`get_session` instead, because a web framework's error handling has
    to run *after* the rollback, not inside it.
    """
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session for the duration of one request.

    Deliberately does **not** commit: read endpoints should not open a write transaction, and a
    write endpoint states its own commit point so that "what got persisted" is visible in the
    handler rather than implied by the dependency. A failed request is always rolled back.
    """
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def get_sync_engine(settings: Settings | None = None) -> Engine:
    """Return the process-wide synchronous engine — Alembic, scripts, analytical tooling.

    Kept separate from the async engine rather than derived from it: Alembic drives DDL on a
    plain connection, and mixing that into the API's request pool would let a long migration
    starve request handling.
    """
    global _sync_engine
    if _sync_engine is None:
        with _lock:
            if _sync_engine is None:
                config = settings or get_settings()
                _sync_engine = create_engine(
                    config.database_url_sync,
                    echo=config.debug,
                    pool_size=config.db_pool_size,
                    max_overflow=config.db_pool_size,
                    pool_pre_ping=True,
                    pool_recycle=_POOL_RECYCLE_SECONDS,
                )
                _LOGGER.debug("db.engine.created", pool_size=config.db_pool_size, mode="sync")
    return _sync_engine


async def dispose_engine() -> None:
    """Close every pooled connection and forget the engines.

    Called from the API's ``lifespan`` shutdown and from test teardown. Disposing both engines
    here keeps the reset in one place — a test that leaves a synchronous pool open against a
    torn-down database container fails in a way that points nowhere near the cause.
    """
    global _engine, _sessionmaker, _sync_engine
    with _lock:
        engine, sync_engine = _engine, _sync_engine
        _engine = None
        _sessionmaker = None
        _sync_engine = None
    if engine is not None:
        await engine.dispose()
    if sync_engine is not None:
        sync_engine.dispose()
    _LOGGER.debug("db.engine.disposed")


async def check_database(timeout_s: float = 5.0) -> bool:
    """Readiness probe for ``GET /ready``: is PostgreSQL reachable *and* PostGIS installed?

    Checking the extension as well as the connection is what makes this probe meaningful here.
    A database that answers ``SELECT 1`` but has no PostGIS will fail every spatial query in the
    application — corridor coverage, gap analysis, the whole map — so reporting it as ready
    would only move the failure somewhere harder to diagnose.

    Never raises: a readiness endpoint that returns 500 tells an orchestrator far less than one
    that returns ``ready: false``.
    """
    statement = sa.text("SELECT extname FROM pg_extension WHERE extname = 'postgis'")
    try:
        async with asyncio.timeout(timeout_s):
            async with get_engine().connect() as connection:
                result = await connection.execute(statement)
                if result.scalar_one_or_none() is None:
                    _LOGGER.warning("db.check.postgis_missing")
                    return False
                return True
    except (TimeoutError, sa.exc.SQLAlchemyError, OSError) as exc:
        _LOGGER.warning("db.check.failed", error=type(exc).__name__, detail=str(exc))
        return False
