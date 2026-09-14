"""The route-analysis engine behind ``POST /api/v1/routes/analyze`` (BUILD_SPEC §7.3).

This module answers one question end to end: *what does this trip cost this car today, and
why?* It is the heart of AutoTwin, so the reasoning behind its structure is written down here
rather than scattered through the code.

The pipeline
------------

1. **Resolve the route.** A seeded corridor (``route_slug`` / ``route_id``) is read straight
   out of PostGIS together with its ``route_segments`` — no routing engine on the request path,
   which is why the demo is fast and works offline. Free text is geocoded with Nominatim,
   routed with OSRM and segmented in process with
   :func:`~autotwin_core.geo.segmentation.segment_polyline`.
2. **Weather per segment** — one query. A ``LATERAL`` join runs a KNN lookup
   (``ORDER BY location <-> midpoint LIMIT 1``) once per segment midpoint inside PostgreSQL.
   The alternative, a query per segment, is 41 round trips for a 200 km corridor and 79 for a
   400 km one.
3. **Traffic per segment** — one query. Events are pre-filtered by the corridor's bounding box
   (which the geography index can serve), then matched to segments with ``ST_DWithin`` against
   the segment geometry, so a 14 km roadworks zone raises the severity of every segment it
   actually covers instead of only the one holding its representative point.
4. **Assumed speed.** Free-flow speed comes from the routing engine's own steps; the matched
   severity's ``delay_factor`` (BUILD_SPEC §2) divides it down. Traffic acts on *time*, and
   deriving the speed this way is what makes the free-flow counterfactual in step 7 exact.
5. **Both models, always.** Every segment is evaluated by the physical road-load model *and*,
   when one is trained, by the ML regressor. With no model the analysis runs on the baseline
   alone and says so — ``model_name`` is null rather than the response pretending.
6. **SOC trajectory** by integrating segment energy against the usable capacity.
7. **Penalties as counterfactuals.** ``weather_penalty_percent`` and
   ``traffic_penalty_percent`` are not coefficients: the physical model is re-run over every
   segment at 20 °C in still, dry air, and again at free-flow speed, and the penalty is the
   measured difference. A reader can reproduce each one.
8. **Explanation** from :func:`~autotwin_ml.insights.explain_route_energy` — a deterministic
   counterfactual ladder whose drivers sum exactly to the deviation from nominal. No LLM.

What this model does not know
-----------------------------

There is no elevation source in the data set, so ``gradient_percent`` is zero on every segment
and the ``gradient`` driver is always zero. Wind *speed* is observed but not wind *direction*,
so the headwind component is zero rather than guessed. Both gaps are reported in the response's
``assumptions`` list instead of being hidden behind a plausible number.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_api.middleware import set_data_mode
from autotwin_api.schemas.common import ProvenanceOut
from autotwin_api.schemas.routes import (
    ChargingOptimizeRequest,
    ChargingPlanOut,
    ChargingStationSummaryOut,
    ChargingStopOut,
    DataModesOut,
    EnergyDriverOut,
    EnergyExplanationOut,
    RouteAnalysisOut,
    RouteAnalysisRouteOut,
    RouteAnalyzeRequest,
    RouteEndpointOut,
    RouteSegmentAnalysisOut,
    RouteSelector,
    VehicleProfileOut,
)
from autotwin_api.schemas.traffic import TrafficEventOut
from autotwin_api.schemas.weather import WeatherObservationOut
from autotwin_contracts import (
    Bundesland,
    ChargingCategory,
    Coordinate,
    DataOrigin,
    EnergyIntensity,
    GeoJSONLineString,
    ProviderMode,
    RoadClass,
    SourceSystem,
    TrafficEventType,
    TrafficSeverity,
    VehicleProfile,
    WeatherCondition,
    get_vehicle_profile,
    utc_now,
)
from autotwin_contracts.records import RouteResult, RouteStepRecord
from autotwin_core.errors import NotFoundError, ValidationError
from autotwin_core.geo.segmentation import PolylineSegment, segment_polyline
from autotwin_core.logging import get_logger
from autotwin_core.providers import GeocodingProvider, RoutingProvider
from autotwin_ml.baseline import PhysicalEnergyModel
from autotwin_ml.charging import (
    ChargingCandidate,
    ChargingPlan,
    optimise_charging,
)
from autotwin_ml.features import default_speed_limit_kmh
from autotwin_ml.inference import EnergyPredictor
from autotwin_ml.insights import EnergyExplanation, TrafficContext, WeatherContext
from autotwin_ml.insights import explain_route_energy as build_explanation
from autotwin_ml.registry import ModelNotAvailable, get_active_model
from autotwin_ml.types import EnergyResult, RouteEnergySegment, SegmentConditions

__all__ = [
    "RouteAnalysisResult",
    "analyse_route",
    "plan_charging",
    "resolve_route",
]

_LOGGER = get_logger(__name__)

GEOMETRY_TOLERANCE_DEG: Final[float] = 0.0001
"""Douglas-Peucker tolerance (~11 m) applied to stored geometry before it goes on the wire.

A seeded corridor is a 3 000-vertex OSRM polyline; at 11 m the simplified line is
pixel-identical at every zoom level a 200 km corridor is ever viewed at, and the payload drops
from 64 kB to 8 kB. Routes planned on the request path are emitted at the resolution the engine
returned, because re-simplifying them would pull a geometry library into the API for one call.
"""

MATCH_TOLERANCE_DEG: Final[float] = 0.0005
"""Tolerance (~55 m) for the geometry used in the corridor *matching* queries.

Distinct from the transport tolerance on purpose: geodesic line-to-line distance is O(n·m), and
a corridor query against 3 000-vertex geometry costs seconds where 150 vertices costs
milliseconds. 55 m of shortcut is far inside the 2 km buffer the match uses, so no event
changes side of the threshold because of it.
"""

BBOX_PADDING_DEG: Final[float] = 0.06
"""Padding (~6.7 km) on the corridor bounding box used to pre-filter events and observations.

Wider than any matching buffer this module uses, so the cheap index-backed box test can only
ever admit extra candidates, never drop a true one.
"""

REFERENCE_TEMPERATURE_C: Final[float] = 20.0
"""Temperature the weather counterfactual is evaluated at — where HVAC load is zero and the
cold-battery factor is one (BUILD_SPEC §10.1). Also the fallback when no observation is in
range, in which case the response's ``assumptions`` says so."""

_PERCENT: Final[float] = 100.0
_MIN_ANALYSIS_SPEED_KMH: Final[float] = 5.0
"""Floor on a segment's assumed speed. A routing engine occasionally reports a step with a
near-zero free-flow speed (a ferry leg, a barrier); dividing distance by it would produce a
segment that takes days and dominates the whole trip's auxiliary energy."""

_MAX_CHARGING_CANDIDATES: Final[int] = 60
"""Upper bound on the sites handed to the beam search after thinning — a 600 km corridor at the
default 10 km spacing produces ~60, and the search cost grows with candidates * beam * depth."""


# ======================================================================================
# Value objects
# ======================================================================================


@dataclass(frozen=True, slots=True)
class AnalysisSegment:
    """One analysis chunk of a route, before any energy or weather is attached."""

    ordinal: int
    """0-based position along the route; the key the frontend joins chart and map on."""

    start_offset_m: float
    """Distance from the origin to the start of this segment, in metres."""

    distance_m: float
    """Segment length in metres."""

    road_class: RoadClass
    """Class covering most of the segment."""

    speed_limit_kmh: float | None
    """Posted limit, or ``None`` on an unrestricted Autobahn stretch."""

    free_flow_speed_kmh: float
    """Speed this segment would be driven at with no traffic, in km/h."""

    geometry: GeoJSONLineString | None
    """Geometry for the response; ``None`` only if the stored row had none."""


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """A route ready to analyse, however it was obtained."""

    id: UUID | None
    slug: str | None
    name: str
    origin_name: str
    destination_name: str
    origin: Coordinate
    destination: Coordinate
    geometry: GeoJSONLineString
    distance_m: float
    duration_s: float
    is_demo: bool
    routing_mode: ProviderMode
    """How the geometry was obtained — a stored corridor reports ``cache`` at best, because it
    is a persisted copy of an earlier answer rather than something fetched for this request."""

    segments: tuple[AnalysisSegment, ...]
    warnings: tuple[str, ...] = ()

    @property
    def segment_source(self) -> _SegmentSource:
        """SQL that materialises this route's segments inside PostgreSQL for corridor queries."""
        if self.id is not None:
            return _SegmentSource.from_stored(self.id)
        return _SegmentSource.from_segments(self.segments)


