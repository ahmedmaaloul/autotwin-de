"""The five demo corridors → ``routes`` + ``route_segments``.

These are the corridors every screenshot, every route analysis and every simulated trip runs
on, so they are seeded rather than typed in: the endpoints go through the
:class:`~autotwin_core.providers.base.GeocodingProvider`, the geometry through the
:class:`~autotwin_core.providers.base.RoutingProvider`, and the result is cut into analysis
segments by :func:`~autotwin_core.geo.segmentation.segment_polyline`.

``data_origin`` is **derived**, never ``official``: OpenStreetMap published the road network,
OSRM computed a path through it, and the path is AutoTwin's own derivation. The provenance
block records OSRM as the source and the request URL as ``source_url``, so the corridor can be
recomputed exactly.

**Per-segment attributes come from OSRM's own steps**, not from a guess. Each step carries a
distance, a duration and a road reference; the pipeline lays the steps out along the route as
offset ranges and, for each segment, takes the road class covering the most metres and the
distance-weighted mean of the steps' free-flow speeds. What the source cannot answer is left
``NULL`` — elevation, temperature, traffic severity and the predicted consumption are filled in
per request by ``POST /api/v1/routes/analyze``, because they depend on *when* you drive.

**Idempotent on ``slug``.** Re-seeding updates the route in place and replaces its segments;
the route id is stable, so a trip or a prediction that references a corridor keeps referencing
it. A corridor whose routing fails is reported and skipped — one unreachable engine must not
cost the other four corridors.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

import polars as pl
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_contracts import (
    Coordinate,
    DataOrigin,
    Place,
    ProvenanceInfo,
    ProviderMode,
    RoadClass,
    RouteResult,
    RouteStepRecord,
    SourceSystem,
    utc_now,
)
from autotwin_core.db import Route, RouteSegment, session_scope
from autotwin_core.db.types import to_shape_linestring, to_shape_point
from autotwin_core.errors import ProviderError
from autotwin_core.geo import PolylineSegment, segment_polyline
from autotwin_core.logging import get_logger
from autotwin_core.providers import GeocodingProvider, RoutingProvider, close_provider
from autotwin_ingestion.lake import LakeZone
from autotwin_ingestion.pipelines.base import (
    IngestionPipeline,
    PipelineOutcome,
    RunContext,
    provenance_columns,
)
from autotwin_ingestion.providers.geocoding import NominatimGeocodingProvider
from autotwin_ingestion.providers.routing import DEFAULT_PROFILE, OSRMRoutingProvider

__all__ = [
    "DEMO_CORRIDORS",
    "SILVER_DATASET",
    "SILVER_SCHEMA",
    "DemoCorridor",
    "RouteSeedPipeline",
]

_logger = get_logger(__name__)

SILVER_DATASET: Final[str] = "route_segments"
"""Silver dataset: one row per seeded segment, for corridor analysis in the notebook."""

SILVER_SCHEMA: Final[dict[str, pl.DataType]] = {
    "route_id": pl.String(),
    "slug": pl.String(),
    "ordinal": pl.Int32(),
    "start_offset_m": pl.Float64(),
    "distance_m": pl.Float64(),
    "midpoint_latitude": pl.Float64(),
    "midpoint_longitude": pl.Float64(),
    "road_class": pl.String(),
    "speed_limit_kmh": pl.Float64(),
    "assumed_speed_kmh": pl.Float64(),
    "data_origin": pl.String(),
}
"""Column types of the silver dataset, stated rather than inferred.

