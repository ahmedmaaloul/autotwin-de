"""DWD weather observations (BUILD_SPEC §7).

Two endpoints with deliberately different jobs. ``GET /api/v1/weather`` is a *filter*: give it a
box or a point-and-radius and it pages through everything that matches, newest first — the shape
a table or a map layer wants. ``GET /api/v1/weather/latest`` is a *lookup*: give it a point and
it answers with the one observation that describes the weather there, which is what the route
analysis and the dashboard need and what a paged list makes awkward.

Both read the database rather than calling the DWD adapter on the request path. That is not a
shortcut: the ingestion pipeline owns the download, the parsing and the quality report, and an
API that re-fetched per request would bypass all three and make the response time depend on
Offenbach. The freshness of what is served is still reported truthfully — the data mode comes
from the ingestion run that wrote the rows, downgraded to at best ``cache`` because nothing here
was fetched during this request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Query

from autotwin_api.deps import BoundingBoxQuery, DbSession, Pagination
from autotwin_api.middleware import set_data_mode
from autotwin_api.schemas.common import ProvenanceOut
from autotwin_api.schemas.weather import WeatherObservationOut
from autotwin_contracts import (
    BoundingBox,
    DataOrigin,
    Page,
    ProviderMode,
    SourceSystem,
    WeatherCondition,
    utc_now,
)
from autotwin_core.errors import NotFoundError, ValidationError

__all__ = ["router"]

router = APIRouter()

_SELECT_COLUMNS: Final[str] = """
    w.id, w.observed_at, w.temperature_c, w.precipitation_mm, w.wind_speed_ms, w.wind_gust_ms,
    w.humidity_percent, w.pressure_hpa, w.condition,
    w.source, w.source_identifier, w.source_url, w.source_timestamp, w.data_origin,
    w.ingestion_run_id, w.ingested_at, run.provider_mode,
    ws.name AS station_name,
    ST_Y(w.location::geometry) AS latitude,
    ST_X(w.location::geometry) AS longitude
"""
"""Every column both endpoints project. Written once so the two cannot drift apart."""

_FROM_CLAUSE: Final[str] = """
    FROM weather_observations w
    LEFT JOIN weather_stations ws ON ws.id = w.weather_station_id
    LEFT JOIN data_ingestion_runs run ON run.id = w.ingestion_run_id
"""

_LIST_COUNT_SQL: Final[str] = "SELECT count(*) {from_clause} {where}"
"""Count matching observations. ``{where}`` is assembled from fixed predicate fragments chosen
by :func:`_build_filters`; every client value travels as a bound parameter."""

_LIST_SQL: Final[str] = """
SELECT {columns}{distance}
{from_clause}
{where}
ORDER BY w.observed_at DESC, w.id
LIMIT :limit OFFSET :offset
"""
"""One page of observations, newest first."""

_LATEST_SQL: Final[str] = """
WITH point AS MATERIALIZED (
    SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography AS geom
),
latest AS MATERIALIZED (
    SELECT DISTINCT ON (COALESCE(w.weather_station_id, w.id)) {columns},
           w.location
    {from_clause}
    CROSS JOIN point p
    WHERE w.observed_at <= :at
      AND ST_DWithin(w.location, p.geom, :radius_m)
    ORDER BY COALESCE(w.weather_station_id, w.id), w.observed_at DESC
)
SELECT l.*, ST_Distance(l.location, p.geom) AS distance_m
FROM latest l CROSS JOIN point p
ORDER BY l.location <-> p.geom
LIMIT 1
"""
"""Freshest reading of the nearest station within a radius.