@dataclass(frozen=True, slots=True)
class _SegmentSource:
    """A ``SELECT`` producing ``(ordinal, start_offset_m, distance_m, geom)`` for one route.

    The corridor queries (weather, traffic, charging) are identical for a stored corridor and
    for one planned on the request path; only where the segment geometry comes from differs.
    Capturing that difference in a tiny SQL fragment plus its parameters keeps three
    non-trivial spatial queries written once instead of twice.

    The fragment is chosen by this module, never built from user input; every value that
    reaches PostgreSQL does so as a bound parameter.
    """

    sql: str
    params: Mapping[str, Any]

    @classmethod
    def from_stored(cls, route_id: UUID) -> _SegmentSource:
        """Read the persisted ``route_segments`` rows — the demo's fast path."""
        return cls(
            sql=(
                "SELECT s.ordinal, s.start_offset_m, s.distance_m, "
                "ST_Simplify(s.geometry, :match_tolerance) AS geom "
                "FROM route_segments s WHERE s.route_id = :route_id"
            ),
            params={"route_id": route_id, "match_tolerance": MATCH_TOLERANCE_DEG},
        )

    @classmethod
    def from_segments(cls, segments: Sequence[AnalysisSegment]) -> _SegmentSource:
        """Send in-process segments to PostgreSQL as parallel arrays.

        ``unnest(...) WITH ORDINALITY`` turns three arrays into rows in one pass, which keeps
        the round trip to a single statement no matter how long the route is.
        """
        return cls(
            sql=(
                "SELECT (t.idx - 1)::int AS ordinal, t.start_offset_m, t.distance_m, "
                "ST_SetSRID(ST_GeomFromText(t.wkt), 4326) AS geom "
                "FROM unnest("
                "  CAST(:segment_wkts AS text[]),"
                "  CAST(:segment_starts AS float8[]),"
                "  CAST(:segment_lengths AS float8[])"
                ") WITH ORDINALITY AS t(wkt, start_offset_m, distance_m, idx)"
            ),
            params={
                "segment_wkts": [_linestring_wkt(segment.geometry) for segment in segments],
                "segment_starts": [segment.start_offset_m for segment in segments],
                "segment_lengths": [segment.distance_m for segment in segments],
            },
        )


@dataclass(frozen=True, slots=True)
class _SegmentWeather:
    """The observation a segment adopted, and how far away and how old it was."""

    observation: WeatherObservationOut
    distance_km: float
    age_hours: float

    def is_usable(self, *, radius_km: float, max_age_hours: float) -> bool:
        """Whether this observation may drive the physics for its segment.

        Both bounds matter and for different reasons: a station 300 km away describes another
        weather system, and a three-day-old reading describes another day. Either way the
        honest answer is "no observation", not a number that looks like one.
        """
        return self.distance_km <= radius_km and self.age_hours <= max_age_hours


@dataclass(frozen=True, slots=True)
class RouteAnalysisResult:
    """An analysis plus the intermediate artefacts the charging optimiser needs.

    ``/optimize-charging`` runs the same analysis as ``/analyze`` and then plans on top of it.
    Returning the energy segments alongside the response model is what lets it do that without
    either duplicating the pipeline or re-parsing its own JSON.
    """

    response: RouteAnalysisOut
    route: ResolvedRoute
    profile: VehicleProfile
    energy_segments: tuple[RouteEnergySegment, ...]
    mean_temperature_c: float
    energy_deficit_kwh: float


# ======================================================================================
# Route resolution
# ======================================================================================


async def resolve_route(
    session: AsyncSession,
    selector: RouteSelector,
    *,
    routing: RoutingProvider,
    geocoding: GeocodingProvider,
) -> ResolvedRoute:
    """Turn a request's route selector into a geometry with segments.

    Raises:
        NotFoundError: The named corridor does not exist, or a free-text place could not be
            geocoded. Both are 404: the client asked for something that is not there.
        ProviderError: The routing engine or the geocoder failed and could not fall back.
    """
    if selector.route_id is not None or selector.route_slug is not None:
        return await _load_stored_route(
            session,
            route_id=selector.route_id,
            slug=selector.route_slug,
        )
    origin = await _resolve_endpoint(
        geocoding,
        text=selector.origin,
        point=selector.origin_point,
        field="origin",
    )
    destination = await _resolve_endpoint(
        geocoding,
        text=selector.destination,
        point=selector.destination_point,
        field="destination",
    )
    return await _plan_route(routing, origin, destination)


_STORED_ROUTE_SQL: Final[str] = """
SELECT r.id, r.slug, r.name, r.origin_name, r.destination_name, r.distance_m, r.duration_s,
       r.routing_profile, r.is_demo,
       ST_Y(r.origin::geometry) AS origin_latitude,
       ST_X(r.origin::geometry) AS origin_longitude,
       ST_Y(r.destination::geometry) AS destination_latitude,
       ST_X(r.destination::geometry) AS destination_longitude,
       ST_AsGeoJSON(ST_Simplify(r.geometry, :tolerance), 6) AS geometry_json,
       r.source, r.source_identifier, r.source_url, r.source_timestamp,
       r.data_origin, r.ingestion_run_id, r.ingested_at, run.provider_mode
FROM routes r
LEFT JOIN data_ingestion_runs run ON run.id = r.ingestion_run_id
WHERE {predicate}
"""
"""Load one corridor. ``{predicate}`` is filled with one of two fixed fragments chosen by the
caller — never with anything a client typed; the identifier itself is always bound."""


async def _load_stored_route(
    session: AsyncSession,
    *,
    route_id: UUID | None,
    slug: str | None,
) -> ResolvedRoute:
    """Read a seeded corridor and its segments out of PostGIS.

    Two queries rather than one join: a route has up to ~120 segments, and joining would repeat
    the (already simplified) route geometry on every one of them.
    """
    predicate = "r.id = :route_id" if route_id is not None else "r.slug = :slug"
    statement = sa.text(_STORED_ROUTE_SQL.format(predicate=predicate))
    row = (
        await session.execute(
            statement,
            {"route_id": route_id, "slug": slug, "tolerance": GEOMETRY_TOLERANCE_DEG},
        )
    ).one_or_none()
    if row is None:
        identifier = str(route_id) if route_id is not None else str(slug)
        msg = f"no route with {'id' if route_id is not None else 'slug'} {identifier!r}"
        raise NotFoundError(msg, details={"route": identifier})

    segments = await _load_stored_segments(session, row.id)
    if not segments:
        msg = f"route {row.slug or row.id} has no segments; re-run `seed routes` to rebuild them"
        raise NotFoundError(msg, details={"route": str(row.id)})

    return ResolvedRoute(
        id=row.id,
        slug=row.slug,
        name=row.name,
        origin_name=row.origin_name,
        destination_name=row.destination_name,
        origin=Coordinate(latitude=row.origin_latitude, longitude=row.origin_longitude),
        destination=Coordinate(
            latitude=row.destination_latitude,
            longitude=row.destination_longitude,
        ),
        geometry=_parse_linestring(row.geometry_json),
        distance_m=float(row.distance_m),
        duration_s=float(row.duration_s),
        is_demo=bool(row.is_demo),
        routing_mode=_stored_mode(row.provider_mode),
        segments=segments,
    )


