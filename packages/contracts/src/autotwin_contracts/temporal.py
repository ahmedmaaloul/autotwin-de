"""Timezone discipline for every contract model.

BUILD_SPEC §14 is categorical: *dates in code are ``datetime`` with ``tzinfo=UTC``, never
naive*. Enforcing that on the boundary types means the database layer, the event bus and the
API can all assume aware UTC timestamps without re-checking.

Naive input is *interpreted* as UTC rather than rejected. German open data regularly ships
timestamps without an offset (DWD hourly files, CSV exports), and failing the whole ingestion
run over a missing ``Z`` would violate the degrade-never-crash rule — the adapters convert
local time explicitly before handing values over, so anything still naive here is UTC by
construction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator

__all__ = ["UtcDatetime", "ensure_utc", "utc_now"]


def utc_now() -> datetime:
    """Current time as an aware UTC datetime — the only clock the contracts use."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime, attaching UTC to naive input."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
"""A ``datetime`` field that is always aware and always normalised to UTC."""