``speed_limit_kmh`` is null for every unrestricted Autobahn stretch, which on the flagship
corridor is most of it — exactly the case where inference produces a ``Null`` column.
"""


@dataclass(frozen=True, slots=True)
class DemoCorridor:
    """One demo corridor of BUILD_SPEC §8, as it is seeded."""

    slug: str
    """Stable key, e.g. ``frankfurt-stuttgart``. The ``routes.slug`` unique constraint."""

    name: str
    """Human label shown in the route picker."""

    origin_query: str
    """Geocoder query for the start, e.g. ``Frankfurt am Main``."""

    destination_query: str
    """Geocoder query for the destination."""

    origin_fallback: Coordinate
    """Start point used when the geocoder answers nothing.

    These are the coordinates the bundled Nominatim snapshot returns for the same query, so an
    offline seed produces the same corridor as an online one rather than no corridor at all.
    """

    destination_fallback: Coordinate
    """Destination point under the same rule."""


DEMO_CORRIDORS: Final[tuple[DemoCorridor, ...]] = (
    DemoCorridor(
        slug="frankfurt-stuttgart",
        name="Frankfurt am Main → Stuttgart",
        origin_query="Frankfurt am Main",
        destination_query="Stuttgart",
        origin_fallback=Coordinate(latitude=50.1109, longitude=8.6821),
        destination_fallback=Coordinate(latitude=48.7784, longitude=9.1800),
    ),
    DemoCorridor(
        slug="frankfurt-muenchen",
        name="Frankfurt am Main → München",
        origin_query="Frankfurt am Main",
        destination_query="München",
        origin_fallback=Coordinate(latitude=50.1109, longitude=8.6821),
        destination_fallback=Coordinate(latitude=48.1371, longitude=11.5754),
    ),
    DemoCorridor(
        slug="stuttgart-muenchen",
        name="Stuttgart → München",
        origin_query="Stuttgart",
        destination_query="München",
        origin_fallback=Coordinate(latitude=48.7784, longitude=9.1800),
        destination_fallback=Coordinate(latitude=48.1371, longitude=11.5754),
    ),
    DemoCorridor(
        slug="muenchen-ingolstadt",
        name="München → Ingolstadt",
        origin_query="München",
        destination_query="Ingolstadt",
        origin_fallback=Coordinate(latitude=48.1371, longitude=11.5754),
        destination_fallback=Coordinate(latitude=48.7630, longitude=11.4250),
    ),
    DemoCorridor(
        slug="wolfsburg-berlin",
        name="Wolfsburg → Berlin",
        origin_query="Wolfsburg",
        destination_query="Berlin",
        origin_fallback=Coordinate(latitude=52.4206, longitude=10.7862),
        destination_fallback=Coordinate(latitude=52.5174, longitude=13.3951),
    ),
)
"""The corridors of BUILD_SPEC §8, flagship first.