async def _load_stored_segments(
    session: AsyncSession,
    route_id: UUID,
) -> tuple[AnalysisSegment, ...]:
    """Load ``route_segments`` in route order, geometry simplified for transport."""
    statement = sa.text(
        "SELECT s.ordinal, s.start_offset_m, s.distance_m, s.road_class, s.speed_limit_kmh, "
        "       s.assumed_speed_kmh, "
        "       ST_AsGeoJSON(ST_Simplify(s.geometry, :tolerance), 6) AS geometry_json "
        "FROM route_segments s WHERE s.route_id = :route_id ORDER BY s.ordinal"
    )
    rows = (
        await session.execute(
            statement,
            {"route_id": route_id, "tolerance": GEOMETRY_TOLERANCE_DEG},
        )
    ).all()
    return tuple(
        AnalysisSegment(
            ordinal=int(row.ordinal),
            start_offset_m=float(row.start_offset_m),
            distance_m=float(row.distance_m),
            road_class=RoadClass(row.road_class),
            speed_limit_kmh=_optional_float(row.speed_limit_kmh),
            free_flow_speed_kmh=_free_flow_speed(
                assumed_speed_kmh=_optional_float(row.assumed_speed_kmh),
                road_class=RoadClass(row.road_class),
                speed_limit_kmh=_optional_float(row.speed_limit_kmh),
            ),
            geometry=_parse_linestring(row.geometry_json),
        )
        for row in rows
    )


async def _resolve_endpoint(
    geocoding: GeocodingProvider,
    *,
    text: str | None,
    point: Any,
    field: str,
) -> tuple[str, Coordinate]:
    """Resolve one end of a requested route to a name and a coordinate.

    An explicit coordinate wins over free text, because a caller that sends one has already
    done the disambiguation the geocoder would otherwise guess at.
    """
    if point is not None:
        label = text or f"{point.latitude:.4f}, {point.longitude:.4f}"
        return label, Coordinate(latitude=point.latitude, longitude=point.longitude)
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


async def _plan_route(
    routing: RoutingProvider,
    origin: tuple[str, Coordinate],
    destination: tuple[str, Coordinate],
) -> ResolvedRoute:
    """Route between two resolved endpoints and cut the geometry into analysis segments."""
    origin_name, origin_point = origin
    destination_name, destination_point = destination
    result = await routing.route(origin_point, destination_point)
    set_data_mode(result.mode)
    route: RouteResult = result.data

    polyline_segments = segment_polyline(route.geometry)
    attributes = _project_steps(route.steps, polyline_segments, route.average_speed_kmh)
    segments = tuple(
        AnalysisSegment(
            ordinal=segment.ordinal,
            start_offset_m=segment.start_offset_m,
            distance_m=segment.length_m,
            road_class=road_class,
            speed_limit_kmh=speed_limit_kmh,
            free_flow_speed_kmh=_free_flow_speed(
                assumed_speed_kmh=free_flow_kmh,
                road_class=road_class,
                speed_limit_kmh=speed_limit_kmh,
            ),
            geometry=segment.as_linestring(),
        )
        for segment, (road_class, speed_limit_kmh, free_flow_kmh) in zip(
            polyline_segments, attributes, strict=True
        )
    )
    return ResolvedRoute(
        id=None,
        slug=None,
        name=f"{origin_name} → {destination_name}",
        origin_name=origin_name,
        destination_name=destination_name,
        origin=route.origin,
        destination=route.destination,
        geometry=GeoJSONLineString.from_coordinates(route.geometry),
        distance_m=route.distance_m,
        duration_s=route.duration_s,
        is_demo=False,
        routing_mode=result.mode,
        segments=segments,
        warnings=tuple(result.warnings),
    )


def _project_steps(
    steps: Sequence[RouteStepRecord],
    segments: Sequence[PolylineSegment],
    fallback_speed_kmh: float,
) -> list[tuple[RoadClass, float | None, float]]:
    """Project the routing engine's steps onto equal-length segments.

    Steps and segments are both ordered along the route but cut at different places: a step is
    one manoeuvre (20 m at a roundabout, 40 km on the Autobahn), a segment is a fixed 5 km of
    analysis. Laying the steps out as offset ranges and intersecting them with each segment is
    what lets a segment say "motorway, 130 km/h" with the engine's own numbers behind it.

    The seeded corridors were built the same way by the ingestion pipeline, so a route analysed
    from text and the same route analysed from its slug describe their segments identically.
    """
    if not steps:
        return [(RoadClass.unknown, None, fallback_speed_kmh) for _ in segments]

    ranges: list[tuple[float, float, RouteStepRecord]] = []
    offset = 0.0
    for step in steps:
        ranges.append((offset, offset + step.distance_m, step))
        offset += step.distance_m

    projected: list[tuple[RoadClass, float | None, float]] = []
    for segment in segments:
        start, end = segment.start_offset_m, segment.end_offset_m
        metres_by_class: dict[RoadClass, float] = {}
        limit_by_class: dict[RoadClass, float | None] = {}
        weighted_speed = 0.0
        covered = 0.0
        for step_start, step_end, step in ranges:
            overlap = min(end, step_end) - max(start, step_start)
            if overlap <= 0.0:
                continue
            metres_by_class[step.road_class] = metres_by_class.get(step.road_class, 0.0) + overlap
            limit_by_class.setdefault(step.road_class, step.speed_limit_kmh)
            if step.duration_s > 0.0:
                weighted_speed += overlap * (step.distance_m / step.duration_s) * 3.6
                covered += overlap
        if not metres_by_class:
            projected.append((RoadClass.unknown, None, fallback_speed_kmh))
            continue
        dominant = max(metres_by_class.items(), key=lambda item: item[1])[0]
        speed = weighted_speed / covered if covered > 0.0 else fallback_speed_kmh
        projected.append((dominant, limit_by_class.get(dominant), speed))
    return projected


# ======================================================================================
# Corridor queries
# ======================================================================================


_WEATHER_SQL: Final[str] = """
WITH seg AS MATERIALIZED (
    {segments}
),
mid AS MATERIALIZED (
    SELECT ordinal, ST_LineInterpolatePoint(geom, 0.5)::geography AS point FROM seg
),
bounds AS MATERIALIZED (
    SELECT ST_Expand(ST_Extent(geom)::geometry, :padding)::geography AS box FROM seg
),
recent AS MATERIALIZED (
    SELECT DISTINCT ON (COALESCE(w.weather_station_id, w.id))
           w.id, w.observed_at, w.location, w.temperature_c, w.precipitation_mm,
           w.wind_speed_ms, w.wind_gust_ms, w.humidity_percent, w.pressure_hpa, w.condition,
           w.source, w.source_identifier, w.source_url, w.source_timestamp, w.data_origin,
           w.ingestion_run_id, w.ingested_at, ws.name AS station_name, run.provider_mode
    FROM weather_observations w
    CROSS JOIN bounds b
    LEFT JOIN weather_stations ws ON ws.id = w.weather_station_id
    LEFT JOIN data_ingestion_runs run ON run.id = w.ingestion_run_id
    WHERE w.location && b.box AND w.observed_at <= :at
    ORDER BY COALESCE(w.weather_station_id, w.id), w.observed_at DESC
)
SELECT mid.ordinal, o.id, o.observed_at, o.temperature_c, o.precipitation_mm, o.wind_speed_ms,
       o.wind_gust_ms, o.humidity_percent, o.pressure_hpa, o.condition, o.station_name,
       o.source, o.source_identifier, o.source_url, o.source_timestamp, o.data_origin,
       o.ingestion_run_id, o.ingested_at, o.provider_mode,
       ST_Y(o.location::geometry) AS latitude,
       ST_X(o.location::geometry) AS longitude,
       ST_Distance(o.location, mid.point) AS distance_m
FROM mid
LEFT JOIN LATERAL (
    SELECT r.* FROM recent r ORDER BY r.location <-> mid.point LIMIT 1
) o ON TRUE
ORDER BY mid.ordinal
"""
"""Nearest recent observation per segment midpoint, in one round trip.

``LATERAL`` + ``ORDER BY location <-> point LIMIT 1`` is PostGIS's index-backed KNN search: the
planner walks the GiST index outward from each midpoint and stops at the first hit, instead of
computing a distance to every observation. ``DISTINCT ON (station)`` collapses each station's
history to its freshest row first, so the KNN cannot return a stale reading from a nearby
station in preference to a fresh one a kilometre further out.
"""

