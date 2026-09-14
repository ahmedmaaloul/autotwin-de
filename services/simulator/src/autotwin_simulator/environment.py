"""The world the simulated fleet drives through: roads, weather, traffic and charging sites.

Everything a vehicle needs to know about *where* it is comes from here, and every value this
module returns is one of exactly two kinds:

**Observed.** Read once, at engine start, from rows another pipeline ingested — ``routes`` and
``route_segments`` (OSRM geometry, OSM road classes), ``weather_observations`` (DWD),
``traffic_events`` (Autobahn GmbH) and ``charging_stations`` (Bundesnetzagentur). These are
real German open data.

**Synthetic.** Produced by a seeded, closed-form model documented in ``docs/data/simulation.md``
when the corresponding table is empty or the operator asked for a fixed scenario. Used for the
ambient temperature, precipitation, wind, the road gradient (always — see below) and the
traffic state on corridors with no ingested events.

Which one answered is never guessed: :class:`RouteEnvironment` carries
:attr:`~RouteEnvironment.weather_source` and :attr:`~RouteEnvironment.traffic_source`, the
engine logs them, and they are written into ``simulation_runs.config`` so a run can be read
back years later and understood.

**The gradient is always synthetic.** OSRM returns no elevation — neither the demo server nor a
locally built Geofabrik extract carries a height dimension, because OSM itself does not. A real
system samples a digital elevation model (SRTM 1", or Copernicus EU-DEM at 25 m for Europe)
along the route geometry and differentiates it. AutoTwin substitutes a seeded sum of three
sinusoids per corridor, tuned to German motorway grades: mean zero over the route, peaks around
±2.5 %, and stable for a given corridor so two runs of the same seed drive the same hills. It is
a plausible *shape*, not the terrain between Frankfurt and Stuttgart, and nothing that reads it
may claim otherwise.

Resolution is per 500 m bin, precomputed once per corridor and direction. That matters: the
engine must not issue a database query — or even a trigonometric elevation evaluation — per
vehicle per tick, and with several hundred vehicles on a laptop the difference between a lookup
and a query is the difference between a demo and a stall.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from autotwin_contracts import (
    ChargingCategory,
    Coordinate,
    RoadClass,
    TrafficSeverity,
    WeatherCondition,
)
from autotwin_core.db.models import Route, RouteSegment
from autotwin_core.db.types import from_wkb_linestring
from autotwin_core.geo import bearing_deg, cumulative_distances_m, point_at_offset
from autotwin_core.logging import get_logger
from autotwin_ml import ChargingCandidate

__all__ = [
    "BIN_LENGTH_M",
    "CHARGING_CORRIDOR_BUFFER_KM",
    "DIURNAL_AMPLITUDE_C",
    "GERMANY_ANNUAL_MEAN_C",
    "GRADIENT_WAVES",
    "SEASONAL_AMPLITUDE_C",
    "EnvironmentConfig",
    "ObservedTraffic",
    "ObservedWeather",
    "RoadConditions",
    "RouteEnvironment",
    "RouteSegmentProfile",
    "SimulationRoute",
    "TrafficIntensity",
    "WeatherMode",
    "build_route_environment",
    "derive_condition",
    "load_charging_candidates",
    "load_route_traffic",
    "load_route_weather",
    "load_simulation_routes",
    "rush_hour_weight",
    "seeded_rng",
    "synthetic_gradient_percent",
    "synthetic_precipitation_mm",
    "synthetic_temperature_c",
    "synthetic_wind",
]

_LOGGER = get_logger(__name__)


# --------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------


def seeded_rng(*parts: object) -> random.Random:
    """Build a reproducible random generator from a tuple of identifying parts.

    Every stochastic element of the simulator goes through here, and every call site passes the
    master seed plus enough context to make the stream unique: ``seeded_rng(seed, "driver",
    vehicle_id)``. Deriving one generator per concern rather than sharing one global stream is
    what makes the simulation reproducible *and* composable — adding a vehicle, or changing the
    order in which vehicles are stepped, cannot shift the numbers every other vehicle sees.

    The parts are joined into a string because :class:`random.Random` hashes a ``str`` seed with
    SHA-512 (``version=2``), which is stable across interpreter runs and across platforms;
    Python's ``hash()`` of a string is not, being randomised per process by PYTHONHASHSEED.
    """
    key = "|".join(str(part) for part in parts)
    # S311: Mersenne Twister is exactly right here. This is a physics simulation that must be
    # byte-for-byte reproducible from a seed, not a source of cryptographic material. It is the
    # only construction site in the package, so the suppression stays auditable.
    return random.Random(key)  # noqa: S311


# --------------------------------------------------------------------------------------
# Operator-facing scenario switches (persisted in simulation_runs.weather_mode / .traffic_intensity)
# --------------------------------------------------------------------------------------


class WeatherMode(StrEnum):
    """Where the ambient weather of a run comes from — ``simulation_runs.weather_mode``.

    ``observed`` is the honest default; the fixed-temperature modes exist because the winter
    consumption penalty is the single most interesting property of an EV energy model, and
    demonstrating it needs a run that is cold everywhere rather than a run that happens to be
    cold in the north.
    """

    observed = "observed"
    """Prefer ingested DWD observations near the corridor; fall back to ``synthetic``."""

    synthetic = "synthetic"
    """Always the seeded climatology, whatever is in ``weather_observations``."""

    cold = "cold"
    """Fixed -10 °C, dry, calm — a German winter morning."""

    mild = "mild"
    """Fixed +20 °C, dry, calm — the temperature at which the HVAC envelope is zero."""

    hot = "hot"
    """Fixed +35 °C, dry, calm — a heatwave with the air conditioning working."""

    @property
    def fixed_temperature_c(self) -> float | None:
        """The temperature this mode pins the whole fleet to, or ``None`` if it models one."""
        return _FIXED_TEMPERATURES_C.get(self)


_FIXED_TEMPERATURES_C: Final[dict[WeatherMode, float]] = {
    WeatherMode.cold: -10.0,
    WeatherMode.mild: 20.0,
    WeatherMode.hot: 35.0,
}


class TrafficIntensity(StrEnum):
    """How congested the corridors are — ``simulation_runs.traffic_intensity``."""

    observed = "observed"
    """Use ingested ``traffic_events`` on the corridor; fall back to the rush-hour model."""

    none = "none"
    """Free-flowing everywhere. The reference case for energy comparisons."""

    light = "light"
    """``low`` everywhere with occasional ``moderate`` stretches."""

    moderate = "moderate"
    """``moderate`` baseline with ``high`` stretches — a normal weekday."""

    heavy = "heavy"
    """``high`` baseline with ``severe`` stretches — Friday afternoon on the A5."""

    @property
    def baseline_weight(self) -> float:
        """Congestion pressure present outside the commuting peaks, 0-1.

        A *baseline* rather than a floor on the severity, and the difference matters. A floor
        makes every window of a ``moderate`` run at least ``moderate``, which leaves the
        ``traffic_severity_ordinal`` feature almost constant and teaches a model nothing. A
        baseline shifts the whole diurnal cycle upward while keeping night-time free-flowing, so
        the same run contains empty roads at 03:00 and congestion at 08:00.
        """
        return _TRAFFIC_BASELINES[self]

    @property
    def peak_severity(self) -> TrafficSeverity:
        """Worst severity this scenario can reach at the height of the rush hour."""
        return _TRAFFIC_PEAKS[self]


_TRAFFIC_BASELINES: Final[dict[TrafficIntensity, float]] = {
    TrafficIntensity.observed: 0.20,
    TrafficIntensity.none: 0.0,
    TrafficIntensity.light: 0.10,
    TrafficIntensity.moderate: 0.25,
    TrafficIntensity.heavy: 0.45,
}

_TRAFFIC_PEAKS: Final[dict[TrafficIntensity, TrafficSeverity]] = {
    TrafficIntensity.observed: TrafficSeverity.high,
    TrafficIntensity.none: TrafficSeverity.low,
    TrafficIntensity.light: TrafficSeverity.moderate,
    TrafficIntensity.moderate: TrafficSeverity.high,
    TrafficIntensity.heavy: TrafficSeverity.severe,
}


@dataclass(frozen=True, slots=True)
class EnvironmentConfig:
    """Scenario switches that apply to every corridor of one simulation run."""

    seed: int
    """Master seed; every synthetic field derives its own stream from it."""

    weather_mode: WeatherMode = WeatherMode.observed
    traffic_intensity: TrafficIntensity = TrafficIntensity.observed

    weather_radius_km: float = 60.0
    """Radius around the corridor searched for DWD observations.

    Germany's 10-minute network has roughly 400 reporting stations, so 60 km almost always
    finds one; a corridor that finds none falls back to the climatology rather than reaching
    across the country for a station whose weather is not this corridor's weather."""

    weather_max_age_hours: float = 6.0
    """Observations older than this are ignored. Matches ``AUTOTWIN_CACHE_TTL_SECONDS``."""

    traffic_buffer_km: float = 3.0
    """How far off the corridor a traffic event still counts as being on it."""

    traffic_event_reach_km: float = 2.5
    """Half-length of the stretch an event's severity is applied to, either side of its point.

    The Autobahn API publishes a representative point plus a line geometry; AutoTwin's ingested
    row keeps the point, so the extent is modelled as a fixed reach. 2.5 km either side is the
    order of magnitude of a German motorway roadworks zone."""


