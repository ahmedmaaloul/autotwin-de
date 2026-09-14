"""Charging infrastructure: the Bundesnetzagentur register, its statistics and corridor coverage.

This is the data-richest part of the API — 116 440 sites and 219 821 connectors — so the
listing endpoints are written around what the indexes of BUILD_SPEC §3.2 can actually serve:

* every filter is a predicate on an indexed column (`bundesland`, `charging_category`,
  `max_power_kw`, `operator`) or on the GiST index over `location`;
* the free-text search is a pair of ``ILIKE '%…%'`` predicates on `operator`/`city`, the form a
  `pg_trgm` GIN index accelerates — see :func:`_text_search_condition`;
* only the columns a response needs are selected. `charging_stations.raw` holds the trimmed
  original register record, and fetching it for a page of 500 sites would move megabytes that
  no caller asked for.

The corridor endpoints delegate to :mod:`autotwin_api.services.coverage`, which runs the whole
gap analysis in PostGIS and is documented there.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Final
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Query
from geoalchemy2 import Geography, Geometry
from sqlalchemy import Row, Select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from autotwin_api.deps import BoundingBoxQuery, DbSession, Pagination
from autotwin_api.middleware import set_data_mode
from autotwin_api.pagination import count_rows, paginate_rows
from autotwin_api.schemas.charging import (
    BundeslandStat,
    ChargingPointOut,
    ChargingStationDetail,
    ChargingStationSummary,
    ChargingStatistics,
    CorridorCoverageOut,
    CoverageGapOut,
    GrowthPoint,
    OperatorStat,
    PowerClassStat,
    StationFeature,
    StationFeatureCollection,
    StationFeatureProperties,
    UnderservedCorridorOut,
    UnderservedEvaluation,
    UnderservedParameters,
    UnderservedReport,
)
from autotwin_api.schemas.common import GeoPointOut, ProvenanceOut
from autotwin_api.services.coverage import (
    DEFAULT_BUFFER_KM,
    DEFAULT_TOP_GAPS,
    TARGET_MAX_GAP_KM,
    compute_corridor_coverage,
    methodology_sentence,
)
from autotwin_contracts import (
    FAST_CHARGER_THRESHOLD_KW,
    BoundingBox,
    Bundesland,
    ChargingCategory,
    IngestionOutcome,
    Page,
    ProviderMode,
    SourceSystem,
    utc_now,
)
from autotwin_core.db.models import ChargingPoint, ChargingStation, DataIngestionRun
from autotwin_core.db.types import SRID_WGS84
from autotwin_core.errors import NotFoundError, ValidationError
from autotwin_core.logging import get_logger

__all__ = ["register_data_mode", "router"]

_LOGGER = get_logger(__name__)

router = APIRouter()

_POINT_GEOMETRY: Final[Geometry] = Geometry(geometry_type="POINT", srid=SRID_WGS84)
"""Target type for the ``geography → geometry`` cast.

``ST_X``/``ST_Y`` are geometry functions and PostGIS offers no geography overload, so the column
has to be cast. The typmod must be spelled out: SQLAlchemy renders a bare ``Geometry()`` as
``geometry(GEOMETRY,-1)``, which PostgreSQL rejects.
"""

_LATITUDE: Final[Any] = sa.func.ST_Y(sa.cast(ChargingStation.location, _POINT_GEOMETRY)).label(
    "latitude"
)
_LONGITUDE: Final[Any] = sa.func.ST_X(sa.cast(ChargingStation.location, _POINT_GEOMETRY)).label(
    "longitude"
)

_GEOJSON_FEATURE_CAP: Final[int] = 20_000
"""Hard cap on ``/stations/geojson`` (BUILD_SPEC §7).

The register holds 116 440 sites; serialising all of them would be a ~20 MB response that no
browser can usefully draw. Features are ordered strongest-first before the cap bites, so a
truncated collection keeps the sites that matter for route planning rather than an arbitrary
slice, and ``truncated``/``total_matching`` say plainly that it happened.
"""

_MAX_TOP_OPERATORS: Final[int] = 100
_DEFAULT_TOP_OPERATORS: Final[int] = 20

_LIKE_ESCAPE: Final[str] = "\\"
"""Escape character for the free-text search; see :func:`_text_search_condition`."""

_UNDERSERVED_MAX_SEGMENTS: Final[int] = 25
"""Cap on the stretches reported per underserved corridor.

