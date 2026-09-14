"""Traffic disruptions from the Autobahn GmbH feed (BUILD_SPEC §7).

The list endpoint and the GeoJSON endpoint share their filters exactly — the same box, the same
event type, the same severity, the same road, the same notion of "active" — because a map and
the table beside it that disagree about what they are showing is a bug report waiting to happen.
Only the envelope differs: ``Page[TrafficEvent]`` for the table, an RFC 7946
``FeatureCollection`` for the MapLibre source.

"Active" is evaluated against a reference instant rather than ``now()`` so that a map can be
rewound: an event counts as active when it has started (or states no start) and has not ended
(or states no end). Roughly a quarter of the feed carries no validity window at all, which is
why the null cases are treated as open rather than dropped.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Query

from autotwin_api.deps import BoundingBoxQuery, DbSession, Pagination
from autotwin_api.middleware import set_data_mode
from autotwin_api.schemas.common import ProvenanceOut
from autotwin_api.schemas.traffic import TrafficEventOut
from autotwin_contracts import (
    BoundingBox,
    DataOrigin,
    GeoJSONFeature,
    GeoJSONFeatureCollection,
    GeoJSONLineString,
    GeoJSONPoint,
    Page,
    ProviderMode,
    SourceSystem,
    TrafficEventType,
    TrafficSeverity,
    utc_now,
)

__all__ = ["router"]

router = APIRouter()

GEOMETRY_TOLERANCE_DEG: Final[float] = 0.0001
"""Douglas-Peucker tolerance (~11 m) applied to event geometry before it goes on the wire.

A single roadworks report can carry 600 vertices; at 11 m the simplified line is indistinguishable
on any map showing a stretch of Autobahn, and a page of 50 events stays a small response.
"""

GEOJSON_FEATURE_CAP: Final[int] = 20_000
"""Hard cap on features in one collection, matching the charging layer's cap in BUILD_SPEC §7.

A cap rather than pagination because a map source cannot page: the layer either has the
features or it does not. 20 000 is far above the size of the whole federal feed, so in practice
this only guards against a filter mistake.
"""

_COLUMNS: Final[str] = """
    e.id, e.external_id, e.event_type, e.severity, e.road_name, e.direction, e.title,
    e.description, e.starts_at, e.ends_at, e.is_blocked, e.delay_minutes,
    e.source, e.source_identifier, e.source_url, e.source_timestamp, e.data_origin,
    e.ingestion_run_id, e.ingested_at, run.provider_mode,
    ST_Y(e.location::geometry) AS latitude,
    ST_X(e.location::geometry) AS longitude,
    ST_AsGeoJSON(ST_Simplify(e.geometry, :tolerance), 6) AS geometry_json
"""
"""Projection shared by both endpoints, so the table and the map cannot drift apart."""

_FROM_CLAUSE: Final[str] = """
    FROM traffic_events e
    LEFT JOIN data_ingestion_runs run ON run.id = e.ingestion_run_id