# --------------------------------------------------------------------------------------
# Corridors
# --------------------------------------------------------------------------------------

BIN_LENGTH_M: Final[float] = 500.0
"""Resolution of the precomputed environment arrays.

500 m is 14 seconds at 130 km/h — finer than any gradient, weather or traffic feature this
model resolves, and coarse enough that a 500 km corridor is 1 000 floats per field."""

CHARGING_CORRIDOR_BUFFER_KM: Final[float] = 5.0
"""How far off the corridor a charging site may be and still be a candidate stop.

Matches the default ``corridor_buffer_km`` of the route analysis (BUILD_SPEC §7.2) so that the
sites a simulated vehicle stops at are the same sites the coverage report counts."""


@dataclass(frozen=True, slots=True)
class RouteSegmentProfile:
    """Road class and speed limit for one stretch of a corridor.

    Built from a ``route_segments`` row when the corridor has been analysed, or from the
    corridor's own average speed when it has not — see :func:`_fallback_segments`.
    """

    start_offset_m: float
    distance_m: float
    road_class: RoadClass
    speed_limit_kmh: float | None

    @property
    def end_offset_m(self) -> float:
        """Offset of the far end of this stretch, in metres from the corridor origin."""
        return self.start_offset_m + self.distance_m


@dataclass(frozen=True, slots=True)
class SimulationRoute:
    """A corridor the fleet drives, in one direction.

    Frozen and precomputed: the cumulative-distance array is built once so that resolving a
    vehicle's position is a bisect rather than a walk down the polyline, which is the difference
    between O(n) and O(log n) several hundred times per tick.
    """

    slug: str
    """Identity of this corridor *and direction*, e.g. ``frankfurt-stuttgart:reverse``."""

    base_slug: str
    """Identity of the corridor irrespective of direction — the key the terrain is seeded on."""

    name: str
    """Human label, e.g. ``Frankfurt am Main → Stuttgart``."""

    coordinates: tuple[Coordinate, ...]
    """Geometry in travel order."""

    cumulative_m: tuple[float, ...]
    """Distance from the origin to each vertex; ``cumulative_m[-1]`` is the corridor length."""

    distance_m: float
    """Corridor length in metres, as the routing engine reported it."""

    duration_s: float
    """Free-flow driving time in seconds, as the routing engine reported it."""

    segments: tuple[RouteSegmentProfile, ...]
    """Road class and speed limit along the corridor, in travel order."""

    route_id: UUID | None = None
    """``routes.id`` when the corridor came from the database; ``None`` for an ad-hoc one."""

    is_reverse: bool = False
    """True when this is the return direction of its base corridor."""

    @property
    def length_m(self) -> float:
        """Length measured on the geometry, which is what the vehicle actually drives.

        Distinct from :attr:`distance_m`: the routing engine's figure is computed on its own
        internal network, and the polyline AutoTwin stores is a simplified overview of it. The
        vehicle integrates along the polyline, so the polyline's length is the one that has to
        bound its offset.
        """
        return self.cumulative_m[-1]

    @property
    def free_flow_speed_kmh(self) -> float:
        """Average speed the routing engine assumed, in km/h; 0.0 for a zero-duration route."""
        if self.duration_s <= 0.0:
            return 0.0
        return self.distance_m / 1000.0 / (self.duration_s / 3600.0)

    def coordinate_at(self, offset_m: float) -> Coordinate:
        """Position at ``offset_m`` metres along the corridor, clamped to its ends."""
        return point_at_offset(self.coordinates, offset_m, cumulative_m=self.cumulative_m)

    def reversed_route(self) -> SimulationRoute:
        """The same corridor driven the other way.

        Keeps :attr:`route_id` — it is the same physical corridor, and telemetry written for a
        return trip belongs to the same ``routes`` row — but flips the geometry, the segment
        offsets and :attr:`is_reverse`, which the terrain model reads to mirror and negate the
        gradient profile instead of inventing a second, unrelated set of hills.
        """
        coordinates = tuple(reversed(self.coordinates))
        cumulative = tuple(cumulative_distances_m(list(coordinates)))
        total_m = self.segments[-1].end_offset_m if self.segments else self.distance_m
        segments = tuple(
            RouteSegmentProfile(
                start_offset_m=total_m - segment.end_offset_m,
                distance_m=segment.distance_m,
                road_class=segment.road_class,
                speed_limit_kmh=segment.speed_limit_kmh,
            )
            for segment in reversed(self.segments)
        )
        origin, destination = _split_name(self.name)
        return SimulationRoute(
            slug=f"{self.base_slug}:reverse",
            base_slug=self.base_slug,
            name=f"{destination} → {origin}",
            coordinates=coordinates,
            cumulative_m=cumulative,
            distance_m=self.distance_m,
            duration_s=self.duration_s,
            segments=segments,
            route_id=self.route_id,
            is_reverse=not self.is_reverse,
        )