A corridor would need 25 gaps above a 50 km threshold — 1 250 km of uncovered road — to hit
this, so in practice it only bounds the payload when the caller passes an unusually small
``max_gap_km``.
"""


# ---------------------------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------------------------


def _text_search_condition(term: str) -> sa.ColumnElement[bool]:
    """Match ``term`` against operator or city, case-insensitively and anywhere in the value.

    ``ILIKE '%term%'`` rather than a prefix match because the register's operator names are
    corporate full names ("EnBW mobility+ AG und Co.KG"): a user searching for *enbw* means the
    middle of the string as often as the start. An unanchored ``ILIKE`` cannot use a B-tree
    index, which is exactly what `pg_trgm`'s GIN operator class exists for — the extension is
    installed on this database, so the shape of the predicate is what decides whether the search
    is a trigram lookup or a sequential scan.

    ``%`` and ``_`` in the user's term are escaped: unescaped, a query for ``100%`` would match
    every row, which looks like a broken filter rather than like the wildcard the user never
    intended to type.
    """
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    pattern = f"%{escaped}%"
    return sa.or_(
        ChargingStation.operator.ilike(pattern, escape=_LIKE_ESCAPE),
        ChargingStation.city.ilike(pattern, escape=_LIKE_ESCAPE),
    )


def _bbox_condition(bbox: BoundingBox) -> sa.ColumnElement[bool]:
    """Restrict to a bounding box, staying on the ``geography`` GiST index.

    The envelope is built in WGS 84 and cast to ``geography`` so the comparison happens in the
    column's own type; casting the *column* to geometry instead would make the index unusable
    and turn every map pan into a sequential scan of 116 440 rows.
    """
    envelope = sa.cast(
        sa.func.ST_MakeEnvelope(bbox.west, bbox.south, bbox.east, bbox.north, SRID_WGS84),
        Geography(geometry_type="POLYGON", srid=SRID_WGS84),
    )
    return sa.func.ST_Intersects(ChargingStation.location, envelope)


def _station_filters(
    *,
    bbox: BoundingBox | None,
    bundesland: Bundesland | None,
    operator: str | None,
    min_power_kw: float | None,
    fast_only: bool,
    category: ChargingCategory | None,
    commissioned_from: date | None,
    commissioned_to: date | None,
    q: str | None,
) -> list[sa.ColumnElement[bool]]:
    """Translate the shared query parameters into WHERE clauses.

    One function for both ``/stations`` and ``/stations/geojson`` because the two endpoints are
    the same query with a different projection, and a filter implemented twice is a filter that
    will eventually mean two different things.
    """
    conditions: list[sa.ColumnElement[bool]] = []
    if bbox is not None:
        conditions.append(_bbox_condition(bbox))
    if bundesland is not None:
        conditions.append(ChargingStation.bundesland == bundesland)
    if operator is not None:
        conditions.append(ChargingStation.operator == operator)
    if min_power_kw is not None:
        conditions.append(ChargingStation.max_power_kw >= min_power_kw)
    if fast_only:
        conditions.append(ChargingStation.is_fast_charger.is_(True))
    if category is not None:
        conditions.append(ChargingStation.charging_category == category)
    if commissioned_from is not None:
        conditions.append(ChargingStation.commissioned_on >= commissioned_from)
    if commissioned_to is not None:
        conditions.append(ChargingStation.commissioned_on <= commissioned_to)
    if q:
        conditions.append(_text_search_condition(q))
    return conditions


# ---------------------------------------------------------------------------------------------
# Data freshness
# ---------------------------------------------------------------------------------------------


async def register_data_mode(session: AsyncSession) -> ProviderMode | None:
    """How the charging register currently in the database was obtained.

    These endpoints serve persisted rows rather than a live provider call, but the honesty rule
    of BUILD_SPEC §0.2 still applies: a client is entitled to know whether the 116 440 sites it
    is looking at were downloaded from the Bundesnetzagentur or read out of the bundled fixture.
    The answer is the ``provider_mode`` of the newest ingestion run that actually wrote rows.

    Runs with status ``failed`` are skipped deliberately. A failed live attempt leaves the
    previous snapshot in place, and reporting its mode would describe an attempt rather than the
    data — the most misleading answer available.

    One indexed single-row lookup on ``ix_data_ingestion_runs_source_started_at``. Exported
    because ``/api/v1/analytics/regions`` answers off the same register and owes the same header.
    """
    statement = (
        sa.select(DataIngestionRun.provider_mode)
        .where(
            DataIngestionRun.source == SourceSystem.bundesnetzagentur,
            DataIngestionRun.status != IngestionOutcome.failed,
        )
        .order_by(DataIngestionRun.started_at.desc())
        .limit(1)
    )
    return (await session.execute(statement)).scalars().first()


async def _publish_register_mode(session: AsyncSession) -> None:
    """Set ``X-AutoTwin-Data-Mode`` from the register's ingestion history, if it is known."""
    mode = await register_data_mode(session)
    if mode is not None:
        set_data_mode(mode)


