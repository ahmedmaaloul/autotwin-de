"""Shared pytest fixtures.

Two decisions here shape every test in the suite:

1. **Settings are forced into fixture mode before anything imports them.** `get_settings()` is
   `lru_cache`d, so the first caller wins for the whole process. Doing this in a session-scoped
   autouse fixture guarantees no test ever reaches the network or a developer's real `.env`.

2. **Nothing here needs a database.** Tests that genuinely need PostGIS are marked
   `@pytest.mark.integration` and get their session from `db_session` below; everything else
   runs in milliseconds with no container.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _isolate_settings(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Point every setting at a throwaway directory and forbid live provider calls."""
    data_dir = tmp_path_factory.mktemp("autotwin-data")

    os.environ.update(
        {
            "AUTOTWIN_ENV": "ci",
            "AUTOTWIN_DATA_MODE": "fixture",
            "AUTOTWIN_DATA_DIR": str(data_dir),
            "AUTOTWIN_LOG_LEVEL": "WARNING",
            "AUTOTWIN_LOG_FORMAT": "console",
            "AUTOTWIN_KAFKA_ENABLED": "false",
            "AUTOTWIN_LLM_ENABLED": "false",
            # A deliberately unroutable host: if a provider ignores fixture mode and tries the
            # network, the test fails fast instead of hanging or hitting a real endpoint.
            "AUTOTWIN_OSRM_BASE_URL": "http://127.0.0.1:9",
            "AUTOTWIN_NOMINATIM_BASE_URL": "http://127.0.0.1:9",
            "AUTOTWIN_HTTP_TIMEOUT_S": "2",
        }
    )

    from autotwin_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def frozen_now() -> datetime:
    """A fixed instant, so assertions about freshness and ordering are deterministic."""
    return datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def fixtures_dir() -> Path:
    """The committed provider fixtures — real source bytes, trimmed."""
    return REPO_ROOT / "services" / "ingestion" / "src" / "autotwin_ingestion" / "fixtures"


@pytest.fixture
async def db_session() -> AsyncIterator[object]:
    """A transactional session for `@pytest.mark.integration` tests.

    Everything the test writes is rolled back, so integration tests can run repeatedly against
    a developer's local database without leaving rows behind.
    """
    from autotwin_core.db.session import get_sessionmaker

    maker = get_sessionmaker()
    async with maker() as session:
        transaction = await session.begin()
        try:
            yield session
        finally:
            await transaction.rollback()
            await session.close()
