"""Corridor charging coverage and gap analysis (BUILD_SPEC §7.2).

This module answers the one question that a station count cannot: *can an electric car actually
drive this corridor?* A route with forty fast chargers in its first 100 km and none in its last
200 km is undriveable; a route with ten evenly spaced ones is comfortable. Only a **gap**
measurement separates them, so the headline figure here is ``max_gap_km`` — the longest stretch
of the corridor with no qualifying charger on it.

The analysis is deliberately the same one that ``dbt/models/marts/mart_charging_coverage.sql``
runs in the warehouse. With the default parameters (5 km corridor, 50 kW threshold) the two
must agree row for row; where the endpoint is called with other parameters it is the same
method with different constants. Keeping them consistent is why the thresholds below are
written next to the dbt variable they mirror.

Four things about the SQL are load-bearing, and each of them is a mistake this module exists to
avoid:

1. **Selection is done on ``geography``.** ``ST_DWithin(location, route::geography, metres)``
   measures true metres on the spheroid, so ``buffer_km`` means kilometres on the ground.
2. **Projection is done in EPSG:25832.** ``ST_LineLocatePoint`` is a *geometry* function and
   measures in the units of the coordinate system it is handed. On raw WGS 84 that unit is
   degrees, and at 51° N one degree of longitude covers only ~63 % of the ground distance of
   one degree of latitude — an east-west corridor's offsets would be systematically compressed
   against a north-south one, in a direction-dependent way that no aggregate would reveal.
   Both the line and each station are therefore pushed through ``ST_Transform(…, 25832)``
   (ETRS89 / UTM 32N, the official German national grid) first.
3. **The two edge gaps are included.** Origin → first qualifying charger and last qualifying
   charger → destination. Omitting them is the classic error: a corridor whose only two fast
   chargers sit at km 10 and km 12 has one interior gap of 2 km and looks immaculate, while the
   280 km after km 12 are the entire problem.
4. **A corridor with no qualifying charger is one gap the length of the route**, not zero gaps,
   so the worst case sorts to the top of a "worst corridors" list instead of vanishing from it.

Those four together give the completeness invariant asserted in :func:`_check_gap_invariant`:
the gaps must tile the route exactly once, so they must sum to the route's geometry length. It
is what catches a missing edge gap, a double-counted station and an offset measured against the
wrong length — three mistakes that leave every other number in the payload looking plausible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_contracts import FAST_CHARGER_THRESHOLD_KW, GeoJSONLineString
from autotwin_core.logging import get_logger

__all__ = [
    "DEFAULT_BUFFER_KM",
    "DEFAULT_TOP_GAPS",
    "GERMANY_METRIC_SRID",
    "TARGET_FAST_STATIONS_PER_100KM",
    "TARGET_MAX_GAP_KM",
    "CorridorCoverage",
    "CorridorGap",
    "compute_corridor_coverage",
    "coverage_score",
    "methodology_sentence",
]

_LOGGER = get_logger(__name__)

GERMANY_METRIC_SRID: Final[int] = 25832
"""ETRS89 / UTM zone 32N — mirrors ``var('germany_metric_srid')`` in ``dbt_project.yml``.

Every "how far along this line" question in AutoTwin is answered in this grid. See the module
docstring for why a degree-based offset is wrong in a direction-dependent way.
"""

DEFAULT_BUFFER_KM: Final[float] = 5.0
"""Corridor half-width. 5 km is roughly a 5-8 minute detour at motorway-junction spacing, and
is ``var('corridor_buffer_m') / 1000`` in dbt."""

TARGET_MAX_GAP_KM: Final[float] = 50.0
"""Planning target for the worst gap (``var('target_max_gap_km')``).

AFIR (EU 2023/1804) requires a >=150 kW pool every 60 km on the TEN-T core network; 50 km is
the stricter internal target the score is calibrated against."""

TARGET_FAST_STATIONS_PER_100KM: Final[float] = 2.0
"""Planning target for corridor density (``var('target_fast_stations_per_100km')``)."""

DEFAULT_TOP_GAPS: Final[int] = 25
"""How many gaps a coverage response carries by default.

The aggregate figures (``gap_count``, ``max_gap_km``, ``mean_gap_km``) are computed over *all*
gaps in PostGIS; only the list of individual gaps — each of which carries a geometry the map
draws — is capped. A 5 km corridor with no power threshold contains several thousand stations
and therefore several thousand gaps, almost all of them metres long, and shipping their
geometries would make the response tens of megabytes of noise.
"""

_GAP_SUM_TOLERANCE_M: Final[float] = 10.0
"""How far the gaps may sum away from the route length before the invariant is reported broken.