def _split_name(name: str) -> tuple[str, str]:
    """Split ``"A → B"`` into its endpoints, tolerating a name that is not in that shape."""
    for separator in (" → ", " -> ", " - "):
        if separator in name:
            origin, _, destination = name.partition(separator)
            return origin.strip(), destination.strip()
    return name, name


# --------------------------------------------------------------------------------------
# Observations read from the database
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ObservedWeather:
    """Ingested DWD weather near one corridor, collapsed to a corridor-wide state.

    A corridor-wide average rather than a per-bin interpolation, deliberately. The 10-minute
    network is ~400 stations for 357 000 km²; interpolating between two stations 80 km apart
    would dress a coarse observation up as a field, and the energy model's temperature
    sensitivity does not need that resolution to be right about winter.
    """

    temperature_c: float
    precipitation_mm: float
    wind_speed_ms: float
    observed_at: datetime
    station_count: int


@dataclass(frozen=True, slots=True)
class ObservedTrafficEvent:
    """One ingested traffic event projected onto a corridor."""

    offset_m: float
    """Where along the corridor the event sits."""

    severity: TrafficSeverity
    is_blocked: bool
    road_name: str | None


@dataclass(frozen=True, slots=True)
class ObservedTraffic:
    """Every ingested traffic event on one corridor."""

    events: tuple[ObservedTrafficEvent, ...]

    def __bool__(self) -> bool:
        """True when the corridor actually has ingested events to model."""
        return bool(self.events)