_TRAFFIC_SQL: Final[str] = """
WITH seg AS MATERIALIZED (
    {segments}
),
bounds AS MATERIALIZED (
    SELECT ST_Expand(ST_Extent(geom)::geometry, :padding) AS box FROM seg
),
near AS MATERIALIZED (
    SELECT e.id, e.external_id, e.event_type, e.severity, e.road_name, e.direction, e.title,
           e.description, e.starts_at, e.ends_at, e.is_blocked, e.delay_minutes,
           e.source, e.source_identifier, e.source_url, e.source_timestamp, e.data_origin,
           e.ingestion_run_id, e.ingested_at, run.provider_mode,
           ST_Y(e.location::geometry) AS latitude,
           ST_X(e.location::geometry) AS longitude,
           ST_AsGeoJSON(ST_Simplify(e.geometry, :tolerance), 6) AS geometry_json,
           COALESCE(ST_Simplify(e.geometry, :match_tolerance), e.location::geometry) AS match_geom
    FROM traffic_events e
    CROSS JOIN bounds b
    LEFT JOIN data_ingestion_runs run ON run.id = e.ingestion_run_id
    WHERE COALESCE(e.geometry, e.location::geometry) && b.box
      AND (e.starts_at IS NULL OR e.starts_at <= :at)
      AND (e.ends_at IS NULL OR e.ends_at >= :at)
)
SELECT seg.ordinal, n.id, n.external_id, n.event_type, n.severity, n.road_name, n.direction,
       n.title, n.description, n.starts_at, n.ends_at, n.is_blocked, n.delay_minutes,
       n.source, n.source_identifier, n.source_url, n.source_timestamp, n.data_origin,
       n.ingestion_run_id, n.ingested_at, n.provider_mode, n.latitude, n.longitude,
       n.geometry_json
FROM seg
JOIN near n ON ST_DWithin(seg.geom::geography, n.match_geom::geography, :buffer_m)
ORDER BY seg.ordinal, n.id
"""
"""Traffic events matched to the segments they actually cover.

Two stages on purpose. The bounding-box test is a cheap index-friendly rejection that removes
the ~95 % of the federal feed nowhere near this corridor; only the survivors pay for the exact
geodesic ``ST_DWithin`` against each segment. Matching against the event's *line* rather than
its representative point is what makes a 14 km Baustelle raise the severity of all three
segments it covers.
"""

_CHARGING_SQL: Final[str] = """
WITH seg AS MATERIALIZED (
    {segments}
)
SELECT DISTINCT ON (st.id)
       st.id, st.external_id, st.operator, st.street, st.house_number, st.postal_code, st.city,
       st.bundesland, st.max_power_kw, st.total_power_kw, st.charging_points_count,
       st.charging_category, st.is_fast_charger,
       ST_Y(st.location::geometry) AS latitude,
       ST_X(st.location::geometry) AS longitude,
       seg.start_offset_m
           + ST_LineLocatePoint(seg.geom, st.location::geometry) * seg.distance_m AS offset_m,
       ST_Distance(seg.geom::geography, st.location) AS lateral_m
FROM seg
JOIN charging_stations st ON ST_DWithin(seg.geom::geography, st.location, :buffer_m)
WHERE st.max_power_kw >= :min_power_kw
ORDER BY st.id, ST_Distance(seg.geom::geography, st.location)
"""
"""Corridor charging sites with their offset along the route.

The offset is anchored to the *segment* the site is nearest to — ``start_offset_m`` plus the
fraction along that segment's geometry — rather than to a fraction of the whole route. Two
reasons: the segment offsets are already geodesically correct (the segmentation measured them
with haversine), and ``ST_LineLocatePoint`` measures a planar fraction in degrees, which over
5 km is exact to a few metres but over 200 km of a route that changes bearing is not.
"""


async def _load_segment_weather(
    session: AsyncSession,
    route: ResolvedRoute,
    *,
    at: datetime,
) -> tuple[dict[int, _SegmentWeather], ProviderMode]:
    """Attach the nearest recent observation to every segment midpoint in one query."""
    source = route.segment_source
    statement = sa.text(_WEATHER_SQL.format(segments=source.sql))
    rows = (
        await session.execute(
            statement,
            {**source.params, "padding": BBOX_PADDING_DEG, "at": at},
        )
    ).all()

    matches: dict[int, _SegmentWeather] = {}
    modes: list[ProviderMode] = []
    for row in rows:
        if row.id is None:
            continue
        observation = WeatherObservationOut(
            id=row.id,
            observed_at=row.observed_at,
            latitude=float(row.latitude),
            longitude=float(row.longitude),
            station_name=row.station_name,
            temperature_c=_optional_float(row.temperature_c),
            precipitation_mm=_optional_float(row.precipitation_mm),
            wind_speed_ms=_optional_float(row.wind_speed_ms),
            wind_gust_ms=_optional_float(row.wind_gust_ms),
            humidity_percent=_optional_float(row.humidity_percent),
            pressure_hpa=_optional_float(row.pressure_hpa),
            condition=WeatherCondition(row.condition),
            distance_km=float(row.distance_m) / 1000.0,
            provenance=_provenance(row),
        )
        matches[int(row.ordinal)] = _SegmentWeather(
            observation=observation,
            distance_km=float(row.distance_m) / 1000.0,
            age_hours=max(0.0, (at - row.observed_at).total_seconds() / 3600.0),
        )
        modes.append(_stored_mode(row.provider_mode))
    return matches, _weakest_mode(modes)


async def _load_segment_traffic(
    session: AsyncSession,
    route: ResolvedRoute,
    *,
    at: datetime,
    buffer_km: float,
) -> tuple[dict[int, list[TrafficEventOut]], ProviderMode]:
    """Match the active traffic events to the segments they cover, in one query."""
    source = route.segment_source
    statement = sa.text(_TRAFFIC_SQL.format(segments=source.sql))
    rows = (
        await session.execute(
            statement,
            {
                **source.params,
                "padding": BBOX_PADDING_DEG,
                "tolerance": GEOMETRY_TOLERANCE_DEG,
                "match_tolerance": MATCH_TOLERANCE_DEG,
                "at": at,
                "buffer_m": buffer_km * 1000.0,
            },
        )
    ).all()

    by_segment: dict[int, list[TrafficEventOut]] = {}
    modes: list[ProviderMode] = []
    cache: dict[UUID, TrafficEventOut] = {}
    for row in rows:
        event = cache.get(row.id)
        if event is None:
            event = TrafficEventOut(
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
                geometry=_parse_optional_linestring(row.geometry_json),
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                is_blocked=bool(row.is_blocked),
                delay_minutes=_optional_float(row.delay_minutes),
                provenance=_provenance(row),
            )
            cache[row.id] = event
            modes.append(_stored_mode(row.provider_mode))
        by_segment.setdefault(int(row.ordinal), []).append(event)
    return by_segment, _weakest_mode(modes)


async def _load_charging_candidates(
    session: AsyncSession,
    route: ResolvedRoute,
    *,
    buffer_km: float,
    min_power_kw: float,
) -> list[tuple[ChargingStationSummaryOut, float, float]]:
    """Corridor charging sites as ``(station, offset_km, lateral_km)``, ordered by offset."""
    source = route.segment_source
    statement = sa.text(_CHARGING_SQL.format(segments=source.sql))
    rows = (
        await session.execute(
            statement,
            {
                **source.params,
                "buffer_m": buffer_km * 1000.0,
                "min_power_kw": min_power_kw,
            },
        )
    ).all()

    candidates = [
        (
            ChargingStationSummaryOut(
                id=row.id,
                external_id=row.external_id,
                operator=row.operator,
                street=row.street,
                house_number=row.house_number,
                postal_code=row.postal_code,
                city=row.city,
                bundesland=Bundesland(row.bundesland) if row.bundesland else None,
                latitude=float(row.latitude),
                longitude=float(row.longitude),
                max_power_kw=_optional_float(row.max_power_kw),
                total_power_kw=_optional_float(row.total_power_kw),
                charging_points_count=int(row.charging_points_count),
                charging_category=ChargingCategory(row.charging_category),
                is_fast_charger=bool(row.is_fast_charger),
            ),
            max(0.0, float(row.offset_m) / 1000.0),
            float(row.lateral_m) / 1000.0,
        )
        for row in rows
    ]
    candidates.sort(key=lambda item: (item[1], str(item[0].id)))
    return candidates


# ======================================================================================
# The analysis
# ======================================================================================