# ---------------------------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------------------------

_SUMMARY_COLUMNS: Final[tuple[Any, ...]] = (
    ChargingStation.id,
    ChargingStation.external_id,
    ChargingStation.operator,
    ChargingStation.city,
    ChargingStation.postal_code,
    ChargingStation.street,
    ChargingStation.house_number,
    ChargingStation.bundesland,
    ChargingStation.max_power_kw,
    ChargingStation.total_power_kw,
    ChargingStation.charging_points_count,
    ChargingStation.charging_category,
    ChargingStation.is_fast_charger,
    ChargingStation.commissioned_on,
    _LATITUDE,
    _LONGITUDE,
)
"""Exactly the columns :class:`ChargingStationSummary` needs — notably *not* ``raw``."""


def _summary_of(row: Row[Any]) -> ChargingStationSummary:
    """Build the summary model from a result row."""
    return ChargingStationSummary(
        id=row.id,
        external_id=row.external_id,
        operator=row.operator,
        city=row.city,
        postal_code=row.postal_code,
        street=row.street,
        house_number=row.house_number,
        bundesland=row.bundesland,
        latitude=row.latitude,
        longitude=row.longitude,
        max_power_kw=row.max_power_kw,
        total_power_kw=row.total_power_kw,
        charging_points_count=row.charging_points_count,
        charging_category=row.charging_category,
        is_fast_charger=row.is_fast_charger,
        commissioned_on=row.commissioned_on,
    )


def _ordered_station_query(conditions: list[sa.ColumnElement[bool]]) -> Select[Any]:
    """The station query both list endpoints run, strongest site first.

    Ordering by power rather than by name or id is what makes the map's 20 000-feature cap
    defensible: when the collection is truncated, what survives is the infrastructure that
    matters for long-distance driving. ``id`` breaks ties so that paging is stable — two sites
    of equal power must not swap places between page 1 and page 2.
    """
    return (
        sa.select(*_SUMMARY_COLUMNS)
        .where(*conditions)
        .order_by(ChargingStation.max_power_kw.desc().nullslast(), ChargingStation.id.asc())
    )


@router.get(
    "/stations",
    response_model=Page[ChargingStationSummary],
    summary="List charging sites",
    description=(
        "Charging sites from the Bundesnetzagentur Ladesäulenregister, strongest first.\n\n"
        "All filters combine with AND. `q` searches operator and city case-insensitively "
        "anywhere in the value; `operator` is an exact match, so pick its value from "
        "`/charging/statistics`. `bbox` is `west,south,east,north` in WGS 84 degrees."
    ),
)
async def list_stations(
    session: DbSession,
    page: Pagination,
    bbox: BoundingBoxQuery,
    bundesland: Annotated[
        Bundesland | None,
        Query(description="Federal state as an ISO 3166-2:DE code, e.g. `HE`."),
    ] = None,
    operator: Annotated[
        str | None,
        Query(description="Exact operator name as the register spells it.", max_length=200),
    ] = None,
    min_power_kw: Annotated[
        float | None,
        Query(ge=0.0, description="Only sites whose strongest connector reaches this power."),
    ] = None,
    fast_only: Annotated[
        bool,
        Query(description="Only sites flagged as fast chargers (>= 50 kW)."),
    ] = False,
    category: Annotated[
        ChargingCategory | None,
        Query(description="Power class: `normal`, `fast` or `ultra_fast`."),
    ] = None,
    commissioned_from: Annotated[
        date | None,
        Query(description="Only sites commissioned on or after this date (ISO 8601)."),
    ] = None,
    commissioned_to: Annotated[
        date | None,
        Query(description="Only sites commissioned on or before this date (ISO 8601)."),
    ] = None,
    q: Annotated[
        str | None,
        Query(description="Free-text search over operator and city.", max_length=100),
    ] = None,
) -> Page[ChargingStationSummary]:
    """Return one page of charging sites matching the filters."""
    await _publish_register_mode(session)
    conditions = _station_filters(
        bbox=bbox,
        bundesland=bundesland,
        operator=operator,
        min_power_kw=min_power_kw,
        fast_only=fast_only,
        category=category,
        commissioned_from=commissioned_from,
        commissioned_to=commissioned_to,
        q=q,
    )
    return await paginate_rows(session, _ordered_station_query(conditions), page, _summary_of)