"""


def _build_filters(
    *,
    bbox: BoundingBox | None,
    event_type: TrafficEventType | None,
    severity: TrafficSeverity | None,
    road: str | None,
    active_only: bool,
    at: datetime | None,
) -> tuple[str, dict[str, Any]]:
    """Turn the query parameters into one ``WHERE`` clause and its bound values.

    The clause is assembled from fixed fragments chosen here; every client-supplied value
    travels as a bound parameter, including the road name, which is the one filter that takes
    free text.
    """
    clauses: list[str] = []
    params: dict[str, Any] = {"tolerance": GEOMETRY_TOLERANCE_DEG}
    if bbox is not None:
        clauses.append(
            "e.location && ST_MakeEnvelope(:west, :south, :east, :north, 4326)::geography"
        )
        params |= {
            "west": bbox.west,
            "south": bbox.south,
            "east": bbox.east,
            "north": bbox.north,
        }
    if event_type is not None:
        clauses.append("e.event_type = CAST(:event_type AS traffic_event_type)")
        params["event_type"] = event_type.value
    if severity is not None:
        clauses.append("e.severity = CAST(:severity AS traffic_severity)")
        params["severity"] = severity.value
    if road is not None:
        # Autobahn names are short and canonical ("A5", "A81"); an exact, case-folded match is
        # what a user filtering by road means, and it keeps the predicate sargable.
        clauses.append("upper(e.road_name) = upper(:road)")
        params["road"] = road.strip()
    if active_only:
        clauses.append("(e.starts_at IS NULL OR e.starts_at <= :at)")
        clauses.append("(e.ends_at IS NULL OR e.ends_at >= :at)")
        params["at"] = at or utc_now()
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return where, params


def _row_out(row: Any) -> TrafficEventOut:
    """Map one raw-SQL row onto the response model."""
    return TrafficEventOut(
        id=row.id,
        external_id=row.external_id,
        event_type=TrafficEventType(row.event_type),
        severity=TrafficSeverity(row.severity),
        road_name=row.road_name,
        direction=row.direction,
        title=row.title,
        description=row.description,
        latitude=float(row.latitude),
        longitude=float(row.longitude),
        geometry=_line(row.geometry_json),
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        is_blocked=bool(row.is_blocked),
        delay_minutes=None if row.delay_minutes is None else float(row.delay_minutes),
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


def _line(geojson: str | None) -> GeoJSONLineString | None:
    """Parse ``ST_AsGeoJSON`` output, tolerating NULL and a degenerate one-point line."""
    if not geojson:
        return None
    try:
        line = GeoJSONLineString.model_validate_json(geojson)
    except ValueError:
        # Simplification can in principle collapse a line; a LineString needs two positions,
        # and an event that loses its line still has its representative point.
        return None
    return line


def _stored_mode(provider_mode: str | None) -> ProviderMode:
    """Freshness of a stored row: a cached copy at best, a fixture if it was ingested as one."""
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
    "/events",
    response_model=Page[TrafficEventOut],
    summary="List traffic events",
    description=(
        "Roadworks, closures, incidents, warnings and congestion from the Autobahn GmbH feed, "
        "most recently started first.\n\n"
        "* `bbox=west,south,east,north` — only events whose representative point is in the box.\n"
        "* `event_type`, `severity` — exact enum matches.\n"
        "* `road` — exact, case-insensitive road name, e.g. `A5`.\n"
        "* `active_only` — keep only events that have started and have not ended at `at` "
        "(default: now). Events without a validity window count as active, because the source "
        "publishes many long-running Baustellen with no dates. **Defaults to `false` here** so "
        "the endpoint returns the feed as stored; `/traffic/events/geojson` defaults it to "
        "`true`, because a map layer showing expired roadworks is worse than an empty one.\n\n"
        "`geometry` is the published extent of the disruption where the source gives one, "
        "simplified to ~11 m for transport, and null for an event reported as a single point."
    ),
)
async def list_events(
    session: DbSession,
    page: Pagination,
    bbox: BoundingBoxQuery,
    event_type: Annotated[
        TrafficEventType | None,
        Query(description="Keep only events of this type."),
    ] = None,
    severity: Annotated[
        TrafficSeverity | None,
        Query(description="Keep only events of this severity."),
    ] = None,
    road: Annotated[
        str | None,
        Query(
            description="Road name to filter by, e.g. 'A5'. Case-insensitive, exact.",
            examples=["A5"],
            max_length=32,
        ),
    ] = None,
    active_only: Annotated[
        bool,
        Query(description="Keep only events valid at `at`."),
    ] = False,
    at: Annotated[
        datetime | None,
        Query(description="Instant `active_only` is evaluated against (UTC); defaults to now."),
    ] = None,
) -> Page[TrafficEventOut]:
    """Page through the traffic feed."""
    where, params = _build_filters(
        bbox=bbox,
        event_type=event_type,
        severity=severity,
        road=road,
        active_only=active_only,
        at=at,
    )
    total = int(
        (
            await session.execute(sa.text(f"SELECT count(*) {_FROM_CLAUSE} {where}"), params)
        ).scalar_one()
    )
    if total == 0:
        set_data_mode(ProviderMode.fixture)
        return Page.empty(page=page.page, page_size=page.page_size)

    rows = (
        await session.execute(
            sa.text(
                f"SELECT {_COLUMNS} {_FROM_CLAUSE} {where} "
                "ORDER BY e.starts_at DESC NULLS LAST, e.id LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": page.limit, "offset": page.offset},
        )
    ).all()
    _report_mode([_stored_mode(row.provider_mode) for row in rows])
    return Page.build(
        [_row_out(row) for row in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get(
    "/events/geojson",
    response_model=GeoJSONFeatureCollection,
    summary="Traffic events as GeoJSON",
    description=(
        "The same events as `/traffic/events`, under the same filters, as an RFC 7946 "
        "`FeatureCollection` a MapLibre source can consume directly.\n\n"
        "Each feature carries the **line** where the source published one and falls back to the "
        "representative **point** otherwise — a 14 km Baustelle drawn as a single pin "
        "understates it badly. `properties` carries `id`, `external_id`, `event_type`, "
        "`severity`, `is_blocked`, `road_name`, `direction`, `title`, `delay_minutes` and "
        f"`geometry_kind`, and the collection carries a `bbox` for initial map fitting. Capped "
        f"at {GEOJSON_FEATURE_CAP:,} features.\n\n"
        "`active_only` **defaults to `true`** here, unlike on `/traffic/events`: a map layer "
        "drawing roadworks that ended in 2019 is worse than one drawing nothing."
    ),
)
async def events_geojson(
    session: DbSession,
    bbox: BoundingBoxQuery,
    event_type: Annotated[
        TrafficEventType | None,
        Query(description="Keep only events of this type."),
    ] = None,
    severity: Annotated[
        TrafficSeverity | None,
        Query(description="Keep only events of this severity."),
    ] = None,
    road: Annotated[
        str | None,
        Query(description="Road name to filter by, e.g. 'A5'.", max_length=32),
    ] = None,
    active_only: Annotated[
        bool,
        Query(description="Keep only events valid at `at`."),
    ] = True,
    at: Annotated[
        datetime | None,
        Query(description="Instant `active_only` is evaluated against (UTC); defaults to now."),
    ] = None,
    limit: Annotated[
        int,
        Query(ge=1, le=GEOJSON_FEATURE_CAP, description="Maximum features to return."),
    ] = GEOJSON_FEATURE_CAP,
) -> GeoJSONFeatureCollection:
    """Render the filtered events as a feature collection with an extent."""
    where, params = _build_filters(
        bbox=bbox,
        event_type=event_type,
        severity=severity,
        road=road,
        active_only=active_only,
        at=at,
    )
    rows = (
        await session.execute(
            sa.text(
                f"SELECT {_COLUMNS} {_FROM_CLAUSE} {where} "
                "ORDER BY e.severity DESC, e.id LIMIT :limit"
            ),
            {**params, "limit": limit},
        )
    ).all()
    _report_mode([_stored_mode(row.provider_mode) for row in rows])

    features: list[GeoJSONFeature] = []
    west = south = float("inf")
    east = north = float("-inf")
    for row in rows:
        line = _line(row.geometry_json)
        latitude, longitude = float(row.latitude), float(row.longitude)
        geometry: GeoJSONLineString | GeoJSONPoint = line or GeoJSONPoint(
            coordinates=(longitude, latitude)
        )
        positions = line.coordinates if line is not None else [(longitude, latitude)]
        for position_longitude, position_latitude in positions:
            west = min(west, position_longitude)
            east = max(east, position_longitude)
            south = min(south, position_latitude)
            north = max(north, position_latitude)
        features.append(
            GeoJSONFeature(
                id=str(row.id),
                geometry=geometry,
                properties={
                    "id": str(row.id),
                    "external_id": row.external_id,
                    "event_type": TrafficEventType(row.event_type).value,
                    "severity": TrafficSeverity(row.severity).value,
                    "is_blocked": bool(row.is_blocked),
                    "road_name": row.road_name,
                    "direction": row.direction,
                    "title": row.title,
                    "delay_minutes": (
                        None if row.delay_minutes is None else float(row.delay_minutes)
                    ),
                    "geometry_kind": "LineString" if line is not None else "Point",
                },
            )
        )

    extent = (west, south, east, north) if features else None
    return GeoJSONFeatureCollection(features=features, bbox=extent)