async def analyse_route(
    session: AsyncSession,
    request: RouteAnalyzeRequest,
    *,
    routing: RoutingProvider,
    geocoding: GeocodingProvider,
) -> RouteAnalysisResult:
    """Run the full BUILD_SPEC §7.3 analysis for one request.

    Raises:
        NotFoundError: Unknown corridor, or a place that could not be geocoded.
        ValidationError: Unknown ``vehicle_code``.
        ProviderError: The routing engine or geocoder failed with no fallback available.
    """
    profile = _vehicle_profile(request.vehicle_code)
    at = request.at or utc_now()

    route = await resolve_route(session, request, routing=routing, geocoding=geocoding)
    weather, weather_mode = await _load_segment_weather(session, route, at=at)
    traffic, traffic_mode = await _load_segment_traffic(
        session,
        route,
        at=at,
        buffer_km=request.traffic_buffer_km,
    )
    for mode in (route.routing_mode, weather_mode, traffic_mode):
        set_data_mode(mode)

    usable_weather = {
        ordinal: match
        for ordinal, match in weather.items()
        if match.is_usable(
            radius_km=request.weather_radius_km,
            max_age_hours=request.weather_max_age_hours,
        )
    }
    temperatures = [
        match.observation.temperature_c
        for match in usable_weather.values()
        if match.observation.temperature_c is not None
    ]
    mean_temperature_c = (
        sum(temperatures) / len(temperatures) if temperatures else REFERENCE_TEMPERATURE_C
    )

    conditions = tuple(
        _segment_conditions(
            segment,
            weather=usable_weather.get(segment.ordinal),
            events=traffic.get(segment.ordinal, ()),
            fallback_temperature_c=mean_temperature_c,
        )
        for segment in route.segments
    )

    model = PhysicalEnergyModel()
    physical = tuple(model.segment_energy(profile, condition) for condition in conditions)
    predictor = await _load_predictor()
    soc_percent = _mean_soc_percent(profile, physical, request.start_soc_percent)
    energies = _integrate_energy(
        profile,
        conditions,
        physical,
        predictor,
        soc_percent=soc_percent,
    )

    assumptions = _assumptions(
        route=route,
        usable_weather=usable_weather,
        temperatures=temperatures,
        predictor=predictor,
        soc_percent=soc_percent,
    )
    response = _build_response(
        request=request,
        route=route,
        profile=profile,
        conditions=conditions,
        physical=physical,
        energies=energies,
        weather=usable_weather,
        traffic=traffic,
        model=model,
        predictor=predictor,
        weather_mode=weather_mode,
        traffic_mode=traffic_mode,
        assumptions=assumptions,
        generated_at=at,
    )
    energy_segments = tuple(
        RouteEnergySegment(
            start_offset_km=segment.start_offset_m / 1000.0,
            distance_km=segment.distance_m / 1000.0,
            duration_s=condition.effective_duration_s,
            energy_kwh=energy.kwh,
        )
        for segment, condition, energy in zip(route.segments, conditions, energies, strict=True)
    )
    return RouteAnalysisResult(
        response=response,
        route=route,
        profile=profile,
        energy_segments=energy_segments,
        mean_temperature_c=mean_temperature_c,
        energy_deficit_kwh=response.energy_deficit_kwh,
    )


def _segment_conditions(
    segment: AnalysisSegment,
    *,
    weather: _SegmentWeather | None,
    events: Sequence[TrafficEventOut],
    fallback_temperature_c: float,
) -> SegmentConditions:
    """Build the physics input for one segment.

    ``speed_kmh`` is the traffic-slowed speed and ``traffic_severity`` is the severity that
    slowed it. Both are set, and that is not redundant: the physical model uses the speed, while
    :attr:`~autotwin_ml.types.SegmentConditions.free_flow_speed_kmh` multiplies the speed back up
    by the same severity's delay factor to build the no-traffic counterfactual. Setting one
    without the other would make the counterfactual silently wrong.
    """
    severity = _worst_severity(events)
    free_flow = segment.free_flow_speed_kmh
    assumed = max(_MIN_ANALYSIS_SPEED_KMH, free_flow / severity.delay_factor)
    observation = weather.observation if weather is not None else None
    temperature = (
        observation.temperature_c
        if observation is not None and observation.temperature_c is not None
        else fallback_temperature_c
    )
    return SegmentConditions(
        speed_kmh=assumed,
        distance_km=segment.distance_m / 1000.0,
        # No elevation source is ingested, so every segment is modelled as flat and the
        # `gradient` driver of the explanation is always zero. Reported in `assumptions`.
        gradient_percent=0.0,
        outside_temperature_c=temperature,
        battery_temperature_c=None,
        acceleration_ms2=0.0,
        traffic_severity=severity,
        precipitation_mm=(
            observation.precipitation_mm
            if observation is not None and observation.precipitation_mm is not None
            else 0.0
        ),
        wind_speed_ms=(
            observation.wind_speed_ms
            if observation is not None and observation.wind_speed_ms is not None
            else 0.0
        ),
        # Wind direction is not in the DWD rows AutoTwin ingests, so the headwind component
        # cannot be derived. Zero is the only honest value; the scalar wind speed still reaches
        # the ML model as a feature.
        headwind_ms=0.0,
        road_class=segment.road_class,
        speed_limit_kmh=segment.speed_limit_kmh,
        duration_s=None,
    )


async def _load_predictor() -> EnergyPredictor | None:
    """Load the active ML model, or ``None`` when nothing is trained.

    Degrading is the whole point: an untrained deployment must still analyse routes on the
    physical baseline, and the response says ``model_name: null`` so the UI can state it. Loading
    runs in a worker thread because deserialising a booster is blocking filesystem work; the
    registry caches it, and the application warms it at startup.
    """
    try:
        model = await asyncio.to_thread(get_active_model)
    except ModelNotAvailable as exc:
        _LOGGER.info("routes.analyze.model_unavailable", detail=str(exc))
        return None
    return EnergyPredictor(model)


def _mean_soc_percent(
    profile: VehicleProfile,
    physical: Sequence[EnergyResult],
    start_soc_percent: float,
) -> float:
    """State of charge to serve the model's ``soc_percent`` feature with.

    The feature is one number per prediction, but a trip's SOC falls continuously, so something
    has to be chosen. Predicting each segment at its *own* running SOC would be the obvious
    answer and is circular — the running SOC is derived from the predictions. The mean of the
    start SOC and the arrival SOC the **physical** model implies breaks that circle: it is
    available before any prediction is made, and it is much closer to the trip than the
    predictor's fixed default would be.
    """
    total_kwh = sum(result.kwh for result in physical)
    arrival = start_soc_percent - total_kwh / profile.usable_capacity_kwh * _PERCENT
    return min(_PERCENT, max(0.0, (start_soc_percent + max(0.0, arrival)) / 2.0))


def _integrate_energy(
    profile: VehicleProfile,
    conditions: Sequence[SegmentConditions],
    physical: Sequence[EnergyResult],
    predictor: EnergyPredictor | None,
    *,
    soc_percent: float,
) -> tuple[EnergyResult, ...]:
    """Per-segment energy as the analysis reports it: ML where available, physics otherwise.

    When a model is active its per-segment consumption replaces the physical total, but the
    physical *decomposition* is scaled to match rather than discarded. That keeps the additive
    identity of :class:`~autotwin_ml.types.EnergyResult` intact — which the explanation ladder
    relies on — and it makes the ML-versus-physics difference surface as the explanation's
    ``model_correction`` driver instead of vanishing.

    The whole route goes through the model in **one** call: a 400 km corridor is 79 segments,
    and 79 separate calls into a boosted ensemble cost roughly an order of magnitude more than
    one call with a 79-row matrix.
    """
    if predictor is None:
        return tuple(physical)
    predictions = predictor.predict_segments(profile, list(conditions), soc_percent=soc_percent)
    rescaled: list[EnergyResult] = []
    for condition, baseline, prediction in zip(conditions, physical, predictions, strict=True):
        rate = max(0.0, prediction.prediction_kwh_100km)
        target = rate * condition.distance_km / _PERCENT
        rescaled.append(_rescale(baseline, target))
    return tuple(rescaled)