@router.get(
    "/stations/geojson",
    response_model=StationFeatureCollection,
    summary="Charging sites as GeoJSON",
    description=(
        f"The same filters as `/charging/stations`, rendered as an RFC 7946 FeatureCollection "
        f"for a MapLibre source. Capped at {_GEOJSON_FEATURE_CAP:,} features; sites are ordered "
        "by power before the cap applies, so a truncated collection keeps the strongest "
        "infrastructure rather than an arbitrary slice. Check `truncated` and "
        "`total_matching`, and narrow `bbox` or `min_power_kw` when it is set."
    ),
)
async def stations_geojson(
    session: DbSession,
    bbox: BoundingBoxQuery,
    bundesland: Annotated[Bundesland | None, Query(description="Federal state code.")] = None,
    operator: Annotated[
        str | None,
        Query(description="Exact operator name.", max_length=200),
    ] = None,
    min_power_kw: Annotated[
        float | None,
        Query(ge=0.0, description="Minimum power of the strongest connector, in kW."),
    ] = None,
    fast_only: Annotated[bool, Query(description="Only fast chargers (>= 50 kW).")] = False,
    category: Annotated[
        ChargingCategory | None,
        Query(description="Power class filter."),
    ] = None,
    commissioned_from: Annotated[
        date | None,
        Query(description="Commissioned on or after this date."),
    ] = None,
    commissioned_to: Annotated[
        date | None,
        Query(description="Commissioned on or before this date."),
    ] = None,
    q: Annotated[
        str | None,
        Query(description="Free-text search over operator and city.", max_length=100),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=_GEOJSON_FEATURE_CAP,
            description=f"Feature cap for this request (1-{_GEOJSON_FEATURE_CAP}).",
        ),
    ] = _GEOJSON_FEATURE_CAP,
) -> StationFeatureCollection:
    """Return the matching sites as a GeoJSON FeatureCollection, capped and honest about it."""
    await _publish_register_mode(session)
    conditions = _station_filters(
        bbox=bbox,
        bundesland=bundesland,
        operator=operator,
        min_power_kw=min_power_kw,
        fast_only=fast_only,
        category=category,
        commissioned_from=commissioned_from,
        commissioned_to=commissioned_to,
        q=q,
    )
    statement = _ordered_station_query(conditions)
    total = await count_rows(session, statement)
    rows = (await session.execute(statement.limit(limit))).all()

    features = [
        StationFeature(
            id=str(row.id),
            geometry=GeoPointOut.from_coordinate(latitude=row.latitude, longitude=row.longitude),
            properties=StationFeatureProperties(
                id=row.id,
                operator=row.operator,
                max_power_kw=row.max_power_kw,
                charging_category=row.charging_category,
                is_fast_charger=row.is_fast_charger,
                city=row.city,
            ),
        )
        for row in rows
    ]
    extent: tuple[float, float, float, float] | None = None
    if rows:
        longitudes = [float(row.longitude) for row in rows]
        latitudes = [float(row.latitude) for row in rows]
        extent = (min(longitudes), min(latitudes), max(longitudes), max(latitudes))

    if total > len(features):
        _LOGGER.info("charging.geojson.truncated", total=total, returned=len(features))

    return StationFeatureCollection(
        features=features,
        bbox=extent,
        total_matching=total,
        truncated=total > len(features),
    )