Ten metres over a 400 km corridor: large enough to absorb the floating-point error of summing a
few thousand fractions, far too small to hide a missing edge gap.
"""

_SUBDIVIDE_MAX_VERTICES: Final[int] = 128
"""Vertex budget per piece when the route is cut up for the corridor join.

Not a tuning knob for the *answer* — cutting the line changes nothing, because a point is within
``d`` of a line exactly when it is within ``d`` of at least one of its pieces — but decisive for
the *cost*. Run against the whole 3 058-vertex Frankfurt-Stuttgart geometry, PostgreSQL expands
one bounding box over the entire corridor, pulls ~11 000 candidate rows out of the GiST index
and then measures each against all 3 058 segments on the spheroid: 33 s. Cut into ~24 pieces of
128 vertices, each piece probes the index with a tight box and tests against 128 segments: about
1 s for the identical 5 465 stations.
"""

_SCORE_GAP_WEIGHT: Final[float] = 0.6
"""Weight of the gap term in :func:`coverage_score`.

Weighted towards the gap rather than the density because a single long gap makes a corridor
undriveable, while a thin-but-even network merely makes it slow. Same split as the dbt mart.
"""

_SCORE_DENSITY_WEIGHT: Final[float] = 1.0 - _SCORE_GAP_WEIGHT


@dataclass(frozen=True, slots=True)
class CorridorGap:
    """One uncovered stretch of a corridor, measured along the route geometry."""

    start_offset_km: float
    """Distance from the origin at which the gap begins."""

    end_offset_km: float
    """Distance from the origin at which the gap ends."""

    gap_km: float
    """Length of the gap. Equal to ``end_offset_km - start_offset_km`` up to rounding."""

    kind: str
    """``origin``, ``between``, ``destination`` or ``whole_route`` — which of the four kinds of
    gap this is. Named so a client can tell "200 km before the first charger" from "200 km
    between two chargers"; they are very different planning problems."""

    geometry: GeoJSONLineString | None
    """The stretch of road itself, for drawing on the map. ``None`` for a zero-length gap,
    where ``ST_LineSubstring`` would return a point rather than a line."""


@dataclass(frozen=True, slots=True)
class CorridorCoverage:
    """The full coverage analysis of one corridor at one set of parameters."""

    route_id: UUID
    route_slug: str | None
    route_name: str

    buffer_km: float
    """Corridor half-width the analysis was run with."""

    min_power_kw: float
    """Power from which a station counted as a charging opportunity for the gap analysis."""

    route_distance_km: float
    """The routing engine's driving distance — the number the UI shows, and the base for the
    densities below."""

    route_geometry_length_km: float
    """Length of the stored polyline in EPSG:25832 — the base for every offset and gap.

    It differs from :attr:`route_distance_km` by a few per mille, because a polyline is a chord
    approximation of the road. Mixing the two inside one figure is how a gap analysis stops
    adding up to its own route, so both are published and each column uses exactly one of them.
    """

    stations_in_corridor: int | None
    """Every charging site within the buffer, regardless of power — ``None`` when the caller
    asked for the qualifying stations only and the total was never counted."""

    fast_stations_in_corridor: int | None
    """Sites at or above 50 kW (BUILD_SPEC §3.2 ``is_fast_charger``); ``None`` as above."""

    qualifying_stations_in_corridor: int
    """Sites at or above :attr:`min_power_kw` — the set the gaps were computed from."""

    stations_per_100km: float | None
    """:attr:`stations_in_corridor` per 100 km of driving distance."""

    qualifying_stations_per_100km: float
    """:attr:`qualifying_stations_in_corridor` per 100 km of driving distance."""

    gap_count: int
    """Number of gaps the decomposition produced, including the two edge gaps."""

    max_gap_km: float
    """The longest gap. The headline figure of the whole analysis."""

    mean_gap_km: float
    """Arithmetic mean over all gaps."""

    gaps: tuple[CorridorGap, ...]
    """The reported gaps, largest first — see :data:`DEFAULT_TOP_GAPS` for why this is capped."""

    coverage_score: float
    """0-100 planning heuristic; see :func:`coverage_score`."""

    methodology: str
    """One German sentence stating exactly how the numbers above were produced."""