def _rescale(result: EnergyResult, target_kwh: float) -> EnergyResult:
    """Scale a decomposition to a new total, preserving the additive identity.

    A zero-energy segment cannot be scaled (there is nothing to scale), so it is returned
    unchanged; that only happens for a segment with no time on it, which costs nothing either
    way.
    """
    if result.kwh <= 0.0:
        return result
    factor = target_kwh / result.kwh
    return EnergyResult(
        kwh=target_kwh,
        distance_km=result.distance_km,
        duration_s=result.duration_s,
        rolling_kwh=result.rolling_kwh * factor,
        aero_kwh=result.aero_kwh * factor,
        gradient_kwh=result.gradient_kwh * factor,
        inertia_kwh=result.inertia_kwh * factor,
        auxiliary_kwh=result.auxiliary_kwh * factor,
        hvac_kwh=result.hvac_kwh * factor,
        regen_kwh=result.regen_kwh * factor,
    )


def _counterfactual_kwh(
    model: PhysicalEnergyModel,
    profile: VehicleProfile,
    conditions: Sequence[SegmentConditions],
    *,
    neutralise_weather: bool,
    neutralise_traffic: bool,
) -> float:
    """Re-run the physical model with some conditions switched off, and total the energy.

    This is what makes ``weather_penalty_percent`` and ``traffic_penalty_percent`` attributions
    rather than guesses: each is the measured difference between two full runs of the same
    model over the same segments, differing only in the conditions named here.
    """
    total = 0.0
    for condition in conditions:
        altered = condition
        if neutralise_traffic:
            altered = replace(
                altered,
                speed_kmh=altered.free_flow_speed_kmh,
                traffic_severity=TrafficSeverity.low,
                duration_s=None,
            )
        if neutralise_weather:
            altered = replace(
                altered,
                outside_temperature_c=REFERENCE_TEMPERATURE_C,
                battery_temperature_c=REFERENCE_TEMPERATURE_C,
                precipitation_mm=0.0,
                wind_speed_ms=0.0,
                headwind_ms=0.0,
            )
        total += model.segment_energy(profile, altered).kwh
    return total


def _build_response(
    *,
    request: RouteAnalyzeRequest,
    route: ResolvedRoute,
    profile: VehicleProfile,
    conditions: Sequence[SegmentConditions],
    physical: Sequence[EnergyResult],
    energies: Sequence[EnergyResult],
    weather: Mapping[int, _SegmentWeather],
    traffic: Mapping[int, list[TrafficEventOut]],
    model: PhysicalEnergyModel,
    predictor: EnergyPredictor | None,
    weather_mode: ProviderMode,
    traffic_mode: ProviderMode,
    assumptions: Sequence[str],
    generated_at: datetime,
) -> RouteAnalysisOut:
    """Assemble the §7.3 payload from the analysed segments."""
    usable_kwh = profile.usable_capacity_kwh
    distance_km = sum(segment.distance_m for segment in route.segments) / 1000.0
    total_kwh = sum(energy.kwh for energy in energies)
    physical_kwh = sum(energy.kwh for energy in physical)
    duration_s = sum(condition.effective_duration_s for condition in conditions)

    segment_rows: list[RouteSegmentAnalysisOut] = []
    soc = request.start_soc_percent
    for segment, condition, baseline, energy in zip(
        route.segments, conditions, physical, energies, strict=True
    ):
        soc -= energy.kwh / usable_kwh * _PERCENT
        events = traffic.get(segment.ordinal, [])
        segment_rows.append(
            RouteSegmentAnalysisOut(
                ordinal=segment.ordinal,
                start_offset_km=segment.start_offset_m / 1000.0,
                distance_km=segment.distance_m / 1000.0,
                road_class=segment.road_class,
                speed_limit_kmh=segment.speed_limit_kmh,
                free_flow_speed_kmh=segment.free_flow_speed_kmh,
                assumed_speed_kmh=condition.speed_kmh,
                duration_s=condition.effective_duration_s,
                temperature_c=condition.outside_temperature_c,
                traffic_severity=_worst_severity(events) if events else None,
                traffic_event_count=len(events),
                kwh=energy.kwh,
                kwh_per_100km=energy.kwh_per_100km,
                baseline_kwh_per_100km=baseline.kwh_per_100km,
                soc_at_end_percent=min(_PERCENT, max(0.0, soc)),
                energy_intensity=EnergyIntensity.from_ratio(
                    energy.kwh_per_100km,
                    profile.nominal_consumption_kwh_100km,
                ),
                geometry=segment.geometry,
            )
        )

    arrival_soc = request.start_soc_percent - total_kwh / usable_kwh * _PERCENT
    required_kwh = (request.min_arrival_soc_percent - arrival_soc) / _PERCENT * usable_kwh
    deficit_kwh = max(0.0, required_kwh)

    free_flow_kwh = _counterfactual_kwh(
        model,
        profile,
        conditions,
        neutralise_weather=False,
        neutralise_traffic=True,
    )
    mild_kwh = _counterfactual_kwh(
        model,
        profile,
        conditions,
        neutralise_weather=True,
        neutralise_traffic=False,
    )
    neutral_kwh = _counterfactual_kwh(
        model,
        profile,
        conditions,
        neutralise_weather=True,
        neutralise_traffic=True,
    )

    explanation = build_explanation(
        profile,
        list(conditions),
        list(energies),
        weather=_weather_context(weather),
        traffic=_traffic_context(traffic),
        model=model,
    )
    seen: set[UUID] = set()
    events_in_order: list[TrafficEventOut] = []
    for ordinal in sorted(traffic):
        for event in traffic[ordinal]:
            if event.id not in seen:
                seen.add(event.id)
                events_in_order.append(event)

    return RouteAnalysisOut(
        route=RouteAnalysisRouteOut(
            id=route.id,
            slug=route.slug,
            name=route.name,
            distance_m=route.distance_m,
            duration_s=route.duration_s,
            geometry=route.geometry,
            origin=RouteEndpointOut(
                name=route.origin_name,
                latitude=route.origin.latitude,
                longitude=route.origin.longitude,
            ),
            destination=RouteEndpointOut(
                name=route.destination_name,
                latitude=route.destination.latitude,
                longitude=route.destination.longitude,
            ),
            is_demo=route.is_demo,
        ),
        vehicle=_vehicle_out(profile),
        start_soc_percent=request.start_soc_percent,
        arrival_soc_percent=min(_PERCENT, max(0.0, arrival_soc)),
        min_soc_percent_required=request.min_arrival_soc_percent,
        energy_kwh_total=total_kwh,
        energy_deficit_kwh=deficit_kwh,
        estimated_duration_s=duration_s,
        avg_consumption_kwh_100km=_per_100km(total_kwh, distance_km),
        baseline_kwh_100km=_per_100km(physical_kwh, distance_km),
        model_kwh_100km=_per_100km(total_kwh, distance_km) if predictor is not None else None,
        model_name=predictor.name if predictor is not None else None,
        model_version=predictor.version if predictor is not None else None,
        weather_penalty_percent=_penalty_percent(physical_kwh, mild_kwh),
        traffic_penalty_percent=_penalty_percent(physical_kwh, free_flow_kwh),
        total_penalty_percent=_penalty_percent(physical_kwh, neutral_kwh),
        charging_required=deficit_kwh > 0.0,
        segments=segment_rows,
        traffic_events=events_in_order,
        weather_points=_weather_points(weather),
        explanation=_explanation_out(explanation),
        data_modes=DataModesOut(
            routing=route.routing_mode,
            weather=weather_mode,
            traffic=traffic_mode,
        ),
        assumptions=list(assumptions),
        generated_at=generated_at,
    )