@router.get(
    "/stations/{station_id}",
    response_model=ChargingStationDetail,
    summary="One charging site",
    description=(
        "A single site with every connector and the provenance block of BUILD_SPEC §3.1, so "
        "the UI can always answer *where did this row come from, and when was it true?*"
    ),
)
async def get_station(session: DbSession, station_id: UUID) -> ChargingStationDetail:
    """Return one site, or 404 if it does not exist."""
    await _publish_register_mode(session)
    statement = (
        sa.select(ChargingStation, _LATITUDE, _LONGITUDE)
        .options(selectinload(ChargingStation.points))
        .where(ChargingStation.id == station_id)
    )
    row = (await session.execute(statement)).first()
    if row is None:
        raise NotFoundError(
            f"No charging station with id {station_id}",
            details={"station_id": str(station_id)},
        )

    station: ChargingStation = row[0]
    return ChargingStationDetail(
        id=station.id,
        external_id=station.external_id,
        operator=station.operator,
        city=station.city,
        postal_code=station.postal_code,
        street=station.street,
        house_number=station.house_number,
        bundesland=station.bundesland,
        latitude=row.latitude,
        longitude=row.longitude,
        max_power_kw=station.max_power_kw,
        total_power_kw=station.total_power_kw,
        charging_points_count=station.charging_points_count,
        charging_category=station.charging_category,
        is_fast_charger=station.is_fast_charger,
        commissioned_on=station.commissioned_on,
        charging_points=[
            ChargingPointOut.model_validate(point) for point in sorted(station.points, key=_ordinal)
        ],
        provenance=ProvenanceOut.model_validate(station),
    )


def _ordinal(point: ChargingPoint) -> int:
    """Sort key for a site's connectors — register order, not insertion order."""
    return point.ordinal


# ---------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------


async def _by_bundesland(session: AsyncSession) -> list[BundeslandStat]:
    """Per-state site counts, fast connectors and installed power.

    The fast-connector figure is counted on ``charging_points`` rather than taken from
    ``charging_stations.charging_points_count``: a 300 kW site with one CCS plug and three
    22 kW plugs has four connectors and one fast one, and the station-level count cannot tell
    the difference.

    Installed power falls back from the site's published total to its strongest connector, which
    is what keeps a single-connector site from contributing zero to its state's total.
    """
    power = sa.func.coalesce(ChargingStation.total_power_kw, ChargingStation.max_power_kw)
    site_statement = (
        sa.select(
            ChargingStation.bundesland.label("bundesland"),
            sa.func.count().label("stations"),
            sa.func.coalesce(sa.func.sum(power), 0.0).label("total_kw"),
        )
        .where(ChargingStation.bundesland.is_not(None))
        .group_by(ChargingStation.bundesland)
    )
    connector_statement = (
        sa.select(
            ChargingStation.bundesland.label("bundesland"),
            sa.func.count().label("fast_points"),
        )
        .select_from(ChargingPoint)
        .join(ChargingStation, ChargingStation.id == ChargingPoint.station_id)
        .where(
            ChargingStation.bundesland.is_not(None),
            ChargingPoint.power_kw >= FAST_CHARGER_THRESHOLD_KW,
        )
        .group_by(ChargingStation.bundesland)
    )

    fast_points = {
        row.bundesland: int(row.fast_points)
        for row in (await session.execute(connector_statement)).all()
    }
    rows = (await session.execute(site_statement)).all()
    stats = [
        BundeslandStat(
            bundesland=row.bundesland,
            label_de=Bundesland(row.bundesland).label_de,
            stations=int(row.stations),
            fast_points=fast_points.get(row.bundesland, 0),
            total_kw=round(float(row.total_kw), 1),
        )
        for row in rows
    ]
    stats.sort(key=lambda stat: stat.stations, reverse=True)
    return stats


async def _by_power_class(session: AsyncSession) -> list[PowerClassStat]:
    """Site counts per power class, with every class present even at zero.

    A class that vanishes because nothing falls into it makes a bar chart silently change shape
    between deployments, so the three of BUILD_SPEC §2 are always emitted.
    """
    # Labelled `stations` rather than `count`: `Row.count` is the tuple method, so a column of
    # that name is shadowed and reads back as a bound method instead of a number.
    statement = sa.select(
        ChargingStation.charging_category.label("category"),
        sa.func.count().label("stations"),
    ).group_by(ChargingStation.charging_category)
    counts = {row.category: int(row.stations) for row in (await session.execute(statement)).all()}
    return [
        PowerClassStat(category=category, count=counts.get(category, 0))
        for category in ChargingCategory
    ]


