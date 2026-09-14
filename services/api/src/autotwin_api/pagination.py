"""Offset pagination for every list endpoint (BUILD_SPEC §7).

The envelope and the parameter bounds are defined once, in
:mod:`autotwin_contracts.api`, because the frontend's ``Page<T>`` is generated from them. What
lives here is the *execution*: turning a SQLAlchemy ``Select`` into that envelope with one
``COUNT`` and one windowed ``SELECT``.

Doing it centrally is not ceremony. ``has_next`` computed by hand in fourteen routers is
fourteen chances to report a further page that returns nothing, and a ``COUNT`` that forgets to
drop the ``ORDER BY`` makes PostgreSQL sort a result set it is about to throw away.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import Query
from sqlalchemy import Row, Select
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_contracts import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Page, PaginationParams

__all__ = [
    "count_rows",
    "paginate",
    "paginate_rows",
    "pagination_params",
]


def pagination_params(
    page: Annotated[
        int,
        Query(ge=1, description="1-based page index.", examples=[1]),
    ] = 1,
    page_size: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PAGE_SIZE,
            description=f"Rows per page (1-{MAX_PAGE_SIZE}).",
            examples=[DEFAULT_PAGE_SIZE],
        ),
    ] = DEFAULT_PAGE_SIZE,
) -> PaginationParams:
    """FastAPI dependency exposing ``?page=&page_size=`` with the bounds of BUILD_SPEC §7.

    Written as an explicit function rather than a Pydantic query-model so that the two
    parameters appear in the OpenAPI schema with their own descriptions and examples — the
    frontend reads those at the call site, in the generated types.
    """
    return PaginationParams(page=page, page_size=page_size)


async def count_rows(session: AsyncSession, statement: Select[Any]) -> int:
    """Count the rows ``statement`` would return, ignoring its ordering.

    ``ORDER BY`` is stripped before wrapping the query: sorting rows that are only going to be
    counted is pure cost, and on a large corridor query it is the difference between an index
    scan and a sort node. ``statement`` must not already carry ``LIMIT``/``OFFSET`` — the
    window is applied by :func:`paginate`, after this call.
    """
    count_statement = sa.select(sa.func.count()).select_from(statement.order_by(None).subquery())
    return int((await session.execute(count_statement)).scalar_one())


async def paginate[RowT, ItemT](
    session: AsyncSession,
    statement: Select[tuple[RowT]],
    params: PaginationParams,
    mapper: Callable[[RowT], ItemT],
) -> Page[ItemT]:
    """Run ``statement`` as one page and map each entity through ``mapper``.

    For the ordinary case of a single-entity select — ``select(ChargingStation).where(...)``.
    Use :func:`paginate_rows` when the statement selects several columns.

    An empty result skips the windowed query entirely: a filter that matches nothing is common
    (a Bundesland with no ultra-fast chargers, a vehicle with no trips) and a second round trip
    to fetch zero rows buys nothing.

    Args:
        session: Session to execute on; no transaction is committed here.
        statement: The filtered, ordered query **without** ``LIMIT``/``OFFSET``.
        params: Page and page size, from :func:`pagination_params`.
        mapper: Converts one ORM entity into its response model.
    """
    total = await count_rows(session, statement)
    if total == 0:
        return Page.empty(page=params.page, page_size=params.page_size)

    windowed = statement.limit(params.limit).offset(params.offset)
    rows = (await session.execute(windowed)).scalars().all()
    return Page.build(
        [mapper(row) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


async def paginate_rows[ItemT](
    session: AsyncSession,
    statement: Select[Any],
    params: PaginationParams,
    mapper: Callable[[Row[Any]], ItemT],
) -> Page[ItemT]:
    """Page a multi-column select, handing each :class:`~sqlalchemy.Row` to ``mapper``.

    Separate from :func:`paginate` because ``Result.scalars()`` silently keeps only the first
    column: aggregate queries such as ``select(Station.operator, func.count())`` would lose
    their counts without a word if they went through the single-entity path.
    """
    total = await count_rows(session, statement)
    if total == 0:
        return Page.empty(page=params.page, page_size=params.page_size)

    windowed = statement.limit(params.limit).offset(params.offset)
    rows = (await session.execute(windowed)).all()
    return Page.build(
        [mapper(row) for row in rows],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )
