"""Route planning and the flagship energy analysis (BUILD_SPEC §7, §7.3, §7.4).

Six endpoints, in the order a user meets them: browse the seeded corridors, open one, search for
a place, plan a fresh route, **analyse** it, and plan its charging. Only the last three do
anything hard, and all three of those delegate to
:mod:`autotwin_api.services.route_analysis` — this module stays HTTP.

Two details are load-bearing and easy to get wrong:

* **Declaration order.** ``/geocode`` is registered before ``/{route_id}``. Starlette matches in
  registration order, so the literal path has to come first or ``geocode`` would be read as a
  route identifier.
* **``/{route_id}`` accepts a UUID *or* a slug.** The demo links corridors by slug
  (``frankfurt-stuttgart``), and forcing the frontend to resolve a slug to a UUID before it can
  open a page would buy nothing but a round trip.
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Query, status

from autotwin_api.deps import (
    DbSession,
    GeocodingProviderDep,
    Pagination,
    RoutingProviderDep,
)
from autotwin_api.middleware import set_data_mode
from autotwin_api.schemas.common import ProvenanceOut
from autotwin_api.schemas.routes import (
    ChargingOptimizeRequest,
    ChargingPlanOut,
    PlaceOut,
    RouteAnalysisOut,
    RouteAnalyzeRequest,
    RouteDetailOut,
    RouteEndpointOut,
    RoutePlanRequest,
    RoutePlanResponse,
    RouteSegmentOut,
    RouteSummaryOut,
)
from autotwin_api.services.route_analysis import (
    GEOMETRY_TOLERANCE_DEG,
    analyse_route,
    plan_charging,
)
from autotwin_contracts import (
    Coordinate,
    DataOrigin,
    GeoJSONLineString,
    Page,
    RoadClass,
    SourceSystem,
)
from autotwin_core.errors import NotFoundError, ValidationError

__all__ = ["router"]

router = APIRouter()

_SUMMARY_COLUMNS: Final[str] = """
    r.id, r.slug, r.name, r.origin_name, r.destination_name, r.distance_m, r.duration_s,
    r.routing_profile, r.is_demo,
    ST_Y(r.origin::geometry) AS origin_latitude,
    ST_X(r.origin::geometry) AS origin_longitude,
    ST_Y(r.destination::geometry) AS destination_latitude,
    ST_X(r.destination::geometry) AS destination_longitude
"""

_LIST_COUNT_SQL: Final[str] = "SELECT count(*) FROM routes r {where}"
"""Count matching corridors. ``{where}`` is assembled from fixed predicate fragments chosen by
the handler; every client value travels as a bound parameter."""

_LIST_SQL: Final[str] = """
SELECT {columns}
FROM routes r
{where}
ORDER BY r.is_demo DESC, r.name, r.id
LIMIT :limit OFFSET :offset
"""
"""One page of corridors, demo corridors first (BUILD_SPEC §7)."""

_DETAIL_SQL: Final[str] = """
SELECT {columns},
       ST_AsGeoJSON(ST_Simplify(r.geometry, :tolerance), 6) AS geometry_json,
       r.source, r.source_identifier, r.source_url, r.source_timestamp,
       r.data_origin, r.ingestion_run_id, r.ingested_at
FROM routes r
WHERE {predicate}
"""
"""One corridor by UUID or slug; ``{predicate}`` is one of two fixed fragments, and the
identifier itself is bound either way."""

_DETAIL_SEGMENTS_SQL: Final[str] = """
SELECT s.ordinal, s.start_offset_m, s.distance_m, s.road_class, s.speed_limit_kmh,
       s.assumed_speed_kmh,
       ST_AsGeoJSON(ST_Simplify(s.geometry, :tolerance), 6) AS geometry_json
FROM route_segments s
WHERE s.route_id = :route_id
ORDER BY s.ordinal
"""

_GEOCODE_LIMIT: Final[int] = 10
"""Upper bound on candidates returned. Nominatim's usage policy asks for modest queries, and a
place picker that shows more than ten options is not helping anyone choose."""


def _summary(row: Any) -> RouteSummaryOut:
    """Map one route row onto the list model."""
    return RouteSummaryOut(
        id=row.id,
        slug=row.slug,
        name=row.name,
        origin_name=row.origin_name,
        destination_name=row.destination_name,
        origin=RouteEndpointOut(
            name=row.origin_name,
            latitude=float(row.origin_latitude),
            longitude=float(row.origin_longitude),
        ),
        destination=RouteEndpointOut(
            name=row.destination_name,
            latitude=float(row.destination_latitude),
            longitude=float(row.destination_longitude),
        ),
        distance_m=float(row.distance_m),
        duration_s=float(row.duration_s),
        is_demo=bool(row.is_demo),
    )


@router.get(
    "",
    response_model=Page[RouteSummaryOut],
    summary="List routes",
    description=(
        "Stored corridors, **demo corridors first** and then alphabetically — the order the "
        "route page renders its picker in, so the five seeded Autobahn corridors are always at "
        "the top.\n\n"
        "`q` matches the route name, the slug and both place names, case-insensitively. "
        "Geometry is omitted here; `GET /api/v1/routes/{id}` returns it."
    ),
)
async def list_routes(
    session: DbSession,
    page: Pagination,
    q: Annotated[
        str | None,
        Query(
            description="Case-insensitive substring of the name, slug, origin or destination.",
            examples=["stuttgart"],
            max_length=120,
        ),
    ] = None,
    demo_only: Annotated[
        bool,
        Query(description="Keep only the seeded demo corridors."),
    ] = False,
) -> Page[RouteSummaryOut]:
    """Page through the stored corridors."""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if q:
        clauses.append(
            "(r.name ILIKE :q OR r.slug ILIKE :q OR r.origin_name ILIKE :q "
            "OR r.destination_name ILIKE :q)"
        )
        params["q"] = f"%{q.strip()}%"
    if demo_only:
        clauses.append("r.is_demo IS TRUE")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    total = int(
        (await session.execute(sa.text(_LIST_COUNT_SQL.format(where=where)), params)).scalar_one()
    )
    if total == 0:
        return Page.empty(page=page.page, page_size=page.page_size)

    rows = (
        await session.execute(
            sa.text(_LIST_SQL.format(columns=_SUMMARY_COLUMNS, where=where)),
            {**params, "limit": page.limit, "offset": page.offset},
        )
    ).all()
    return Page.build(
        [_summary(row) for row in rows],
        total=total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get(
    "/geocode",
    response_model=list[PlaceOut],
    summary="Search for a place",
    description=(
        "Resolve free text to candidate places with Nominatim, best match first. Feeds the "
        "origin/destination pickers of the route page.\n\n"
        "An empty list means *no such place* — an answer, not a failure. The response header "
        "`X-AutoTwin-Data-Mode` says whether the geocoder answered live or from the on-disk "
        "cache; Nominatim's usage policy limits AutoTwin to one request per second, so a burst "
        "of keystrokes is served from cache by design."
    ),
)
async def geocode(
    geocoding: GeocodingProviderDep,
    q: Annotated[
        str,
        Query(
            min_length=2,
            max_length=200,
            description="What the user typed, e.g. 'Stuttgart Hauptbahnhof'.",
            examples=["Stuttgart"],
        ),
    ],
    limit: Annotated[
        int,
        Query(ge=1, le=_GEOCODE_LIMIT, description="Maximum candidates to return."),
    ] = 5,
) -> list[PlaceOut]:
    """Geocode ``q`` and return the top candidates."""
    result = await geocoding.geocode(q)
    set_data_mode(result.mode)
    return [
        PlaceOut(
            name=place.name,
            display_name=place.display_name,
            latitude=place.coordinate.latitude,
            longitude=place.coordinate.longitude,
            place_type=place.place_type,
            bundesland=place.bundesland,
            country_code=place.country_code,
            bbox=place.bbox.as_tuple() if place.bbox is not None else None,
            source_identifier=place.source_identifier,
        )
        for place in result.data[:limit]
    ]


@router.post(
    "/plan",
    response_model=RoutePlanResponse,
    status_code=status.HTTP_200_OK,
    summary="Plan a route",
    description=(
        "Route between two places and return the **geometry only** — no energy, no weather, no "
        "traffic. This is what the map calls while a user is still choosing endpoints; "
        "`/routes/analyze` is the expensive one.\n\n"
        "Each end is given either as free text (geocoded with Nominatim) or as an exact "
        "coordinate, which skips the geocoder and its ambiguity. The response states how the "
        "routing engine answered (`data_mode`) and when the underlying route was obtained "
        "(`fetched_at`), so a cached answer is never presented as a fresh one."
    ),
)
async def plan_route(
    request: RoutePlanRequest,
    routing: RoutingProviderDep,
    geocoding: GeocodingProviderDep,
) -> RoutePlanResponse:
    """Geocode both ends if needed, route between them, and return the line."""
    origin_name, origin = await _endpoint(
        geocoding,
        text=request.origin,
        point=request.origin_point,
        field="origin",
    )
    destination_name, destination = await _endpoint(
        geocoding,
        text=request.destination,
        point=request.destination_point,
        field="destination",
    )
    result = await routing.route(origin, destination, profile=request.profile)
    set_data_mode(result.mode)
    route = result.data
    return RoutePlanResponse(
        origin=RouteEndpointOut(
            name=origin_name,
            latitude=route.origin.latitude,
            longitude=route.origin.longitude,
        ),
        destination=RouteEndpointOut(
            name=destination_name,
            latitude=route.destination.latitude,
            longitude=route.destination.longitude,
        ),
        distance_m=route.distance_m,
        duration_s=route.duration_s,
        geometry=GeoJSONLineString.from_coordinates(route.geometry),
        profile=route.profile,
        data_mode=result.mode,
        source_url=result.source_url,
        fetched_at=result.fetched_at,
        warnings=list(result.warnings),
    )


@router.post(
    "/analyze",
    response_model=RouteAnalysisOut,
    status_code=status.HTTP_200_OK,
    summary="Analyse a route's energy (flagship)",
    description=(
        "**The flagship endpoint (BUILD_SPEC §7.3).** What does this trip cost this car today, "
        "and why?\n\n"
        "Name the route either by `route_slug`/`route_id` — a seeded corridor, read straight "
        "out of PostGIS with its segments, no routing engine on the request path — or by "
        "`origin`/`destination`, which geocodes, routes and segments on the fly.\n\n"
        "For every ~5 km segment the analysis attaches the nearest recent DWD observation, the "
        "traffic events that actually cover it, an assumed speed derived from the routing "
        "engine's free-flow speed divided by the matched severity's delay factor, and then "
        "evaluates **both** the physical road-load model and the trained ML regressor. With no "
        "model trained the analysis still runs, on the baseline alone, and `model_name` is null "
        "rather than the response pretending otherwise.\n\n"
        "`weather_penalty_percent` and `traffic_penalty_percent` are measured, not estimated: "
        "the physical model is re-run over every segment at 20 °C in still, dry air, and again "
        "at free-flow speed, and the penalty is the difference. `explanation.drivers` comes "
        "from a deterministic counterfactual ladder that sums exactly to the deviation from the "
        "vehicle's nominal consumption — **no language model is involved**. `assumptions` lists "
        "the gaps that apply to this particular answer.\n\n"
        "Every segment carries its own GeoJSON LineString, `start_offset_km`, "
        "`soc_at_end_percent` and `energy_intensity`, keyed by `ordinal` — the Streckenband and "
        "the map layer join on it."
    ),
)
async def analyze_route(
    request: RouteAnalyzeRequest,
    session: DbSession,
    routing: RoutingProviderDep,
    geocoding: GeocodingProviderDep,
) -> RouteAnalysisOut:
    """Run the full energy analysis and return the §7.3 payload."""
    result = await analyse_route(session, request, routing=routing, geocoding=geocoding)
    return result.response


@router.post(
    "/optimize-charging",
    response_model=ChargingPlanOut,
    status_code=status.HTTP_200_OK,
    summary="Plan the charging stops for a route",
    description=(
        "Runs the same analysis as `/routes/analyze`, then plans the charging stops on top of "
        "it (BUILD_SPEC §7.4, §10.3).\n\n"
        "Candidates are the charging sites within `corridor_buffer_km` of the route reaching "
        "`min_power_kw`, selected by a PostGIS corridor query with each site's **real** offset "
        "along the route and its lateral distance; the detour is that distance counted twice, "
        "because a stop leaves the corridor and comes back. A German Autobahn corridor holds "
        "hundreds of fast chargers, so they are thinned to the strongest site per "
        "`candidate_spacing_km` before the beam search runs — two ultra-fast sites 400 m apart "
        "are one decision.\n\n"
        "`feasible: false` is an answer, not an error: `reason_de` / `reason_en` say why no "
        "stop sequence finishes the route. Each stop carries `offset_km`, which is what the "
        "Streckenband places its charging tick with. The analysis the plan was built on is "
        "returned under `analysis`, so the page needs one request, not two."
    ),
)
async def optimize_charging(
    request: ChargingOptimizeRequest,
    session: DbSession,
    routing: RoutingProviderDep,
    geocoding: GeocodingProviderDep,
) -> ChargingPlanOut:
    """Analyse the route, gather corridor candidates and return the best plan found."""
    return await plan_charging(session, request, routing=routing, geocoding=geocoding)


@router.get(
    "/{route_id}",
    response_model=RouteDetailOut,
    summary="Get one route",
    description=(
        "One stored corridor with its geometry, its analysis segments and its provenance.\n\n"
        "`route_id` accepts the row's UUID **or** its slug (`frankfurt-stuttgart`), so the "
        "frontend can link a demo corridor without resolving it first.\n\n"
        f"Geometry is simplified to ~{GEOMETRY_TOLERANCE_DEG * 111_000:.0f} m before transport: "
        "a seeded corridor is a 3 000-vertex OSRM polyline, and at this tolerance the line is "
        "pixel-identical at every zoom a 200 km corridor is viewed at while the payload drops "
        "roughly eightfold."
    ),
)
async def get_route(session: DbSession, route_id: str) -> RouteDetailOut:
    """Return the corridor identified by a UUID or a slug."""
    identifier = _as_uuid(route_id)
    predicate = "r.id = :route_id" if identifier is not None else "r.slug = :slug"
    row = (
        await session.execute(
            sa.text(_DETAIL_SQL.format(columns=_SUMMARY_COLUMNS, predicate=predicate)),
            {
                "route_id": identifier,
                "slug": route_id,
                "tolerance": GEOMETRY_TOLERANCE_DEG,
            },
        )
    ).one_or_none()
    if row is None:
        msg = f"no route with id or slug {route_id!r}"
        raise NotFoundError(msg, details={"route": route_id})

    segment_rows = (
        await session.execute(
            sa.text(_DETAIL_SEGMENTS_SQL),
            {"route_id": row.id, "tolerance": GEOMETRY_TOLERANCE_DEG},
        )
    ).all()

    summary = _summary(row)
    return RouteDetailOut(
        **summary.model_dump(),
        geometry=GeoJSONLineString.model_validate_json(row.geometry_json),
        routing_profile=row.routing_profile,
        segment_count=len(segment_rows),
        segments=[
            RouteSegmentOut(
                ordinal=int(segment.ordinal),
                start_offset_km=float(segment.start_offset_m) / 1000.0,
                distance_km=float(segment.distance_m) / 1000.0,
                road_class=RoadClass(segment.road_class),
                speed_limit_kmh=(
                    None if segment.speed_limit_kmh is None else float(segment.speed_limit_kmh)
                ),
                assumed_speed_kmh=(
                    None if segment.assumed_speed_kmh is None else float(segment.assumed_speed_kmh)
                ),
                geometry=(
                    GeoJSONLineString.model_validate_json(segment.geometry_json)
                    if segment.geometry_json
                    else None
                ),
            )
            for segment in segment_rows
        ],
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


def _as_uuid(value: str) -> UUID | None:
    """Parse ``value`` as a UUID, or ``None`` when it is a slug.

    A failed parse is not an error here: ``frankfurt-stuttgart`` is a perfectly valid route
    identifier, just not a UUID one.
    """
    try:
        return UUID(value)
    except ValueError:
        return None


async def _endpoint(
    geocoding: GeocodingProviderDep,
    *,
    text: str | None,
    point: Any,
    field: str,
) -> tuple[str, Coordinate]:
    """Resolve one end of a plan request to a name and a coordinate.

    An explicit coordinate wins over free text: a caller that sends one has already made the
    choice the geocoder would otherwise guess at.
    """
    if point is not None:
        return (
            text or f"{point.latitude:.4f}, {point.longitude:.4f}",
            Coordinate(latitude=point.latitude, longitude=point.longitude),
        )
    if text is None:  # pragma: no cover - the request model rejects this first
        msg = f"{field} is required"
        raise ValidationError(msg, details={"field": field})
    result = await geocoding.geocode(text)
    set_data_mode(result.mode)
    if not result.data:
        msg = f"no place matched {text!r}"
        raise NotFoundError(msg, details={"field": field, "query": text})
    place = result.data[0]
    return place.name, place.coordinate