async def _by_operator(session: AsyncSession, *, limit: int) -> list[OperatorStat]:
    """The largest operators by site count."""
    statement = (
        sa.select(
            ChargingStation.operator.label("operator"),
            sa.func.count().label("stations"),
        )
        .where(ChargingStation.operator.is_not(None))
        .group_by(ChargingStation.operator)
        .order_by(sa.desc("stations"), sa.asc("operator"))
        .limit(limit)
    )
    return [
        OperatorStat(operator=str(row.operator), stations=int(row.stations))
        for row in (await session.execute(statement)).all()
    ]


async def _growth(session: AsyncSession) -> list[GrowthPoint]:
    """The commissioning curve, with the running total computed once here.

    Only sites whose commissioning date the register publishes can appear, so the cumulative
    figure is a lower bound on the network rather than its size — said plainly in the schema
    description, because a growth chart that quietly disagrees with the headline count is worse
    than no chart.
    """
    year = sa.cast(sa.extract("year", ChargingStation.commissioned_on), sa.Integer).label("year")
    statement = (
        sa.select(year, sa.func.count().label("stations"))
        .where(ChargingStation.commissioned_on.is_not(None))
        .group_by(year)
        .order_by(sa.asc("year"))
    )
    points: list[GrowthPoint] = []
    cumulative = 0
    for row in (await session.execute(statement)).all():
        stations = int(row.stations)
        cumulative += stations
        points.append(GrowthPoint(year=int(row.year), stations=stations, cumulative=cumulative))
    return points


@router.get(
    "/statistics",
    response_model=ChargingStatistics,
    summary="Charging network statistics",
    description=(
        "Four breakdowns of the register snapshot: by federal state (sites, fast connectors, "
        "installed kW), by power class, by operator and by commissioning year. Sites whose "
        "federal state the register's spelling could not be resolved to are left out of "
        "`by_bundesland` — there is no honest state to attribute them to — so its site counts "
        "sum to the *resolved* total, not to `stations_total`."
    ),
)
async def statistics(
    session: DbSession,
    top_operators: Annotated[
        int,
        Query(
            ge=1,
            le=_MAX_TOP_OPERATORS,
            description=f"How many operators to return, largest first (1-{_MAX_TOP_OPERATORS}).",
        ),
    ] = _DEFAULT_TOP_OPERATORS,
) -> ChargingStatistics:
    """Aggregate the register four ways."""
    await _publish_register_mode(session)
    totals_statement = sa.select(
        sa.func.count().label("stations_total"),
        sa.func.count(ChargingStation.commissioned_on).label("with_date"),
    )
    totals = (await session.execute(totals_statement)).one()
    return ChargingStatistics(
        by_bundesland=await _by_bundesland(session),
        by_power_class=await _by_power_class(session),
        by_operator=await _by_operator(session, limit=top_operators),
        growth=await _growth(session),
        stations_total=int(totals.stations_total),
        stations_with_commissioning_date=int(totals.with_date),
        generated_at=utc_now(),
    )


# ---------------------------------------------------------------------------------------------
# Corridor coverage
# ---------------------------------------------------------------------------------------------


@router.get(
    "/coverage",
    response_model=CorridorCoverageOut,
    summary="Charging coverage of one corridor",
    description=(
        "Projects every charging site within `buffer_km` of the route onto the route itself "
        "and measures the stretches between consecutive qualifying sites — **including** the "
        "stretch before the first one and after the last one, which is where an undriveable "
        "corridor usually hides.\n\n"
        "Offsets and gaps are measured along the route geometry in ETRS89 / UTM 32N "
        "(EPSG:25832); densities are quoted against the routing engine's driving distance. "
        "`methodology` states the parameters the figures were produced with, and "
        "`coverage_score` is a planning heuristic, not a standard.\n\n"
        "Identify the route by `route_slug` (`frankfurt-stuttgart`, …) or by `route_id`."
    ),
)
async def coverage(
    session: DbSession,
    route_slug: Annotated[
        str | None,
        Query(description="Slug of the corridor, e.g. `frankfurt-stuttgart`.", max_length=100),
    ] = None,
    route_id: Annotated[UUID | None, Query(description="Route id, as an alternative.")] = None,
    buffer_km: Annotated[
        float,
        Query(gt=0.0, le=50.0, description="Corridor half-width in kilometres."),
    ] = DEFAULT_BUFFER_KM,
    min_power_kw: Annotated[
        float,
        Query(ge=0.0, description="Power from which a site counts as a charging opportunity."),
    ] = FAST_CHARGER_THRESHOLD_KW,
    top_gaps: Annotated[
        int,
        Query(ge=1, le=500, description="How many of the longest gaps to return."),
    ] = DEFAULT_TOP_GAPS,
) -> CorridorCoverageOut:
    """Analyse one corridor. 404 when neither identifier matches a route."""
    if route_slug is None and route_id is None:
        raise ValidationError(
            "Provide route_slug or route_id to identify the corridor",
            details={"parameter": "route_slug"},
        )
    results = await compute_corridor_coverage(
        session,
        route_id=route_id,
        route_slug=route_slug,
        buffer_km=buffer_km,
        min_power_kw=min_power_kw,
        top_gaps=top_gaps,
        include_geometry=True,
        count_all_stations=True,
    )
    if not results:
        raise NotFoundError(
            "No route matches the given identifier",
            details={"route_slug": route_slug, "route_id": str(route_id) if route_id else None},
        )
    return CorridorCoverageOut.of(results[0], generated_at=utc_now())