async def load_simulation_routes(
    session: AsyncSession,
    *,
    slugs: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[SimulationRoute]:
    """Load corridors from ``routes`` (+ ``route_segments``) in one round trip.

    Demo corridors come first, then everything else alphabetically, so a run started without
    ``--routes`` drives the corridors ``seed routes`` created. Unknown slugs are silently absent
    from the result rather than raising — the caller compares what it asked for with what it got
    and reports the difference, which produces a far more useful message than a KeyError from
    inside a query.
    """
    statement = (
        sa.select(Route)
        .options(selectinload(Route.segments))
        .order_by(Route.is_demo.desc(), Route.slug.asc(), Route.name.asc())
    )
    if slugs:
        statement = statement.where(Route.slug.in_(list(slugs)))
    if limit is not None:
        statement = statement.limit(limit)
    result = await session.execute(statement)
    return [_route_from_row(row) for row in result.scalars().unique().all()]


def _route_from_row(route: Route) -> SimulationRoute:
    """Adapt a ``routes`` row and its segments into the simulator's corridor value object."""
    coordinates = tuple(from_wkb_linestring(route.geometry))
    cumulative = tuple(cumulative_distances_m(list(coordinates)))
    segments = tuple(
        RouteSegmentProfile(
            start_offset_m=segment.start_offset_m,
            distance_m=segment.distance_m,
            road_class=segment.road_class,
            speed_limit_kmh=segment.speed_limit_kmh,
        )
        for segment in sorted(route.segments, key=_segment_ordinal)
    )
    slug = route.slug or f"route-{route.id}"
    return SimulationRoute(
        slug=slug,
        base_slug=slug,
        name=route.name,
        coordinates=coordinates,
        cumulative_m=cumulative,
        distance_m=route.distance_m,
        duration_s=route.duration_s,
        segments=segments or _fallback_segments(route.distance_m, route.duration_s),
        route_id=route.id,
    )


def _segment_ordinal(segment: RouteSegment) -> int:
    """Sort key for route segments; named so the ``sorted`` call reads as an intent."""
    return segment.ordinal


_AVERAGE_SPEED_ROAD_CLASSES: Final[tuple[tuple[float, RoadClass, float | None], ...]] = (
    (95.0, RoadClass.motorway, None),
    (75.0, RoadClass.trunk, 120.0),
    (55.0, RoadClass.primary, 100.0),
    (0.0, RoadClass.secondary, 80.0),
)
"""``(minimum average speed, class, speed limit)`` used when a corridor has no analysed segments.

Inferred from the corridor's *own* free-flow average speed rather than assumed: a route the
engine says averages 100 km/h is a motorway route, and one that averages 45 km/h is not. It is
a coarse stand-in for the ``route_segments`` rows that ``POST /routes/analyze`` writes, and the
simulator logs when it has had to use it."""


def _fallback_segments(distance_m: float, duration_s: float) -> tuple[RouteSegmentProfile, ...]:
    """One segment covering a whole corridor, classified by its average speed.

    Used only when ``route_segments`` is empty for the corridor, which happens before anything
    has analysed it. Stated rather than hidden: the road class drives the driver model's target
    speed, and a corridor silently treated as a residential street would produce a fleet that
    crawls to Stuttgart at 50 km/h.
    """
    speed_kmh = distance_m / 1000.0 / (duration_s / 3600.0) if duration_s > 0.0 else 0.0
    for threshold, road_class, limit in _AVERAGE_SPEED_ROAD_CLASSES:
        if speed_kmh >= threshold:
            return (
                RouteSegmentProfile(
                    start_offset_m=0.0,
                    distance_m=distance_m,
                    road_class=road_class,
                    speed_limit_kmh=limit,
                ),
            )
    raise AssertionError("the road-class table must end with a zero threshold")  # pragma: no cover


async def load_route_weather(
    session: AsyncSession,
    route: SimulationRoute,
    config: EnvironmentConfig,
    *,
    now: datetime,
) -> ObservedWeather | None:
    """Average the recent DWD observations within ``weather_radius_km`` of a corridor.

    One aggregate query per corridor, at engine start. ``ST_DWithin`` on the ``geography`` type
    answers in metres, which is why BUILD_SPEC §3 stores points as ``geography`` in the first
    place. Returns ``None`` when the corridor has no ``routes`` row (an ad-hoc corridor cannot
    be joined against) or when nothing recent is in range.
    """
    if route.route_id is None:
        return None
    statement = sa.text(
        """
        SELECT avg(w.temperature_c)      AS temperature_c,
               avg(w.precipitation_mm)   AS precipitation_mm,
               avg(w.wind_speed_ms)      AS wind_speed_ms,
               max(w.observed_at)        AS observed_at,
               count(DISTINCT w.weather_station_id) AS station_count
          FROM weather_observations AS w
          JOIN routes AS r ON r.id = :route_id
         WHERE w.observed_at >= :min_observed_at
           AND w.temperature_c IS NOT NULL
           AND ST_DWithin(w.location, CAST(r.geometry AS geography), :radius_m)
        """
    )
    row = (
        (
            await session.execute(
                statement,
                {
                    "route_id": route.route_id,
                    "min_observed_at": now - timedelta(hours=config.weather_max_age_hours),
                    "radius_m": config.weather_radius_km * 1000.0,
                },
            )
        )
        .mappings()
        .one()
    )
    if row["temperature_c"] is None or row["observed_at"] is None:
        return None
    return ObservedWeather(
        temperature_c=float(row["temperature_c"]),
        precipitation_mm=float(row["precipitation_mm"] or 0.0),
        wind_speed_ms=float(row["wind_speed_ms"] or 0.0),
        observed_at=row["observed_at"],
        station_count=int(row["station_count"] or 0),
    )


async def load_route_traffic(
    session: AsyncSession,
    route: SimulationRoute,
    config: EnvironmentConfig,
    *,
    now: datetime,
) -> ObservedTraffic:
    """Project the active ingested traffic events of a corridor onto its offsets.

    ``ST_LineLocatePoint`` returns the fraction along the line nearest the event, which becomes
    an offset in metres. Events whose validity window has already closed are excluded; events
    with no window at all are kept, because the Autobahn feed omits ``startTimestamp`` for every
    short-term roadworks item (``docs/data/sources.md`` §3) and dropping those would discard the
    most disruptive category.
    """
    if route.route_id is None:
        return ObservedTraffic(events=())
    statement = sa.text(
        """
        SELECT t.severity::text AS severity,
               t.is_blocked     AS is_blocked,
               t.road_name      AS road_name,
               ST_LineLocatePoint(
                   r.geometry, CAST(t.location AS geometry)
               ) AS position_fraction
          FROM traffic_events AS t
          JOIN routes AS r ON r.id = :route_id
         WHERE ST_DWithin(t.location, CAST(r.geometry AS geography), :buffer_m)
           AND (t.ends_at IS NULL OR t.ends_at >= :now)
           AND (t.starts_at IS NULL OR t.starts_at <= :now)
         ORDER BY position_fraction
        """
    )
    rows = (
        (
            await session.execute(
                statement,
                {
                    "route_id": route.route_id,
                    "buffer_m": config.traffic_buffer_km * 1000.0,
                    "now": now,
                },
            )
        )
        .mappings()
        .all()
    )
    events = tuple(
        ObservedTrafficEvent(
            offset_m=float(row["position_fraction"]) * route.length_m,
            severity=TrafficSeverity(row["severity"]),
            is_blocked=bool(row["is_blocked"]),
            road_name=row["road_name"],
        )
        for row in rows
    )
    return ObservedTraffic(events=events)


async def load_charging_candidates(
    session: AsyncSession,
    route: SimulationRoute,
    *,
    buffer_km: float = CHARGING_CORRIDOR_BUFFER_KM,
    min_power_kw: float = 50.0,
    limit: int = 200,
) -> list[ChargingCandidate]:
    """Charging sites within ``buffer_km`` of a corridor, ordered by offset along it.

    The same corridor query the coverage report of BUILD_SPEC §7.2 runs, reused so that a
    simulated vehicle stops at sites the analysis surfaces rather than at invented ones. Only
    DC-capable sites (``>= 50 kW``) are candidates: a vehicle mid-corridor plugging into an
    11 kW wallbox would sit there for four hours, which is not a decision a driver makes and not
    a stop a route planner should model.

    ``detour_km`` is the perpendicular distance **doubled** — off the corridor and back on.
    """
    if route.route_id is None:
        return []
    statement = sa.text(
        """
        SELECT s.external_id,
               s.operator,
               s.city,
               s.max_power_kw,
               s.charging_category::text AS charging_category,
               ST_Y(CAST(s.location AS geometry)) AS latitude,
               ST_X(CAST(s.location AS geometry)) AS longitude,
               ST_LineLocatePoint(r.geometry, CAST(s.location AS geometry)) AS position_fraction,
               ST_Distance(s.location, CAST(r.geometry AS geography)) AS offset_distance_m
          FROM charging_stations AS s
          JOIN routes AS r ON r.id = :route_id
         WHERE s.max_power_kw >= :min_power_kw
           AND ST_DWithin(s.location, CAST(r.geometry AS geography), :buffer_m)
         ORDER BY position_fraction
         LIMIT :limit
        """
    )
    rows = (
        (
            await session.execute(
                statement,
                {
                    "route_id": route.route_id,
                    "min_power_kw": min_power_kw,
                    "buffer_m": buffer_km * 1000.0,
                    "limit": limit,
                },
            )
        )
        .mappings()
        .all()
    )
    return [
        ChargingCandidate(
            station_id=row["external_id"],
            name=_candidate_name(row["operator"], row["city"], row["external_id"]),
            coordinate=Coordinate(
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
            ),
            offset_km=float(row["position_fraction"]) * route.length_m / 1000.0,
            detour_km=float(row["offset_distance_m"]) * 2.0 / 1000.0,
            max_power_kw=float(row["max_power_kw"]),
            operator=row["operator"],
            charging_category=ChargingCategory(row["charging_category"]),
        )
        for row in rows
    ]


def _candidate_name(operator: str | None, city: str | None, external_id: str) -> str:
    """Readable label for a charging site: ``"EnBW (Karlsruhe)"``, degrading to the raw id."""
    if operator and city:
        return f"{operator} ({city})"
    return operator or city or external_id


# --------------------------------------------------------------------------------------
# Synthetic climatology (BUILD_SPEC §0.4: stated assumptions, never silent invention)
# --------------------------------------------------------------------------------------

GERMANY_ANNUAL_MEAN_C: Final[float] = 9.3
"""Areal mean air temperature of Germany, °C. DWD's 1991-2020 reference period average."""

SEASONAL_AMPLITUDE_C: Final[float] = 8.8
"""Half the January-to-July swing of the German areal mean (~0.5 °C → ~18.1 °C)."""

SEASONAL_MINIMUM_DAY: Final[float] = 20.0
"""Day of year of the annual temperature minimum — the third week of January."""

DIURNAL_AMPLITUDE_C: Final[float] = 4.5
"""Half the typical day-night swing, in Kelvin.

A 9 K day-night range averaged over the year: German summers swing more (12-14 K inland) and
winters less (3-5 K under cloud). The seasonal dependence of the *amplitude* is not modelled —
one more place where this is an envelope rather than a climate model."""

DIURNAL_PEAK_HOUR: Final[float] = 15.0
"""Local hour of the daily maximum. Air temperature lags solar noon by ~3 h."""

LATITUDE_LAPSE_C_PER_DEGREE: Final[float] = 0.55
"""Cooling per degree of latitude north of :data:`REFERENCE_LATITUDE_DEG`.

Flensburg is roughly 4 °C cooler than Freiburg in the annual mean over 7.5 degrees; 0.55 °C per
degree reproduces that and stands in for continentality, altitude and maritime influence all at
once. It is a gradient, not a map."""

REFERENCE_LATITUDE_DEG: Final[float] = 51.0
"""Latitude the annual mean is quoted at — roughly the centre of Germany."""

SYNOPTIC_AMPLITUDE_C: Final[float] = 5.0
"""Amplitude of the multi-day warm/cold spells superimposed on the climatology, in Kelvin.

Together with the seasonal and diurnal terms this puts the synthetic temperature between about
-11 °C and +30 °C over a year at German latitudes, which is the range of *daily mean* extremes
the DWD network actually records. It does not reach the -20 °C and +38 °C of record days: those
are the tails the HVAC curve is clamped over anyway, and inventing them would widen the training
set with conditions the model has no way to be right about."""

SYNOPTIC_PERIOD_HOURS: Final[float] = 84.0
"""Period of those spells — 3.5 days, the order of magnitude of a mid-latitude weather system."""

LOCAL_TIME_OFFSET_HOURS: Final[float] = 1.0
"""CET. The diurnal curve is a solar phenomenon, so it is anchored to local time, not UTC.
Central European *Summer* Time is not modelled: an hour of phase is far below the accuracy of a
four-degree diurnal amplitude."""

PRECIPITATION_THRESHOLD: Final[float] = 0.68
"""Wetness above which the synthetic model produces rain.

Calibrated by measurement, not by assertion: at 0.68 roughly one telemetry sample in ten falls
in a wet window, which is the order of magnitude DWD reports for the fraction of hours with
measurable precipitation over Germany. It is a frequency, not a forecast — the model says
nothing about *when* it rains on any real day."""

PRECIPITATION_MAX_MM_PER_10MIN: Final[float] = 1.4
"""Peak synthetic rate, in mm per 10 minutes — the unit DWD's ``RWS_10`` reports. 1.4 mm/10 min
is 8.4 mm/h: heavy rain, but not a cloudburst."""

SNOW_TEMPERATURE_C: Final[float] = 1.0
"""At or below this, synthetic precipitation falls as snow."""

WIND_MEAN_MS: Final[float] = 3.6
"""Mean 10 m wind speed over Germany, m/s — the DWD long-term areal average."""

WIND_AMPLITUDE_MS: Final[float] = 2.2
"""Swing of the synthetic wind around its mean; floored at 0.2 m/s, so calm is possible."""

WIND_PREVAILING_DIRECTION_DEG: Final[float] = 240.0
"""Germany's prevailing wind is from the west-south-west (meteorological convention: the
bearing the wind blows *from*)."""

WIND_DIRECTION_SWING_DEG: Final[float] = 70.0
"""How far the synthetic direction wanders either side of the prevailing one."""

STORM_WIND_MS: Final[float] = 17.2
"""Beaufort 8 — the threshold DWD calls a "Sturm". Above it the condition is reported as storm."""

GRADIENT_WAVES: Final[tuple[tuple[float, float], ...]] = (
    (40_000.0, 1.20),
    (12_000.0, 0.80),
    (3_000.0, 0.55),
)
"""``(wavelength in metres, amplitude in percent)`` of the synthetic terrain.

Three scales stacked: a 40 km wave for the passage from the Rhine-Main basin up onto a plateau,
a 12 km wave for individual ridges, and a 3 km wave for the ground the road actually follows.
The sum peaks near ±2.5 %, which is where German motorway design keeps ordinary grades (the
steepest stretches of the A8 Albaufstieg reach 6 %, and this model does not claim to find
them). Mean zero over a corridor by construction, so a route neither gains nor loses net height
— a property a real DEM would *not* have, and one more reason this is labelled synthetic."""


def synthetic_temperature_c(
    when: datetime,
    latitude: float,
    *,
    synoptic_phase: float,
) -> float:
    """Ambient air temperature from a closed-form German climatology, in °C.

    Four additive terms: the annual mean, a seasonal cosine with its minimum in late January, a
    diurnal cosine peaking at 15:00 local time, and a latitude gradient. A slow synoptic
    oscillation seeded per corridor is superimposed so that two runs of the same seed see the
    same warm and cold spells but two corridors do not move in lockstep.

    This is a **climatology, not a forecast**: it reproduces the distribution German weather is
    drawn from, and says nothing about any particular day. ``WeatherMode.observed`` prefers real
    DWD rows precisely because they are the ones that are true.
    """
    day_of_year = when.timetuple().tm_yday + when.hour / 24.0
    seasonal = -SEASONAL_AMPLITUDE_C * math.cos(
        2.0 * math.pi * (day_of_year - SEASONAL_MINIMUM_DAY) / 365.25
    )
    local_hour = (when.hour + when.minute / 60.0 + LOCAL_TIME_OFFSET_HOURS) % 24.0
    diurnal = -DIURNAL_AMPLITUDE_C * math.cos(
        2.0 * math.pi * (local_hour - DIURNAL_PEAK_HOUR) / 24.0
    )
    latitude_term = (REFERENCE_LATITUDE_DEG - latitude) * LATITUDE_LAPSE_C_PER_DEGREE
    hours = when.timestamp() / 3600.0
    synoptic = SYNOPTIC_AMPLITUDE_C * math.sin(
        2.0 * math.pi * hours / SYNOPTIC_PERIOD_HOURS + synoptic_phase
    )
    return GERMANY_ANNUAL_MEAN_C + seasonal + diurnal + latitude_term + synoptic


def synthetic_precipitation_mm(when: datetime, *, phases: tuple[float, float]) -> float:
    """Precipitation in mm over the preceding 10 minutes — the unit of DWD's ``RWS_10``.

    Two sine waves of incommensurable period (11 h and 29 h) beat against each other to produce
    wet spells that are neither periodic on a human timescale nor independent from one minute to
    the next. Everything below :data:`PRECIPITATION_THRESHOLD` is dry, which is most of the time.
    """
    hours = when.timestamp() / 3600.0
    wetness = 0.5 * (
        math.sin(2.0 * math.pi * hours / 11.0 + phases[0])
        + math.sin(2.0 * math.pi * hours / 29.0 + phases[1])
    )
    if wetness <= PRECIPITATION_THRESHOLD:
        return 0.0
    intensity = (wetness - PRECIPITATION_THRESHOLD) / (1.0 - PRECIPITATION_THRESHOLD)
    return intensity * PRECIPITATION_MAX_MM_PER_10MIN


def synthetic_wind(when: datetime, *, phase: float) -> tuple[float, float]:
    """Wind speed in m/s and the bearing it blows *from*, in degrees.

    A slow 17-hour oscillation around the German mean, and a direction wandering either side of
    the prevailing west-south-westerly. The speed is floored just above zero rather than allowed
    to cross into negative territory, which would silently flip the direction.
    """
    hours = when.timestamp() / 3600.0
    speed = WIND_MEAN_MS + WIND_AMPLITUDE_MS * math.sin(2.0 * math.pi * hours / 17.0 + phase)
    direction = WIND_PREVAILING_DIRECTION_DEG + WIND_DIRECTION_SWING_DEG * math.sin(
        2.0 * math.pi * hours / 53.0 + phase
    )
    return max(0.2, speed), direction % 360.0


def synthetic_gradient_percent(offset_m: float, *, phases: Sequence[float]) -> float:
    """Longitudinal gradient in percent at ``offset_m`` along a corridor.

    Sum of :data:`GRADIENT_WAVES`. Positive is uphill in the direction of travel.
    """
    return sum(
        amplitude * math.sin(2.0 * math.pi * offset_m / wavelength + phase)
        for (wavelength, amplitude), phase in zip(GRADIENT_WAVES, phases, strict=True)
    )


_RUSH_HOURS: Final[tuple[tuple[float, float, float], ...]] = (
    (6.5, 9.0, 1.0),
    (15.5, 18.5, 1.0),
    (11.0, 13.5, 0.45),
)
"""``(start hour, end hour, weight)`` of the German weekday commuting peaks, local time.

The midday shoulder is real and much weaker than the two commuter peaks. Weekends get a flat
0.35 weight instead — freight traffic is banned on Sundays, and leisure traffic peaks on Sunday
afternoons rather than at 08:00."""

_WEEKEND_TRAFFIC_WEIGHT: Final[float] = 0.35
"""Flat weekend congestion propensity — no commute, but Sunday-afternoon leisure traffic."""

_SATURDAY: Final[int] = 5
"""``datetime.weekday()`` of Saturday; 5 and 6 are the weekend."""


def rush_hour_weight(when: datetime) -> float:
    """Congestion propensity from 0 (night) to 1 (peak commute), for a German weekday.

    Deliberately a smooth raised-cosine inside each window rather than a step: traffic builds
    and clears, and a step would put a discontinuity in every vehicle's target speed at 09:00.
    """
    local_hour = (when.hour + when.minute / 60.0 + LOCAL_TIME_OFFSET_HOURS) % 24.0
    if when.weekday() >= _SATURDAY:
        return _WEEKEND_TRAFFIC_WEIGHT
    weight = 0.0
    for start, end, peak in _RUSH_HOURS:
        if start <= local_hour <= end:
            phase = (local_hour - start) / (end - start)
            weight = max(weight, peak * 0.5 * (1.0 - math.cos(2.0 * math.pi * phase)))
    return weight


# --------------------------------------------------------------------------------------
# The resolved environment of one corridor
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoadConditions:
    """Everything the vehicle and driver models need about one point on a corridor.

    Frozen and built per physics step, so it is deliberately small and holds no references back
    into the environment that produced it.
    """

    offset_m: float
    road_class: RoadClass
    speed_limit_kmh: float | None
    gradient_percent: float
    heading_deg: float
    temperature_c: float
    precipitation_mm: float
    wind_speed_ms: float
    wind_direction_deg: float
    traffic_severity: TrafficSeverity
    condition: WeatherCondition

    @property
    def is_wet(self) -> bool:
        """True when the road surface is affected — the driver model slows down for it."""
        return self.precipitation_mm > 0.0


class RouteEnvironment:
    """The resolved world along one corridor in one direction.

    Built once per corridor by :func:`build_route_environment`; after that
    :meth:`conditions_at` is pure arithmetic on precomputed arrays plus a handful of
    trigonometric terms, with no I/O and no allocation beyond the returned value object. That
    property is the reason several hundred vehicles fit in a laptop's tick budget.
    """

    __slots__ = (
        "_bin_count",
        "_config",
        "_gradient_phases",
        "_heading_deg",
        "_observed_weather",
        "_precipitation_phases",
        "_road_class",
        "_speed_limit_kmh",
        "_synoptic_phase",
        "_traffic_base",
        "_traffic_propensity",
        "_wind_phase",
        "charging_candidates",
        "route",
        "traffic_source",
        "weather_source",
    )

    def __init__(
        self,
        route: SimulationRoute,
        config: EnvironmentConfig,
        *,
        observed_weather: ObservedWeather | None = None,
        observed_traffic: ObservedTraffic | None = None,
        charging_candidates: Sequence[ChargingCandidate] = (),
    ) -> None:
        """Precompute the per-bin arrays and the seeded phases of the synthetic fields."""
        self.route = route
        self._config = config
        self.charging_candidates = tuple(charging_candidates)

        terrain_rng = seeded_rng(config.seed, "terrain", route.base_slug)
        self._gradient_phases = tuple(
            terrain_rng.uniform(0.0, 2.0 * math.pi) for _ in GRADIENT_WAVES
        )
        weather_rng = seeded_rng(config.seed, "weather", route.base_slug)
        self._synoptic_phase = weather_rng.uniform(0.0, 2.0 * math.pi)
        self._precipitation_phases = (
            weather_rng.uniform(0.0, 2.0 * math.pi),
            weather_rng.uniform(0.0, 2.0 * math.pi),
        )
        self._wind_phase = weather_rng.uniform(0.0, 2.0 * math.pi)

        self._bin_count = max(1, math.ceil(route.length_m / BIN_LENGTH_M))
        self._road_class, self._speed_limit_kmh = _bin_road_profile(route, self._bin_count)
        self._heading_deg = _bin_headings(route, self._bin_count)

        traffic_rng = seeded_rng(config.seed, "traffic", route.base_slug)
        self._traffic_propensity = tuple(
            traffic_rng.betavariate(2.0, 3.0) for _ in range(self._bin_count)
        )
        self._traffic_base, self.traffic_source = _bin_observed_traffic(
            observed_traffic,
            config,
            bin_count=self._bin_count,
        )

        self._observed_weather = observed_weather
        self.weather_source = _weather_source(config.weather_mode, observed_weather)

    @property
    def config(self) -> EnvironmentConfig:
        """The scenario switches this environment was built with."""
        return self._config

    @property
    def bin_count(self) -> int:
        """Number of 500 m bins the corridor was discretised into."""
        return self._bin_count

    def _bin_index(self, offset_m: float) -> int:
        """Clamp an offset onto a bin index. Offsets past the end clamp to the last bin."""
        index = int(offset_m // BIN_LENGTH_M)
        if index < 0:
            return 0
        if index >= self._bin_count:
            return self._bin_count - 1
        return index

    def gradient_percent(self, offset_m: float) -> float:
        """Synthetic gradient at ``offset_m``, mirrored and negated for the return direction.

        Mirroring rather than re-seeding is what makes the terrain *consistent*: a climb on the
        way south is the same descent on the way north, which is the single most visible
        sanity check a reviewer can run on simulated consumption by direction.
        """
        if self.route.is_reverse:
            mirrored = self.route.length_m - offset_m
            return -synthetic_gradient_percent(mirrored, phases=self._gradient_phases)
        return synthetic_gradient_percent(offset_m, phases=self._gradient_phases)

    def traffic_severity(self, offset_m: float, when: datetime) -> TrafficSeverity:
        """Traffic state at a point and time.

        Ingested events win where they exist. Everywhere else the severity is the seeded
        per-bin propensity scaled by the time of day: the same stretch of the A5 is congested at
        08:00 and empty at 03:00, which is what makes the simulated energy penalty of traffic
        vary within a single vehicle's day rather than being a constant offset.
        """
        index = self._bin_index(offset_m)
        observed = self._traffic_base[index]
        if observed is not None:
            return observed
        intensity = self._config.traffic_intensity
        if intensity is TrafficIntensity.none:
            return TrafficSeverity.low
        baseline = intensity.baseline_weight
        load = baseline + (1.0 - baseline) * rush_hour_weight(when)
        pressure = self._traffic_propensity[index] * load
        return _severity_from_pressure(pressure, intensity.peak_severity)

    def weather_at(
        self, coordinate: Coordinate, when: datetime
    ) -> tuple[float, float, float, float]:
        """``(temperature_c, precipitation_mm, wind_speed_ms, wind_direction_deg)``.

        A fixed-temperature mode pins the temperature and reports dry, calm air, because the
        point of those modes is to isolate the temperature sensitivity of the energy model from
        everything else. Observed weather is held constant for the run (see
        :class:`ObservedWeather`); the synthetic model varies with place and time.
        """
        mode = self._config.weather_mode
        fixed = mode.fixed_temperature_c
        if fixed is not None:
            return fixed, 0.0, 0.0, WIND_PREVAILING_DIRECTION_DEG
        observed = self._observed_weather
        if mode is WeatherMode.observed and observed is not None:
            return (
                observed.temperature_c,
                observed.precipitation_mm,
                observed.wind_speed_ms,
                WIND_PREVAILING_DIRECTION_DEG,
            )
        temperature_c = synthetic_temperature_c(
            when,
            coordinate.latitude,
            synoptic_phase=self._synoptic_phase,
        )
        precipitation_mm = synthetic_precipitation_mm(when, phases=self._precipitation_phases)
        wind_speed_ms, wind_direction_deg = synthetic_wind(when, phase=self._wind_phase)
        return temperature_c, precipitation_mm, wind_speed_ms, wind_direction_deg

    def conditions_at(self, offset_m: float, when: datetime) -> RoadConditions:
        """Resolve every field of :class:`RoadConditions` at one point and time."""
        index = self._bin_index(offset_m)
        coordinate = self.route.coordinate_at(offset_m)
        temperature_c, precipitation_mm, wind_speed_ms, wind_direction_deg = self.weather_at(
            coordinate, when
        )
        return RoadConditions(
            offset_m=offset_m,
            road_class=self._road_class[index],
            speed_limit_kmh=self._speed_limit_kmh[index],
            gradient_percent=self.gradient_percent(offset_m),
            heading_deg=self._heading_deg[index],
            temperature_c=temperature_c,
            precipitation_mm=precipitation_mm,
            wind_speed_ms=wind_speed_ms,
            wind_direction_deg=wind_direction_deg,
            traffic_severity=self.traffic_severity(offset_m, when),
            condition=derive_condition(temperature_c, precipitation_mm, wind_speed_ms),
        )

    def next_charging_candidate(
        self,
        offset_km: float,
        *,
        max_offset_km: float,
        min_power_kw: float = 0.0,
    ) -> ChargingCandidate | None:
        """The strongest charging site between ``offset_km`` and ``max_offset_km``.

        "Strongest" rather than "nearest" because charging time dominates a stop's cost: a
        300 kW site 20 km further on is almost always the better decision than a 50 kW site the
        vehicle reaches first, and the vehicle only ever asks this question inside the range it
        can still reach. Ties break on the offset so the answer is deterministic.
        """
        best: ChargingCandidate | None = None
        for candidate in self.charging_candidates:
            if candidate.offset_km < offset_km or candidate.offset_km > max_offset_km:
                continue
            if candidate.max_power_kw < min_power_kw:
                continue
            if best is None or candidate.max_power_kw > best.max_power_kw:
                best = candidate
        return best


def _severity_from_pressure(pressure: float, peak: TrafficSeverity) -> TrafficSeverity:
    """Map a 0-1 congestion pressure onto the four-level scale, capped at ``peak``."""
    if pressure < 0.25:
        severity = TrafficSeverity.low
    elif pressure < 0.45:
        severity = TrafficSeverity.moderate
    elif pressure < 0.65:
        severity = TrafficSeverity.high
    else:
        severity = TrafficSeverity.severe
    return severity if severity.ordinal <= peak.ordinal else peak


def derive_condition(
    temperature_c: float,
    precipitation_mm: float,
    wind_speed_ms: float,
) -> WeatherCondition:
    """Coarse weather condition from the three values the simulator actually models.

    The simulator's own rule, deliberately simpler than the DWD adapter's
    ``derive_condition``: this one has no humidity and no pressure to work with, so it can only
    distinguish rain from snow from storm from clear. Nothing downstream treats it as an
    observation.
    """
    if precipitation_mm > 0.0:
        return (
            WeatherCondition.snow
            if temperature_c <= SNOW_TEMPERATURE_C
            else (WeatherCondition.rain)
        )
    if wind_speed_ms >= STORM_WIND_MS:
        return WeatherCondition.storm
    return WeatherCondition.clear


def _bin_road_profile(
    route: SimulationRoute,
    bin_count: int,
) -> tuple[tuple[RoadClass, ...], tuple[float | None, ...]]:
    """Rasterise the corridor's segment profile onto the 500 m bin grid.

    A bin takes the class of the segment its midpoint falls in — a bin straddling a boundary has
    to pick one, and the midpoint is the choice that cannot be biased by segment ordering.
    """
    classes: list[RoadClass] = []
    limits: list[float | None] = []
    segments = route.segments
    index = 0
    for bin_index in range(bin_count):
        midpoint_m = (bin_index + 0.5) * BIN_LENGTH_M
        while index + 1 < len(segments) and segments[index].end_offset_m <= midpoint_m:
            index += 1
        segment = segments[index] if segments else None
        classes.append(segment.road_class if segment else RoadClass.unknown)
        limits.append(segment.speed_limit_kmh if segment else None)
    return tuple(classes), tuple(limits)


def _bin_headings(route: SimulationRoute, bin_count: int) -> tuple[float, ...]:
    """Course over ground at the start of each bin, in degrees clockwise from north."""
    headings: list[float] = []
    for bin_index in range(bin_count):
        start_m = bin_index * BIN_LENGTH_M
        end_m = min(start_m + BIN_LENGTH_M, route.length_m)
        start = route.coordinate_at(start_m)
        end = route.coordinate_at(end_m)
        headings.append(bearing_deg(start, end))
    return tuple(headings)


def _bin_observed_traffic(
    observed: ObservedTraffic | None,
    config: EnvironmentConfig,
    *,
    bin_count: int,
) -> tuple[tuple[TrafficSeverity | None, ...], str]:
    """Paint ingested events onto the bin grid; ``None`` means "no observation for this bin".

    Returns the array and the provenance label the engine logs. A corridor with no events at all
    reports ``synthetic_rush_hour`` rather than pretending the road is empty, because an empty
    ``traffic_events`` table means nothing has been ingested, not that Germany is free-flowing.
    """
    if config.traffic_intensity is not TrafficIntensity.observed:
        return (None,) * bin_count, f"scenario:{config.traffic_intensity.value}"
    if observed is None or not observed:
        return (None,) * bin_count, "synthetic_rush_hour"
    painted: list[TrafficSeverity | None] = [None] * bin_count
    reach_bins = max(1, int(config.traffic_event_reach_km * 1000.0 / BIN_LENGTH_M))
    for event in observed.events:
        centre = int(event.offset_m // BIN_LENGTH_M)
        severity = TrafficSeverity.severe if event.is_blocked else event.severity
        for index in range(centre - reach_bins, centre + reach_bins + 1):
            if not 0 <= index < bin_count:
                continue
            current = painted[index]
            if current is None or severity.ordinal > current.ordinal:
                painted[index] = severity
    return tuple(painted), f"autobahn_events:{len(observed.events)}"


def _weather_source(mode: WeatherMode, observed: ObservedWeather | None) -> str:
    """Provenance label for the weather this environment serves."""
    if mode.fixed_temperature_c is not None:
        return f"scenario:{mode.value}"
    if mode is WeatherMode.observed and observed is not None:
        return f"dwd_observations:{observed.station_count}"
    return "synthetic_climatology"


async def build_route_environment(
    route: SimulationRoute,
    config: EnvironmentConfig,
    *,
    session: AsyncSession | None = None,
    now: datetime | None = None,
) -> RouteEnvironment:
    """Resolve one corridor's environment, reading the database once when one is available.

    Called ``vehicle_count``-independent times — once per corridor and direction, at engine
    start — which is the whole point: the hot loop never touches the database.

    Without a session (offline training-data generation, unit tests) every field is synthetic
    and the environment says so through :attr:`RouteEnvironment.weather_source`.
    """
    resolved_now = now if now is not None else datetime.now(UTC)
    observed_weather: ObservedWeather | None = None
    observed_traffic: ObservedTraffic | None = None
    candidates: Sequence[ChargingCandidate] = ()
    if session is not None:
        observed_weather = await load_route_weather(session, route, config, now=resolved_now)
        observed_traffic = await load_route_traffic(session, route, config, now=resolved_now)
        candidates = await load_charging_candidates(session, route)
    environment = RouteEnvironment(
        route,
        config,
        observed_weather=observed_weather,
        observed_traffic=observed_traffic,
        charging_candidates=candidates,
    )
    _LOGGER.info(
        "simulator.environment.built",
        route=route.slug,
        length_km=round(route.length_m / 1000.0, 1),
        bins=environment.bin_count,
        weather_source=environment.weather_source,
        traffic_source=environment.traffic_source,
        charging_candidates=len(environment.charging_candidates),
    )
    return environment