Frankfurt → Stuttgart leads because it is the one the bundled OSRM fixture covers: with the
network unplugged, the demo still has a fully analysed corridor rather than an empty page.
"""

_ROUTE_COLUMNS: Final[tuple[str, ...]] = (
    "name",
    "origin_name",
    "destination_name",
    "origin",
    "destination",
    "geometry",
    "distance_m",
    "duration_s",
    "routing_profile",
    "is_demo",
    "source",
    "source_identifier",
    "source_url",
    "source_timestamp",
    "data_origin",
    "ingestion_run_id",
    "ingested_at",
)
"""Columns refreshed when a corridor is re-seeded; the id and the slug stay put."""

_MIN_GEOMETRY_VERTICES: Final[int] = 2
"""A route with fewer vertices is not a line; the routing adapter is expected to reject it."""


class RouteSeedPipeline(IngestionPipeline):
    """Seeds the demo corridors through the routing and geocoding providers.

    An ingestion pipeline like the others — it reaches external sources, so BUILD_SPEC §15
    requires it to leave a ``data_ingestion_runs`` row, including when it fails.
    """

    name = "osrm_demo_corridors"
    source = SourceSystem.osrm

    def __init__(
        self,
        *,
        routing: RoutingProvider | None = None,
        geocoding: GeocodingProvider | None = None,
        corridors: Sequence[DemoCorridor] = DEMO_CORRIDORS,
        profile: str = DEFAULT_PROFILE,
        **kwargs: Any,
    ) -> None:
        """Configure the seed.

        Args:
            routing: Routing adapter; defaults to OSRM bound to this run's data mode.
            geocoding: Geocoding adapter; defaults to Nominatim bound to this run's data mode.
            corridors: Corridors to seed. Defaults to :data:`DEMO_CORRIDORS`.
            profile: Routing profile; ``driving`` is the only one AutoTwin models.
            **kwargs: Forwarded to :class:`IngestionPipeline`.

        Raises:
            ValueError: ``corridors`` is empty.
        """
        super().__init__(**kwargs)
        if not corridors:
            msg = "at least one corridor is required"
            raise ValueError(msg)
        self._corridors = tuple(corridors)
        self._profile = profile
        self._routing = routing or OSRMRoutingProvider(
            settings=self.settings,
            data_mode=self.data_mode,
        )
        self._geocoding = geocoding or NominatimGeocodingProvider(
            settings=self.settings,
            data_mode=self.data_mode,
        )
        self._owns_routing = routing is None
        self._owns_geocoding = geocoding is None

    async def aclose(self) -> None:
        """Release the adapters this pipeline created."""
        if self._owns_routing:
            await close_provider(self._routing)
        if self._owns_geocoding:
            await close_provider(self._geocoding)

    async def _execute(self, context: RunContext) -> PipelineOutcome:
        """Geocode, route, segment and upsert every corridor."""
        outcome = PipelineOutcome(mode=ProviderMode.live)
        modes: list[ProviderMode] = []
        seeded: list[str] = []
        failed: list[str] = []
        segment_rows: list[dict[str, Any]] = []

        for corridor in self._corridors:
            outcome.report.rows_received += 1
            try:
                origin, destination = await self._endpoints(corridor, outcome)
                route = await self._routing.route(origin, destination, profile=self._profile)
            except ProviderError as error:
                failed.append(corridor.slug)
                outcome.report.rows_rejected += 1
                outcome.warnings.append(f"{corridor.slug}: {error}")
                _logger.warning(
                    "seed.routes.corridor_failed",
                    slug=corridor.slug,
                    error=type(error).__name__,
                    detail=str(error),
                )
                continue

            modes.append(route.mode)
            outcome.warnings.extend(route.warnings)
            if len(route.data.geometry) < _MIN_GEOMETRY_VERTICES:  # pragma: no cover - defensive
                failed.append(corridor.slug)
                outcome.report.rows_rejected += 1
                outcome.warnings.append(f"{corridor.slug}: routing returned no usable geometry")
                continue

            outcome.report.rows_accepted += 1
            seeded.append(corridor.slug)
            segments = segment_polyline(route.data.geometry)
            if context.writes_enabled:
                route_id = await self._persist(
                    corridor,
                    route.data,
                    segments,
                    origin=origin,
                    destination=destination,
                    source_url=route.source_url,
                    source_timestamp=route.fetched_at,
                    run_id=context.run_id,
                )
                outcome.rows_written += 1
                segment_rows.extend(
                    _segment_lake_rows(corridor, route.data, segments, route_id=route_id)
                )
            outcome.source_url = route.source_url or outcome.source_url

        if segment_rows and context.writes_enabled:
            context.lake.write_table(
                segment_rows,
                zone=LakeZone.silver,
                dataset=SILVER_DATASET,
                partition=context.started_at.date(),
                schema=SILVER_SCHEMA,
            )

        outcome.mode = _weakest_mode(modes)
        outcome.details["corridors seeded"] = ", ".join(seeded) or "none"
        if failed:
            outcome.details["corridors failed"] = ", ".join(failed)
        return outcome

    # ------------------------------------------------------------------ endpoints

    async def _endpoints(
        self,
        corridor: DemoCorridor,
        outcome: PipelineOutcome,
    ) -> tuple[Coordinate, Coordinate]:
        """Resolve both endpoints, falling back to the corridor's own coordinates."""
        origin = await self._resolve(corridor.origin_query, corridor.origin_fallback, outcome)
        destination = await self._resolve(
            corridor.destination_query,
            corridor.destination_fallback,
            outcome,
        )
        return origin, destination

    async def _resolve(
        self,
        query: str,
        fallback: Coordinate,
        outcome: PipelineOutcome,
    ) -> Coordinate:
        """Geocode one endpoint.

        A geocoder that is down or that returns nothing must not stop the seed: the fallback
        coordinate is the same point the bundled snapshot answers with, so the corridor is
        identical either way and the substitution is reported as a warning rather than hidden.
        """
        try:
            result = await self._geocoding.geocode(query)
        except ProviderError as error:
            outcome.warnings.append(f"geocoding {query!r} failed ({error}); using the demo point")
            return fallback
        places: list[Place] = list(result.data)
        if not places:
            outcome.warnings.append(f"geocoding {query!r} returned nothing; using the demo point")
            return fallback
        return places[0].coordinate

    # ------------------------------------------------------------------ persistence

    async def _persist(
        self,
        corridor: DemoCorridor,
        route: RouteResult,
        segments: Sequence[PolylineSegment],
        *,
        origin: Coordinate,
        destination: Coordinate,
        source_url: str | None,
        source_timestamp: datetime,
        run_id: UUID | None,
    ) -> UUID:
        """Upsert one corridor and replace its segments, returning the route id."""
        ingested_at = utc_now()
        provenance = ProvenanceInfo(
            source=SourceSystem.osrm,
            source_identifier=corridor.slug,
            source_url=source_url,
            source_timestamp=source_timestamp,
            data_origin=DataOrigin.derived,
            ingested_at=ingested_at,
        )
        values = {
            "slug": corridor.slug,
            "name": corridor.name,
            "origin_name": corridor.origin_query,
            "destination_name": corridor.destination_query,
            "origin": to_shape_point(origin.latitude, origin.longitude),
            "destination": to_shape_point(destination.latitude, destination.longitude),
            "geometry": to_shape_linestring(list(route.geometry)),
            "distance_m": route.distance_m,
            "duration_s": route.duration_s,
            "routing_profile": route.profile,
            "is_demo": True,
            **provenance_columns(provenance, run_id=run_id, ingested_at=ingested_at),
        }

        async with session_scope() as session:
            statement = pg_insert(Route).values([values])
            excluded = statement.excluded
            upsert = statement.on_conflict_do_update(
                index_elements=[Route.slug],
                set_={column: getattr(excluded, column) for column in _ROUTE_COLUMNS},
            ).returning(Route.id)
            route_id: UUID = (await session.execute(upsert)).scalar_one()
            await self._replace_segments(session, route_id, route, segments)
        _logger.info(
            "seed.routes.corridor",
            slug=corridor.slug,
            distance_km=round(route.distance_m / 1000.0, 1),
            duration_min=round(route.duration_s / 60.0, 1),
            segments=len(segments),
        )
        return route_id

    @staticmethod
    async def _replace_segments(
        session: AsyncSession,
        route_id: UUID,
        route: RouteResult,
        segments: Sequence[PolylineSegment],
    ) -> None:
        """Delete the corridor's segments and insert the fresh cut.

        Replace rather than upsert on ``(route_id, ordinal)``: a re-route can change the
        segment *count*, and an upsert would leave the tail of the previous, longer cut behind
        as orphaned rows that still satisfy the unique constraint.
        """
        await session.execute(sa.delete(RouteSegment).where(RouteSegment.route_id == route_id))
        attributes = _segment_attributes(route.steps, segments, route)
        rows = [
            {
                "route_id": route_id,
                "ordinal": segment.ordinal,
                "geometry": to_shape_linestring(list(segment.coordinates)),
                "start_offset_m": segment.start_offset_m,
                "distance_m": segment.length_m,
                "road_class": attribute.road_class,
                "speed_limit_kmh": attribute.speed_limit_kmh,
                "assumed_speed_kmh": attribute.assumed_speed_kmh,
            }
            for segment, attribute in zip(segments, attributes, strict=True)
        ]
        if rows:
            await session.execute(sa.insert(RouteSegment), rows)