def _assumptions(
    *,
    route: ResolvedRoute,
    usable_weather: Mapping[int, _SegmentWeather],
    temperatures: Sequence[float],
    predictor: EnergyPredictor | None,
    soc_percent: float,
) -> list[str]:
    """Modelling gaps that apply to *this* answer, in the order a reader should hear them.

    Written out per response rather than as static documentation because which gaps bite
    depends on the data: a corridor with three DWD stations in range is in a different position
    from one with none, and the page should be able to say which it is looking at.
    """
    notes: list[str] = [
        "No elevation data is ingested, so every segment is modelled as flat "
        "(gradient_percent = 0) and the gradient driver of the explanation is always zero.",
        "Wind direction is not published in the observations AutoTwin ingests, so the headwind "
        "component is zero; the scalar wind speed still reaches the ML model as a feature.",
    ]
    covered = len(usable_weather)
    total = len(route.segments)
    if covered == 0:
        notes.append(
            f"No weather observation was in range of this corridor, so all {total} segments "
            f"were modelled at the {REFERENCE_TEMPERATURE_C:.0f} °C reference temperature."
        )
    elif covered < total:
        notes.append(
            f"{total - covered} of {total} segments had no observation in range and adopted the "
            "corridor mean temperature."
        )
    if not temperatures:
        notes.append(
            "The observations found report no temperature, so the reference temperature was "
            "used for the physics."
        )
    if predictor is None:
        notes.append(
            "No ML model is active; the analysis runs on the physical road-load model alone "
            "(train one with `python -m autotwin_ml.cli train`)."
        )
    else:
        notes.append(
            f"The model's soc_percent feature is held at the trip's mean state of charge "
            f"({soc_percent:.0f} %) for every segment; predicting each segment at its own "
            "running SOC would make the prediction depend on itself."
        )
    notes.extend(route.warnings)
    return notes


# ======================================================================================
# Charging optimisation
# ======================================================================================


async def plan_charging(
    session: AsyncSession,
    request: ChargingOptimizeRequest,
    *,
    routing: RoutingProvider,
    geocoding: GeocodingProvider,
) -> ChargingPlanOut:
    """Analyse the route, pick corridor candidates and run the beam search (BUILD_SPEC §10.3)."""
    analysis = await analyse_route(session, request, routing=routing, geocoding=geocoding)
    corridor = await _load_charging_candidates(
        session,
        analysis.route,
        buffer_km=request.corridor_buffer_km,
        min_power_kw=request.min_power_kw,
    )
    selected = _thin_candidates(corridor, spacing_km=request.candidate_spacing_km)
    stations = {station.id: station for station, _, _ in selected}
    candidates = [
        ChargingCandidate(
            station_id=str(station.id),
            name=_station_label(station),
            coordinate=Coordinate(latitude=station.latitude, longitude=station.longitude),
            offset_km=offset_km,
            # A detour leaves the corridor and comes back, so the lateral distance counts twice.
            detour_km=2.0 * lateral_km,
            max_power_kw=station.max_power_kw or 0.0,
            operator=station.operator,
            charging_category=station.charging_category,
        )
        for station, offset_km, lateral_km in selected
        if station.max_power_kw
    ]

    plan = optimise_charging(
        analysis.energy_segments,
        analysis.profile,
        request.start_soc_percent,
        request.min_arrival_soc_percent,
        candidates,
        target_arrival_soc=request.min_arrival_soc_percent,
        battery_temp_c=analysis.mean_temperature_c,
        max_stops=request.max_stops,
    )
    return _plan_out(plan, stations=stations, analysis=analysis, candidates=len(candidates))


def _thin_candidates(
    corridor: Sequence[tuple[ChargingStationSummaryOut, float, float]],
    *,
    spacing_km: float,
) -> list[tuple[ChargingStationSummaryOut, float, float]]:
    """Keep the single best site per stretch of corridor.

    A 200 km German Autobahn corridor holds ~900 fast-charging sites; handing all of them to a
    beam search multiplies the state space without changing the answer, because two ultra-fast
    sites 400 m apart are one decision. "Best" is the strongest connector, then the shortest
    detour, then the id — a total order, so the selection is deterministic.
    """
    best: dict[int, tuple[ChargingStationSummaryOut, float, float]] = {}
    for entry in corridor:
        station, offset_km, lateral_km = entry
        bucket = int(offset_km // spacing_km)
        current = best.get(bucket)
        key = (-(station.max_power_kw or 0.0), lateral_km, str(station.id))
        if current is None:
            best[bucket] = entry
            continue
        current_key = (-(current[0].max_power_kw or 0.0), current[2], str(current[0].id))
        if key < current_key:
            best[bucket] = entry
    selected = [best[bucket] for bucket in sorted(best)]
    if len(selected) <= _MAX_CHARGING_CANDIDATES:
        return selected
    # Too long a corridor for the spacing chosen: keep the strongest sites, then restore order.
    strongest = sorted(
        selected,
        key=lambda item: (-(item[0].max_power_kw or 0.0), item[2], str(item[0].id)),
    )[:_MAX_CHARGING_CANDIDATES]
    strongest.sort(key=lambda item: (item[1], str(item[0].id)))
    return strongest


def _plan_out(
    plan: ChargingPlan,
    *,
    stations: Mapping[UUID, ChargingStationSummaryOut],
    analysis: RouteAnalysisResult,
    candidates: int,
) -> ChargingPlanOut:
    """Render the optimiser's plan, resolving each stop back to its full station row."""
    stops = [
        ChargingStopOut(
            station=stations[UUID(stop.candidate.station_id)],
            arrival_soc_percent=stop.arrival_soc_percent,
            departure_soc_percent=stop.departure_soc_percent,
            detour_km=stop.detour_km,
            charge_time_min=stop.charge_time_min,
            energy_added_kwh=stop.energy_added_kwh,
            max_power_kw=stop.max_power_kw,
            avg_power_kw=stop.avg_power_kw,
            offset_km=stop.offset_km,
            rationale_de=stop.rationale_de,
            rationale_en=stop.rationale_en,
        )
        for stop in plan.stops
    ]
    return ChargingPlanOut(
        feasible=plan.feasible,
        reason=plan.reason,
        reason_de=plan.reason_de,
        reason_en=plan.reason_en,
        stops=stops,
        total_time_min=plan.total_time_min,
        driving_time_min=plan.driving_time_min,
        charging_time_min=plan.charging_time_min,
        detour_km_total=plan.detour_km_total,
        detour_time_min=plan.detour_time_min,
        arrival_soc_percent=plan.arrival_soc_percent,
        min_soc_percent_reached=plan.min_soc_percent_reached,
        alternatives_considered=plan.alternatives_considered,
        candidates_considered=candidates,
        objective=plan.objective,
        objective_value=plan.objective_value,
        analysis=analysis.response,
    )


# ======================================================================================
# Small helpers
# ======================================================================================


def _vehicle_profile(code: str) -> VehicleProfile:
    """Look up a vehicle profile, turning an unknown code into a 422 that lists the known ones."""
    try:
        return get_vehicle_profile(code)
    except KeyError as exc:
        # `str(KeyError)` is the *repr* of its argument, so it would arrive at the client
        # wrapped in quotes. The raw argument is the sentence the lookup wrote for a human.
        raise ValidationError(str(exc.args[0]), details={"field": "vehicle_code"}) from exc


def _vehicle_out(profile: VehicleProfile) -> VehicleProfileOut:
    """Adapt the shared vehicle profile onto the wire type."""
    return VehicleProfileOut(
        code=profile.code,
        display_name=profile.display_name,
        vehicle_class=profile.vehicle_class,
        battery_capacity_kwh=profile.battery_capacity_kwh,
        usable_capacity_kwh=profile.usable_capacity_kwh,
        nominal_consumption_kwh_100km=profile.nominal_consumption_kwh_100km,
        max_dc_power_kw=profile.max_dc_power_kw,
        max_ac_power_kw=profile.max_ac_power_kw,
        mass_kg=profile.mass_kg,
        drag_area_m2=profile.drag_area,
    )


def _explanation_out(explanation: EnergyExplanation) -> EnergyExplanationOut:
    """Adapt the insight generator's result onto the wire type."""
    return EnergyExplanationOut(
        headline=explanation.headline,
        headline_de=explanation.headline_de,
        headline_en=explanation.headline_en,
        drivers=[
            EnergyDriverOut(
                factor=driver.factor,
                delta_percent=driver.delta_percent,
                direction=driver.direction.value,
                label_de=driver.label_de,
                label_en=driver.label_en,
                delta_kwh=driver.delta_kwh,
                detail_de=driver.detail_de,
                detail_en=driver.detail_en,
            )
            for driver in explanation.drivers
        ],
        nominal_kwh_100km=explanation.nominal_kwh_100km,
        actual_kwh_100km=explanation.actual_kwh_100km,
        total_delta_percent=explanation.total_delta_percent,
        method=explanation.method,
    )


def _weather_context(weather: Mapping[int, _SegmentWeather]) -> WeatherContext:
    """Summarise the corridor's weather for the explanation's detail text."""
    observations = _weather_points(weather)
    temperatures = [o.temperature_c for o in observations if o.temperature_c is not None]
    precipitation = [o.precipitation_mm for o in observations if o.precipitation_mm is not None]
    winds = [o.wind_speed_ms for o in observations if o.wind_speed_ms is not None]
    conditions = [o.condition for o in observations if o.condition is not WeatherCondition.unknown]
    return WeatherContext(
        mean_temperature_c=(sum(temperatures) / len(temperatures)) if temperatures else None,
        min_temperature_c=min(temperatures) if temperatures else None,
        precipitation_mm=sum(precipitation),
        mean_wind_speed_ms=(sum(winds) / len(winds)) if winds else 0.0,
        condition=_dominant_condition(conditions),
    )


def _dominant_condition(conditions: Sequence[WeatherCondition]) -> WeatherCondition:
    """The most frequently observed condition along the corridor.

    Ties break on the enum's declaration order rather than on set iteration order, so two runs
    of the same analysis report the same weather. Hash randomisation would otherwise make a
    corridor with equal rain and cloud observations flip between processes.
    """
    if not conditions:
        return WeatherCondition.unknown
    order = list(WeatherCondition)
    return min(conditions, key=lambda value: (-conditions.count(value), order.index(value)))


def _traffic_context(traffic: Mapping[int, list[TrafficEventOut]]) -> TrafficContext:
    """Summarise the corridor's traffic for the explanation's detail text."""
    events = {event.id: event for bucket in traffic.values() for event in bucket}
    if not events:
        return TrafficContext()
    return TrafficContext(
        event_count=len(events),
        worst_severity=max((e.severity for e in events.values()), key=lambda s: s.ordinal),
        blocked_count=sum(1 for event in events.values() if event.is_blocked),
    )


def _weather_points(weather: Mapping[int, _SegmentWeather]) -> list[WeatherObservationOut]:
    """The distinct observations the segments adopted, in first-use order along the route."""
    seen: set[UUID] = set()
    points: list[WeatherObservationOut] = []
    for ordinal in sorted(weather):
        observation = weather[ordinal].observation
        if observation.id not in seen:
            seen.add(observation.id)
            points.append(observation)
    return points


def _worst_severity(events: Sequence[TrafficEventOut]) -> TrafficSeverity:
    """Highest severity among the events on a segment; ``low`` when the segment is clear.

    ``low`` rather than ``None`` because it is the identity of the delay model: its delay factor
    is 1.0, so a clear segment is driven at free-flow speed and the traffic counterfactual is
    exactly the actual.
    """
    if not events:
        return TrafficSeverity.low
    return max((event.severity for event in events), key=lambda severity: severity.ordinal)


def _free_flow_speed(
    *,
    assumed_speed_kmh: float | None,
    road_class: RoadClass,
    speed_limit_kmh: float | None,
) -> float:
    """Free-flow speed for a segment, in km/h.

    The routing engine's own figure is preferred: it already reflects the geometry, the ramps
    and the curvature of this particular stretch. Only when the engine says nothing does the
    road class decide, capped by the posted limit — that is the ``f(road_class, speed_limit)``
    of BUILD_SPEC §7.3's assumed-speed rule, and it is the fallback rather than the default
    because a table cannot know that the first 5 km out of Frankfurt average 36 km/h.
    """
    if assumed_speed_kmh is not None and assumed_speed_kmh > 0.0:
        return max(_MIN_ANALYSIS_SPEED_KMH, assumed_speed_kmh)
    nominal = default_speed_limit_kmh(road_class)
    if speed_limit_kmh is not None and speed_limit_kmh > 0.0:
        nominal = min(nominal, speed_limit_kmh)
    return max(_MIN_ANALYSIS_SPEED_KMH, nominal)


def _per_100km(kwh: float, distance_km: float) -> float:
    """Consumption in kWh/100 km, or ``0.0`` for a zero-length route."""
    if distance_km <= 0.0:
        return 0.0
    return kwh / distance_km * _PERCENT


def _penalty_percent(actual_kwh: float, counterfactual_kwh: float) -> float:
    """How much more (or less) the trip costs than its counterfactual, in percent."""
    if counterfactual_kwh <= 0.0:
        return 0.0
    return (actual_kwh - counterfactual_kwh) / counterfactual_kwh * _PERCENT


def _station_label(station: ChargingStationSummaryOut) -> str:
    """Human label for a stop's rationale text, e.g. ``"EnBW (Heilbronn)"``."""
    parts = [part for part in (station.operator, station.city) if part]
    if not parts:
        return station.external_id
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} ({parts[1]})"