@router.get(
    "/underserved",
    response_model=UnderservedReport,
    summary="Corridors with charging gaps above the threshold",
    description=(
        "Runs the corridor gap analysis over the demo corridors and reports the stretches "
        "longer than `max_gap_km`.\n\n"
        "`corridors` holds only the corridors that breach the threshold. `evaluated` lists "
        "every corridor that was examined with its worst gap, so an empty `corridors` array "
        "reads as *all five corridors were checked and none breaches 50 km* rather than as a "
        "failed request. AFIR (EU 2023/1804) requires a >= 150 kW pool every 60 km on the "
        "TEN-T core network; the 50 km default here is the stricter internal planning target."
    ),
)
async def underserved(
    session: DbSession,
    min_power_kw: Annotated[
        float,
        Query(ge=0.0, description="Power from which a site counts as a charging opportunity."),
    ] = FAST_CHARGER_THRESHOLD_KW,
    max_gap_km: Annotated[
        float,
        Query(gt=0.0, description="Report stretches longer than this, in kilometres."),
    ] = TARGET_MAX_GAP_KM,
    corridor_buffer_km: Annotated[
        float,
        Query(gt=0.0, le=50.0, description="Corridor half-width in kilometres."),
    ] = DEFAULT_BUFFER_KM,
    route_slug: Annotated[
        str | None,
        Query(description="Restrict the report to one corridor.", max_length=100),
    ] = None,
) -> UnderservedReport:
    """Rank the demo corridors by their worst charging gap."""
    await _publish_register_mode(session)
    results = await compute_corridor_coverage(
        session,
        route_slug=route_slug,
        demo_only=True,
        buffer_km=corridor_buffer_km,
        min_power_kw=min_power_kw,
        top_gaps=_UNDERSERVED_MAX_SEGMENTS,
        min_reported_gap_km=max_gap_km,
        include_geometry=True,
        # Only the qualifying sites matter for a gap analysis, and skipping the total lets the
        # power threshold cut the spatial work roughly in half at 150 kW.
        count_all_stations=False,
    )
    results.sort(key=lambda result: result.max_gap_km, reverse=True)

    corridors: list[UnderservedCorridorOut] = []
    evaluated: list[UnderservedEvaluation] = []
    for result in results:
        slug = result.route_slug or str(result.route_id)
        is_underserved = result.max_gap_km > max_gap_km
        evaluated.append(
            UnderservedEvaluation(
                route_slug=slug,
                name=result.route_name,
                worst_gap_km=result.max_gap_km,
                coverage_score=result.coverage_score,
                is_underserved=is_underserved,
            )
        )
        if not is_underserved:
            continue
        corridors.append(
            UnderservedCorridorOut(
                route_slug=slug,
                name=result.route_name,
                worst_gap_km=result.max_gap_km,
                coverage_score=result.coverage_score,
                segments=[CoverageGapOut.of(gap) for gap in result.gaps if gap.gap_km > max_gap_km],
            )
        )

    return UnderservedReport(
        parameters=UnderservedParameters(
            min_power_kw=min_power_kw,
            max_gap_km=max_gap_km,
            corridor_buffer_km=corridor_buffer_km,
        ),
        corridors=corridors,
        evaluated=evaluated,
        generated_at=utc_now(),
        methodology=methodology_sentence(
            buffer_km=corridor_buffer_km,
            min_power_kw=min_power_kw,
        ),
    )