@dataclass(frozen=True, slots=True)
class _SegmentAttributes:
    """Per-segment facts derived from the routing engine's own steps."""

    road_class: RoadClass
    """Class covering the most metres of the segment."""

    speed_limit_kmh: float | None
    """Posted limit of that dominant step, when the engine states one."""

    assumed_speed_kmh: float | None
    """Distance-weighted mean of the free-flow speeds of the steps covering the segment."""


def _segment_attributes(
    steps: Sequence[RouteStepRecord],
    segments: Sequence[PolylineSegment],
    route: RouteResult,
) -> list[_SegmentAttributes]:
    """Project OSRM's steps onto the equal-length segments.

    Steps and segments are both ordered along the route but cut at different places — a step is
    one manoeuvre (20 m at a roundabout, 40 km on the Autobahn), a segment is a fixed 5 km of
    analysis. Laying the steps out as offset ranges and intersecting them with each segment is
    what lets a segment say "this is motorway at 130 km/h" with the engine's own numbers behind
    it rather than a guess.

    With no steps at all — an engine asked without ``steps=true`` — every segment falls back to
    the route's average speed and :attr:`RoadClass.unknown`, which is honest: the route says
    how fast it is overall and nothing about its road classes.
    """
    fallback_speed = route.average_speed_kmh or None
    if not steps:
        return [_SegmentAttributes(RoadClass.unknown, None, fallback_speed) for _ in segments]

    ranges: list[tuple[float, float, RouteStepRecord]] = []
    offset = 0.0
    for step in steps:
        ranges.append((offset, offset + step.distance_m, step))
        offset += step.distance_m

    attributes: list[_SegmentAttributes] = []
    for segment in segments:
        start, end = segment.start_offset_m, segment.end_offset_m
        by_class: dict[RoadClass, float] = {}
        weighted_speed = 0.0
        covered = 0.0
        limit_by_class: dict[RoadClass, float | None] = {}
        for step_start, step_end, step in ranges:
            overlap = min(end, step_end) - max(start, step_start)
            if overlap <= 0.0:
                continue
            by_class[step.road_class] = by_class.get(step.road_class, 0.0) + overlap
            limit_by_class.setdefault(step.road_class, step.speed_limit_kmh)
            if step.duration_s > 0.0:
                weighted_speed += overlap * (step.distance_m / step.duration_s) * 3.6
                covered += overlap
        if not by_class:
            attributes.append(_SegmentAttributes(RoadClass.unknown, None, fallback_speed))
            continue
        dominant = max(by_class.items(), key=lambda item: item[1])[0]
        speed = weighted_speed / covered if covered > 0.0 else fallback_speed
        attributes.append(
            _SegmentAttributes(
                road_class=dominant,
                speed_limit_kmh=limit_by_class.get(dominant),
                assumed_speed_kmh=speed,
            )
        )
    return attributes