def _stored_mode(provider_mode: str | None) -> ProviderMode:
    """Honest data mode for a row read out of the database rather than fetched now.

    A stored row is at best a *cached* copy of a live answer — it was not fetched during this
    request, so reporting ``live`` would overstate its freshness. A row whose ingestion run ran
    against a fixture stays ``fixture``, because no amount of storing turns bundled test data
    into real German data.
    """
    if provider_mode == ProviderMode.fixture.value:
        return ProviderMode.fixture
    return ProviderMode.cache


def _weakest_mode(modes: Sequence[ProviderMode]) -> ProviderMode:
    """The least fresh of several modes; ``fixture`` when nothing was found at all.

    An empty corridor is reported as ``fixture`` rather than ``live`` on purpose: no rows means
    no evidence of freshness, and the header should not claim more than the data supports.
    """
    if not modes:
        return ProviderMode.fixture
    severity = {ProviderMode.live: 0, ProviderMode.cache: 1, ProviderMode.fixture: 2}
    return max(modes, key=lambda mode: severity[mode])


def _optional_float(value: Any) -> float | None:
    """Coerce a nullable numeric column to ``float | None``, dropping NaN as missing."""
    if value is None:
        return None
    number = float(value)
    return None if math.isnan(number) else number


def _provenance(row: Any) -> ProvenanceOut:
    """Build the provenance block from a raw-SQL row carrying the §3.1 columns."""
    return ProvenanceOut(
        source=SourceSystem(row.source),
        source_identifier=row.source_identifier,
        source_url=row.source_url,
        source_timestamp=row.source_timestamp,
        data_origin=DataOrigin(row.data_origin),
        ingestion_run_id=row.ingestion_run_id,
        ingested_at=row.ingested_at,
    )


def _parse_linestring(geojson: str | None) -> GeoJSONLineString:
    """Parse ``ST_AsGeoJSON`` output into the transport type.

    Raises:
        ValidationError: The column held no geometry. A route without geometry is a broken row,
            not an empty result, and it must not be rendered as an empty map.
    """
    line = _parse_optional_linestring(geojson)
    if line is None:
        msg = "route geometry is missing or is not a LineString"
        raise ValidationError(msg, details={"field": "geometry"})
    return line


def _parse_optional_linestring(geojson: str | None) -> GeoJSONLineString | None:
    """Parse ``ST_AsGeoJSON`` output, tolerating NULL and non-LineString geometry."""
    if not geojson:
        return None
    payload = json.loads(geojson)
    if payload.get("type") != "LineString" or len(payload.get("coordinates", ())) < 2:
        return None
    return GeoJSONLineString.model_validate(payload)


def _linestring_wkt(geometry: GeoJSONLineString | None) -> str:
    """Render a transport geometry back to WKT for a PostGIS parameter.

    Only used on the free-text path, where the geometry was produced in this process and has to
    be handed to PostGIS for the corridor queries. Six decimals is ~0.1 m — far below the
    accuracy of the underlying road network.
    """
    if geometry is None:
        msg = "cannot match a segment without geometry against the corridor"
        raise ValidationError(msg, details={"field": "geometry"})
    points = ", ".join(f"{lon:.6f} {lat:.6f}" for lon, lat in geometry.coordinates)
    return f"LINESTRING({points})"