def coverage_score(*, max_gap_km: float, qualifying_stations_per_100km: float) -> float:
    """Blend the worst gap and the corridor density into a 0-100 planning heuristic.

    Two sub-scores, each 0-100 and each saturating at its target:

    * ``gap_score = 100 * target / max(max_gap, target)`` — 100 while the worst gap is inside
      :data:`TARGET_MAX_GAP_KM`, halving as the worst gap doubles past it.
    * ``density_score = min(100, 100 * stations_per_100km / target)`` against
      :data:`TARGET_FAST_STATIONS_PER_100KM`.

    Weighted 60/40 towards the gap (:data:`_SCORE_GAP_WEIGHT`). Identical to the formula in
    ``mart_charging_coverage.sql``; the two must not be allowed to disagree, which is why the
    weights and targets are named constants on both sides rather than literals in an expression.

    It is a heuristic, not a standard. That is exactly why every response carries
    :func:`methodology_sentence` alongside the number.
    """
    gap_score = 100.0 * TARGET_MAX_GAP_KM / max(max_gap_km, TARGET_MAX_GAP_KM)
    density_score = min(
        100.0,
        100.0 * qualifying_stations_per_100km / TARGET_FAST_STATIONS_PER_100KM,
    )
    return round(_SCORE_GAP_WEIGHT * gap_score + _SCORE_DENSITY_WEIGHT * density_score, 1)


def methodology_sentence(*, buffer_km: float, min_power_kw: float) -> str:
    """Spell out the parameters a coverage number was produced with, in German.

    BUILD_SPEC §7.2 requires the API to be able to state how a coverage figure was produced: a
    bare *"größte Lücke 84 km"* without its assumptions is not an answer, because the same
    corridor yields a very different number at 22 kW than at 150 kW. Building the sentence here,
    from the same constants the computation uses, makes it impossible for the wording and the
    parameters to drift apart.

    The wording deliberately matches ``mart_charging_coverage.sql`` so that a figure read off
    the warehouse and the same figure read off the API describe themselves identically.
    """
    return (
        f"Ladestationen im {buffer_km:g}-km-Korridor der Route; Lücken entlang der "
        f"Streckengeometrie (EPSG:{GERMANY_METRIC_SRID}) zwischen Standorten mit mindestens "
        f"{min_power_kw:g} kW, einschließlich der Abschnitte vor dem ersten und nach dem "
        f"letzten Ladepunkt. Zielwerte: maximale Lücke {TARGET_MAX_GAP_KM:g} km, "
        f"{TARGET_FAST_STATIONS_PER_100KM:g} Standorte je 100 km. "
        f"Bewertung: {_SCORE_GAP_WEIGHT * 100:g} % Lückenziel "
        f"(Ziel / größte Lücke, gedeckelt bei 100) + {_SCORE_DENSITY_WEIGHT * 100:g} % "
        f"Dichteziel (Standorte je 100 km / Ziel, gedeckelt bei 100)."
    )