def _segment_lake_rows(
    corridor: DemoCorridor,
    route: RouteResult,
    segments: Sequence[PolylineSegment],
    *,
    route_id: UUID,
) -> list[dict[str, Any]]:
    """Render the corridor's segments as flat rows for the silver zone."""
    attributes = _segment_attributes(route.steps, segments, route)
    return [
        {
            "route_id": str(route_id),
            "slug": corridor.slug,
            "ordinal": segment.ordinal,
            "start_offset_m": segment.start_offset_m,
            "distance_m": segment.length_m,
            "midpoint_latitude": segment.midpoint.latitude,
            "midpoint_longitude": segment.midpoint.longitude,
            "road_class": attribute.road_class.value,
            "speed_limit_kmh": attribute.speed_limit_kmh,
            "assumed_speed_kmh": attribute.assumed_speed_kmh,
            "data_origin": DataOrigin.derived.value,
        }
        for segment, attribute in zip(segments, attributes, strict=True)
    ]


def _weakest_mode(modes: Sequence[ProviderMode]) -> ProviderMode:
    """The least-live mode among the corridors — one cached route downgrades the whole seed."""
    if not modes:
        return ProviderMode.fixture
    order = (ProviderMode.fixture, ProviderMode.cache, ProviderMode.live)
    return min(modes, key=order.index)