``DISTINCT ON`` collapses each station's history to its newest row *before* the nearest-neighbour
sort runs, so a stale reading from a closer station can never beat a fresh one slightly further
out. The only slots filled into this template are the two column lists above.
"""

_DEFAULT_RADIUS_KM: Final[float] = 50.0
"""Radius used when a point is given without one. Roughly the spacing of the DWD's automatic
station network, so a point almost anywhere in Germany finds at least one station."""


def _row_out(row: Any, *, distance_km: float | None = None) -> WeatherObservationOut:
    """Map one raw-SQL row onto the response model."""
    return WeatherObservationOut(
        id=row.id,
        observed_at=row.observed_at,
        latitude=float(row.latitude),
        longitude=float(row.longitude),
        station_name=row.station_name,
        temperature_c=_optional(row.temperature_c),
        precipitation_mm=_optional(row.precipitation_mm),
        wind_speed_ms=_optional(row.wind_speed_ms),
        wind_gust_ms=_optional(row.wind_gust_ms),
        humidity_percent=_optional(row.humidity_percent),
        pressure_hpa=_optional(row.pressure_hpa),
        condition=WeatherCondition(row.condition),
        distance_km=distance_km,
        provenance=ProvenanceOut(
            source=SourceSystem(row.source),
            source_identifier=row.source_identifier,
            source_url=row.source_url,
            source_timestamp=row.source_timestamp,
            data_origin=DataOrigin(row.data_origin),
            ingestion_run_id=row.ingestion_run_id,
            ingested_at=row.ingested_at,
        ),
    )


def _optional(value: Any) -> float | None:
    """Coerce a nullable numeric column to ``float | None``."""
    return None if value is None else float(value)


def _stored_mode(provider_mode: str | None) -> ProviderMode:
    """Freshness of a row read out of the database rather than fetched now.

    A stored row is at best a cached copy of a live answer; a row ingested from a fixture stays
    a fixture. See the module docstring for why nothing here reports ``live``.
    """
    if provider_mode == ProviderMode.fixture.value:
        return ProviderMode.fixture
    return ProviderMode.cache


def _report_mode(modes: list[ProviderMode]) -> None:
    """Publish the least fresh mode among the rows served, for ``X-AutoTwin-Data-Mode``."""
    if not modes:
        set_data_mode(ProviderMode.fixture)
        return
    severity = {ProviderMode.live: 0, ProviderMode.cache: 1, ProviderMode.fixture: 2}
    set_data_mode(max(modes, key=lambda mode: severity[mode]))


@router.get(
    "",
    response_model=Page[WeatherObservationOut],
    summary="List weather observations",
    description=(
        "DWD observations, newest first, filtered by area and time.\n\n"
        "* `bbox=west,south,east,north` — everything inside the box.\n"
        "* `lat` + `lon` (+ `radius_km`, default 50) — everything within the radius, and each "
        "row then carries its `distance_km` from that point.\n"
        "* `at` — only observations valid at or before this instant, so a past analysis can be "
        "reproduced.\n\n"
        "`bbox` and `lat`/`lon` are mutually exclusive; with neither, all of Germany is "
        "searched. Every measurement is nullable: the DWD publishes partial rows, and a missing "
        "wind speed is not a calm day."
    ),
)
async def list_observations(
    session: DbSession,
    page: Pagination,
    bbox: BoundingBoxQuery,
    latitude: Annotated[
        float | None,
        Query(
            alias="lat",
            ge=-90.0,
            le=90.0,
            description="Latitude of the point to search around, in WGS 84 degrees.",
            examples=[50.11],
        ),
    ] = None,
    longitude: Annotated[
        float | None,
        Query(
            alias="lon",
            ge=-180.0,
            le=180.0,
            description="Longitude of the point to search around, in WGS 84 degrees.",
            examples=[8.68],
        ),
    ] = None,
    radius_km: Annotated[
        float,
        Query(
            gt=0.0,
            le=500.0,
            description="Search radius around `lat`/`lon` in kilometres.",
        ),
    ] = _DEFAULT_RADIUS_KM,
    at: Annotated[
        datetime | None,
        Query(description="Only observations valid at or before this instant (UTC)."),
    ] = None,
) -> Page[WeatherObservationOut]:
    """Page through observations matching the filters, newest first."""
    filters, params = _build_filters(bbox=bbox, latitude=latitude, longitude=longitude, at=at)
    near_point = latitude is not None and longitude is not None
    if near_point:
        filters.append(
            "ST_DWithin(w.location, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, "
            ":radius_m)"
        )
        params["radius_m"] = radius_km * 1000.0

    where = " WHERE " + " AND ".join(filters) if filters else ""
    total = int(
        (
            await session.execute(
                sa.text(_LIST_COUNT_SQL.format(from_clause=_FROM_CLAUSE, where=where)),
                params,
            )
        ).scalar_one()
    )
    if total == 0:
        set_data_mode(ProviderMode.fixture)
        return Page.empty(page=page.page, page_size=page.page_size)

    distance = (
        ", ST_Distance(w.location, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography)"
        " AS distance_m"
        if near_point
        else ", NULL::double precision AS distance_m"
    )
    rows = (
        await session.execute(
            sa.text(
                _LIST_SQL.format(
                    columns=_SELECT_COLUMNS,
                    distance=distance,
                    from_clause=_FROM_CLAUSE,
                    where=where,
                )
            ),
            {**params, "limit": page.limit, "offset": page.offset},
        )
    ).all()

    _report_mode([_stored_mode(row.provider_mode) for row in rows])
    return Page.build(
        [
            _row_out(
                row,
                distance_km=None if row.distance_m is None else float(row.distance_m) / 1000.0,
            )
            for row in rows
        ],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get(
    "/latest",
    response_model=WeatherObservationOut,
    summary="Latest observation near a point",
    description=(
        "The single observation that best describes the weather at `lat`/`lon`: the freshest "
        "reading of the nearest station within `radius_km`.\n\n"
        "Nearest **station** rather than nearest **row** — each station's history is collapsed "
        "to its most recent reading first, so a stale row from a station 5 km away can never "
        "win over a fresh one 6 km away. Answers `404 not_found` when no station reported "
        "inside the radius, because inventing a temperature for an uncovered area is exactly "
        "the invisible fabrication BUILD_SPEC §0 forbids."
    ),
)
async def latest_observation(
    session: DbSession,
    latitude: Annotated[
        float,
        Query(
            alias="lat",
            ge=-90.0,
            le=90.0,
            description="Latitude in WGS 84 decimal degrees.",
            examples=[50.11],
        ),
    ],
    longitude: Annotated[
        float,
        Query(
            alias="lon",
            ge=-180.0,
            le=180.0,
            description="Longitude in WGS 84 decimal degrees.",
            examples=[8.68],
        ),
    ],
    radius_km: Annotated[
        float,
        Query(gt=0.0, le=500.0, description="How far to look for a station, in kilometres."),
    ] = 150.0,
    at: Annotated[
        datetime | None,
        Query(description="Treat this instant as 'now' (UTC); defaults to the current time."),
    ] = None,
) -> WeatherObservationOut:
    """Return the freshest reading of the nearest reporting station."""
    statement = sa.text(_LATEST_SQL.format(columns=_SELECT_COLUMNS, from_clause=_FROM_CLAUSE))
    row = (
        await session.execute(
            statement,
            {
                "lat": latitude,
                "lon": longitude,
                "radius_m": radius_km * 1000.0,
                "at": at or utc_now(),
            },
        )
    ).one_or_none()
    if row is None:
        msg = (
            f"no weather station reported within {radius_km:.0f} km of "
            f"{latitude:.4f}, {longitude:.4f}"
        )
        raise NotFoundError(
            msg,
            details={"latitude": latitude, "longitude": longitude, "radius_km": radius_km},
        )

    _report_mode([_stored_mode(row.provider_mode)])
    return _row_out(row, distance_km=float(row.distance_m) / 1000.0)


def _build_filters(
    *,
    bbox: BoundingBox | None,
    latitude: float | None,
    longitude: float | None,
    at: datetime | None,
) -> tuple[list[str], dict[str, Any]]:
    """Translate the query parameters into SQL predicates and their bound values.

    Every value reaches PostgreSQL as a bound parameter; the strings assembled here are fixed
    fragments chosen by this function, never anything the client typed.
    """
    if (latitude is None) != (longitude is None):
        msg = "lat and lon must be given together"
        raise ValidationError(msg, details={"parameter": "lat/lon"})
    if bbox is not None and latitude is not None:
        msg = "give either bbox or lat/lon, not both"
        raise ValidationError(msg, details={"parameter": "bbox"})

    filters: list[str] = []
    params: dict[str, Any] = {}
    if bbox is not None:
        filters.append(
            "w.location && ST_MakeEnvelope(:west, :south, :east, :north, 4326)::geography"
        )
        params |= {
            "west": bbox.west,
            "south": bbox.south,
            "east": bbox.east,
            "north": bbox.north,
        }
    if latitude is not None and longitude is not None:
        params |= {"lat": latitude, "lon": longitude}
    if at is not None:
        filters.append("w.observed_at <= :at")
        params["at"] = at
    return filters, params