# The whole analysis as one statement, so that a corridor is never described by two round trips
# that could see different data. Written as `text()` rather than through the ORM because
# `ST_Subdivide` in a select list, four window functions over a UNION ALL and a `LATERAL`-free
# fraction-to-substring round trip are all far clearer as SQL than as expression trees — and
# because BUILD_SPEC is explicit that geospatial work belongs in PostGIS.
#
# Every value that comes from the request is a bound parameter. Nothing is interpolated.
_COVERAGE_SQL: Final[str] = """
WITH target_routes AS (
    SELECT
        r.id                                        AS route_id,
        r.slug                                      AS route_slug,
        r.name                                      AS route_name,
        r.distance_m                                AS route_distance_m,
        r.geometry                                  AS geom_wgs84,
        ST_Transform(r.geometry, :metric_srid)      AS geom_metric
    FROM routes AS r
    WHERE (CAST(:route_id AS uuid) IS NULL OR r.id = CAST(:route_id AS uuid))
      AND (CAST(:route_slug AS text) IS NULL OR r.slug = CAST(:route_slug AS text))
      AND (NOT CAST(:demo_only AS boolean) OR r.is_demo)
),

-- MATERIALIZED so the reprojection of the line happens exactly once instead of once per
-- station: the corridor of a demo route holds thousands of them.
route_m AS MATERIALIZED (
    SELECT t.*, ST_Length(t.geom_metric) AS geometry_length_m
    FROM target_routes AS t
),

-- See _SUBDIVIDE_MAX_VERTICES: cutting the line leaves the answer identical and turns one
-- corridor-sized bounding box into a handful of tight ones.
route_parts AS (
    SELECT rm.route_id, ST_Subdivide(rm.geom_wgs84, :subdivide_max_vertices) AS part
    FROM route_m AS rm
),

-- DISTINCT because a station near a join between two pieces matches both of them.
corridor AS (
    SELECT DISTINCT
        p.route_id,
        s.id                            AS station_id,
        s.is_fast_charger,
        COALESCE(s.max_power_kw, 0.0)   AS power_kw,
        s.location
    FROM route_parts AS p
    INNER JOIN charging_stations AS s
        ON COALESCE(s.max_power_kw, 0.0) >= :candidate_min_power_kw
       AND ST_DWithin(s.location, p.part::geography, :buffer_m)
),

-- Position along the route as a fraction in [0, 1]. ST_LineLocatePoint clamps to the line, so
-- the fractions — and therefore every offset below — are inside the route by construction.
projected AS (
    SELECT
        c.route_id,
        c.station_id,
        c.is_fast_charger,
        c.power_kw,
        ST_LineLocatePoint(
            rm.geom_metric,
            ST_Transform(c.location::geometry, :metric_srid)
        ) AS route_fraction
    FROM corridor AS c
    INNER JOIN route_m AS rm ON rm.route_id = c.route_id
),

station_rollup AS (
    SELECT
        route_id,
        count(*)                                                AS stations_in_corridor,
        count(*) FILTER (WHERE is_fast_charger)                 AS fast_stations_in_corridor,
        count(*) FILTER (WHERE power_kw >= :min_power_kw)       AS qualifying_stations
    FROM projected
    GROUP BY route_id
),

qualifying AS (
    SELECT route_id, station_id, route_fraction
    FROM projected
    WHERE power_kw >= :min_power_kw
),

-- `station_id` breaks ties: two sites at the same motorway junction project to the same offset,
-- and an unstable order would make the gap list differ between identical requests.
ordered AS (
    SELECT
        route_id,
        route_fraction AS start_fraction,
        lead(route_fraction) OVER (
            PARTITION BY route_id
            ORDER BY route_fraction ASC, station_id ASC
        ) AS end_fraction
    FROM qualifying
),

bounds AS (
    SELECT route_id, min(route_fraction) AS first_fraction, max(route_fraction) AS last_fraction
    FROM qualifying
    GROUP BY route_id
),

gaps AS (
    SELECT route_id, start_fraction, end_fraction, 'between'::text AS gap_kind
    FROM ordered
    WHERE end_fraction IS NOT NULL

    UNION ALL

    -- Origin -> first qualifying station.
    SELECT rm.route_id, 0.0::double precision, b.first_fraction, 'origin'
    FROM route_m AS rm
    INNER JOIN bounds AS b ON b.route_id = rm.route_id

    UNION ALL

    -- Last qualifying station -> destination.
    SELECT rm.route_id, b.last_fraction, 1.0::double precision, 'destination'
    FROM route_m AS rm
    INNER JOIN bounds AS b ON b.route_id = rm.route_id

    UNION ALL

    -- No qualifying station at all: one gap the length of the route.
    SELECT rm.route_id, 0.0::double precision, 1.0::double precision, 'whole_route'
    FROM route_m AS rm
    WHERE NOT EXISTS (SELECT 1 FROM qualifying AS q WHERE q.route_id = rm.route_id)
),

measured AS (
    SELECT g.*, (g.end_fraction - g.start_fraction) * rm.geometry_length_m AS gap_m
    FROM gaps AS g
    INNER JOIN route_m AS rm ON rm.route_id = g.route_id
),

-- The aggregates are windows rather than a separate grouped CTE so that they are computed over
-- every gap while only the reported ones survive the outer WHERE — which is also what keeps
-- ST_LineSubstring from being evaluated for gaps nobody will draw.
ranked AS (
    SELECT
        measured.*,
        row_number() OVER (PARTITION BY route_id ORDER BY gap_m DESC, start_fraction ASC)
                                                        AS rank_in_route,
        count(*)     OVER (PARTITION BY route_id)       AS gap_count,
        max(gap_m)   OVER (PARTITION BY route_id)       AS max_gap_m,
        avg(gap_m)   OVER (PARTITION BY route_id)       AS mean_gap_m,
        sum(gap_m)   OVER (PARTITION BY route_id)       AS gap_total_m
    FROM measured
)

SELECT
    rm.route_id,
    rm.route_slug,
    rm.route_name,
    rm.route_distance_m,
    rm.geometry_length_m,
    sr.stations_in_corridor,
    sr.fast_stations_in_corridor,
    COALESCE(sr.qualifying_stations, 0)                 AS qualifying_stations,
    k.gap_count,
    k.max_gap_m,
    k.mean_gap_m,
    k.gap_total_m,
    k.gap_kind,
    k.start_fraction * rm.geometry_length_m             AS start_offset_m,
    k.end_fraction   * rm.geometry_length_m             AS end_offset_m,
    k.gap_m,
    -- Cut the substring in the metric grid the fractions were measured in, then bring it back
    -- to WGS 84 for the client. Cutting the WGS 84 line at a metric fraction would put the ends
    -- in the wrong place, by the same degree/metre confusion the projection avoids.
    CASE
        WHEN NOT CAST(:include_geometry AS boolean) THEN NULL
        WHEN k.end_fraction > k.start_fraction THEN ST_AsGeoJSON(
            ST_Transform(ST_LineSubstring(rm.geom_metric, k.start_fraction, k.end_fraction), 4326),
            6
        )::jsonb
    END                                                 AS gap_geometry
FROM ranked AS k
INNER JOIN route_m AS rm ON rm.route_id = k.route_id
LEFT JOIN station_rollup AS sr ON sr.route_id = k.route_id
-- `rank_in_route = 1` is what keeps a fully covered corridor in the result set when the caller
-- asked only for gaps above a threshold. Dropping it would make "no gap breaches 50 km" and
-- "no such route" the same empty answer, and /underserved needs to tell them apart.
WHERE k.rank_in_route <= :top_gaps
  AND (k.gap_m >= :min_reported_gap_m OR k.rank_in_route = 1)
ORDER BY rm.route_name ASC, k.gap_m DESC, k.start_fraction ASC
"""


def _check_gap_invariant(
    *,
    route_name: str,
    gap_total_m: float,
    geometry_length_m: float,
    gap_count: int,
) -> None:
    """Warn when the gaps do not tile the route exactly once.

    Interior gaps plus the two edge gaps must cover the route end to end and overlap nowhere, so
    their lengths must sum to the route's geometry length. Checking it is cheap and it is the
    only assertion that catches the three failures which leave every other number in the payload
    looking perfectly plausible: a missing edge gap, a station counted twice, and offsets
    measured against the driving distance instead of the geometry length.

    A warning rather than an exception: a corridor report that is 30 m out is still worth far
    more to an engineer than a 500, and the log line is what makes the defect findable.
    """
    drift_m = abs(gap_total_m - geometry_length_m)
    if drift_m > _GAP_SUM_TOLERANCE_M:
        _LOGGER.warning(
            "charging.coverage.gap_invariant_broken",
            route=route_name,
            gap_count=gap_count,
            gap_total_m=round(gap_total_m, 3),
            geometry_length_m=round(geometry_length_m, 3),
            drift_m=round(drift_m, 3),
            tolerance_m=_GAP_SUM_TOLERANCE_M,
        )


def _gap_of(row: sa.Row[Any]) -> CorridorGap:
    """Map one result row onto a :class:`CorridorGap`, in kilometres."""
    raw_geometry = row.gap_geometry
    geometry = GeoJSONLineString.model_validate(raw_geometry) if raw_geometry else None
    return CorridorGap(
        start_offset_km=round(float(row.start_offset_m) / 1000.0, 3),
        end_offset_km=round(float(row.end_offset_m) / 1000.0, 3),
        gap_km=round(float(row.gap_m) / 1000.0, 3),
        kind=str(row.gap_kind),
        geometry=geometry,
    )


async def compute_corridor_coverage(
    session: AsyncSession,
    *,
    route_id: UUID | None = None,
    route_slug: str | None = None,
    demo_only: bool = False,
    buffer_km: float = DEFAULT_BUFFER_KM,
    min_power_kw: float = FAST_CHARGER_THRESHOLD_KW,
    top_gaps: int = DEFAULT_TOP_GAPS,
    min_reported_gap_km: float = 0.0,
    include_geometry: bool = True,
    count_all_stations: bool = True,
) -> list[CorridorCoverage]:
    """Run the corridor analysis for every route matching the filters, in one query.

    Args:
        session: Read-only session; nothing here writes.
        route_id: Restrict to one route by primary key.
        route_slug: Restrict to one route by slug (``frankfurt-stuttgart``, …).
        demo_only: Keep only the seeded demo corridors. ``/underserved`` sets this, because
            ad-hoc routes planned through ``/routes/plan`` are not persisted analysis subjects.
        buffer_km: Corridor half-width in kilometres, measured on the spheroid.
        min_power_kw: A station counts as a charging opportunity from this power upwards.
        top_gaps: Cap on the number of individual gaps returned per corridor. Aggregates are
            always computed over all of them.
        min_reported_gap_km: Only return gaps at least this long — except that the longest gap
            of each corridor is always returned, so that a corridor which breaches nothing is
            still reported (with its worst gap) rather than disappearing. ``/underserved`` sets
            this to its ``max_gap_km`` threshold and filters the one survivor out itself.
        include_geometry: Whether to cut a GeoJSON LineString for each returned gap. The map
            needs it; a list view does not, and it is the most expensive column in the query.
        count_all_stations: Count the whole corridor, not only the qualifying stations. Set it
            to ``False`` when the caller does not need the totals: restricting the candidate set
            by power before the spatial test cuts the work by roughly half at 150 kW, and the
            totals are then reported as ``None`` rather than as a number that means something
            narrower than its name.

    Returns:
        One entry per matching route, ordered by route name. A route with no qualifying station
        is present with a single ``whole_route`` gap, never missing.
    """
    parameters: dict[str, Any] = {
        "route_id": route_id,
        "route_slug": route_slug,
        "demo_only": demo_only,
        "metric_srid": GERMANY_METRIC_SRID,
        "subdivide_max_vertices": _SUBDIVIDE_MAX_VERTICES,
        "buffer_m": buffer_km * 1000.0,
        "min_power_kw": min_power_kw,
        # Pushing the power threshold into the join turns the expensive spatial test into a
        # test over the stations that could possibly matter. 0.0 keeps every station, which is
        # what the full rollup needs.
        "candidate_min_power_kw": 0.0 if count_all_stations else min_power_kw,
        "top_gaps": top_gaps,
        "min_reported_gap_m": min_reported_gap_km * 1000.0,
        "include_geometry": include_geometry,
    }
    rows = (await session.execute(sa.text(_COVERAGE_SQL), parameters)).all()

    by_route: dict[UUID, list[sa.Row[Any]]] = {}
    for row in rows:
        by_route.setdefault(row.route_id, []).append(row)

    results: list[CorridorCoverage] = []
    for route_rows in by_route.values():
        head = route_rows[0]
        route_distance_km = float(head.route_distance_m) / 1000.0
        geometry_length_km = float(head.geometry_length_m) / 1000.0
        qualifying = int(head.qualifying_stations)
        # When the candidate set was restricted by power, the rollup counted only the stations
        # that could qualify. Reporting that number as "stations in corridor" would be a
        # narrower fact wearing a wider name, so the totals are withheld instead.
        stations = (
            int(head.stations_in_corridor)
            if count_all_stations and head.stations_in_corridor is not None
            else None
        )
        fast_stations = (
            int(head.fast_stations_in_corridor)
            if count_all_stations and head.fast_stations_in_corridor is not None
            else None
        )
        max_gap_km = float(head.max_gap_m) / 1000.0
        qualifying_per_100km = 100.0 * qualifying / route_distance_km

        _check_gap_invariant(
            route_name=str(head.route_name),
            gap_total_m=float(head.gap_total_m),
            geometry_length_m=float(head.geometry_length_m),
            gap_count=int(head.gap_count),
        )

        results.append(
            CorridorCoverage(
                route_id=head.route_id,
                route_slug=head.route_slug,
                route_name=str(head.route_name),
                buffer_km=buffer_km,
                min_power_kw=min_power_kw,
                route_distance_km=round(route_distance_km, 3),
                route_geometry_length_km=round(geometry_length_km, 3),
                stations_in_corridor=stations,
                fast_stations_in_corridor=fast_stations,
                qualifying_stations_in_corridor=qualifying,
                stations_per_100km=(
                    None if stations is None else round(100.0 * stations / route_distance_km, 2)
                ),
                qualifying_stations_per_100km=round(qualifying_per_100km, 2),
                gap_count=int(head.gap_count),
                max_gap_km=round(max_gap_km, 3),
                mean_gap_km=round(float(head.mean_gap_m) / 1000.0, 3),
                gaps=tuple(_gap_of(row) for row in route_rows),
                coverage_score=coverage_score(
                    max_gap_km=max_gap_km,
                    qualifying_stations_per_100km=qualifying_per_100km,
                ),
                methodology=methodology_sentence(
                    buffer_km=buffer_km,
                    min_power_kw=min_power_kw,
                ),
            )
        )

    return results
