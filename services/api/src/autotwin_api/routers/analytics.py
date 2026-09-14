"""Cross-cutting analyses: energy against its drivers, and the regional infrastructure ranking.

The two endpoints here sit on opposite sides of the honesty rule (BUILD_SPEC §0.2), and that is
the most important thing about this module:

* ``/analytics/energy`` reads the ``telemetry`` table, every row of which is produced by the
  simulator. The response says so in four places — ``data_origin``, ``is_simulated`` and a
  bilingual disclaimer — because a "consumption versus temperature" curve is precisely the kind
  of chart that travels without its caption.
* ``/analytics/regions`` reads the Bundesnetzagentur register and is official data, down to the
  ``X-AutoTwin-Data-Mode`` header it shares with ``/api/v1/charging/*``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_api.deps import DbSession
from autotwin_api.middleware import set_data_mode
from autotwin_api.routers.charging import register_data_mode
from autotwin_api.schemas.analytics import (
    EnergyAnalysis,
    EnergyBucket,
    EnergyDimension,
    RegionComparison,
    RegionStat,
)
from autotwin_contracts import (
    FAST_CHARGER_THRESHOLD_KW,
    Bundesland,
    ChargingCategory,
    DataOrigin,
    RoadClass,
    TrafficSeverity,
    VehicleState,
    utc_now,
)
from autotwin_core.db.models import ChargingPoint, ChargingStation, Telemetry

__all__ = ["router"]

router = APIRouter()

_PER_AREA_UNIT_KM2: Final[float] = 1000.0
"""Densities are quoted per 1 000 km². Per km² would be three leading zeros for every state."""

_AREA_SOURCE: Final[str] = (
    "Statistisches Bundesamt (Destatis), Fläche der Bundesländer — the same figures as "
    "dbt/seeds/bundesland_reference.csv"
)

_BUNDESLAND_AREA_KM2: Final[dict[Bundesland, float]] = {
    Bundesland.BW: 35748.0,
    Bundesland.BY: 70542.0,
    Bundesland.BE: 891.0,
    Bundesland.BB: 29654.0,
    Bundesland.HB: 420.0,
    Bundesland.HH: 755.0,
    Bundesland.HE: 21116.0,
    Bundesland.MV: 23295.0,
    Bundesland.NI: 47710.0,
    Bundesland.NW: 34112.0,
    Bundesland.RP: 19858.0,
    Bundesland.SL: 2571.0,
    Bundesland.SN: 18450.0,
    Bundesland.ST: 20459.0,
    Bundesland.SH: 15804.0,
    Bundesland.TH: 16202.0,
}
"""Land area per federal state in km².

A constant rather than a query: the areas are not AutoTwin's data, they change about once a
decade, and depending on the dbt seed would make an API endpoint fail whenever the warehouse
had not been built. The numbers are the same ones ``bundesland_reference.csv`` carries, so the
API and the marts divide by the same denominator.
"""


# ---------------------------------------------------------------------------------------------
# Energy
# ---------------------------------------------------------------------------------------------

_DISCLAIMER_DE: Final[str] = (
    "SIMULIERT — diese Auswertung beruht auf Telemetrie der AutoTwin-Fahrzeugsimulation, "
    "nicht auf Messungen realer Fahrzeuge."
)
_DISCLAIMER_EN: Final[str] = (
    "SIMULATED — this analysis is computed from AutoTwin's vehicle simulation telemetry, "
    "not from measurements of real vehicles."
)

_TEMPERATURE_MIN_C: Final[float] = -15.0
_TEMPERATURE_MAX_C: Final[float] = 40.0
_TEMPERATURE_BUCKETS: Final[int] = 11
"""5 °C wide, from -15 °C to 40 °C — the range German weather actually spends its year in."""

_SPEED_MIN_KMH: Final[float] = 0.0
_SPEED_MAX_KMH: Final[float] = 200.0
_SPEED_BUCKETS: Final[int] = 10
"""20 km/h wide. The top bucket is open-ended, which on unrestricted Autobahn stretches is not
a rounding convenience but a real part of the distribution."""

_CONGESTION_FREE_FLOW: Final[float] = 0.9
_CONGESTION_MODERATE: Final[float] = 0.7
_CONGESTION_HIGH: Final[float] = 0.5
"""Speed as a fraction of the reference speed, mapped onto :class:`TrafficSeverity`.

The telemetry table has no traffic column (BUILD_SPEC §3.2), so congestion has to be *derived*,
and the ratio of achieved speed to the speed the road would allow is the standard proxy: at or
above 90 % of it traffic is free-flowing, below half of it something is badly wrong. The
thresholds are named here and restated in `method_en` on every response, because a derived
dimension whose derivation is not published is indistinguishable from a measured one.
"""

_ROAD_CLASS_REFERENCE_KMH: Final[dict[RoadClass, float]] = {
    RoadClass.motorway: 130.0,
    RoadClass.trunk: 100.0,
    RoadClass.primary: 100.0,
    RoadClass.secondary: 80.0,
    RoadClass.tertiary: 70.0,
    RoadClass.residential: 50.0,
    RoadClass.service: 30.0,
}
"""Free-flow reference speed per road class, in km/h.

``telemetry.speed_limit_kmh`` is nullable and the simulator does not always know a limit for the
stretch a vehicle is on, so without a fallback the congestion dimension would silently return
nothing at all. These are the customary German values — 130 km/h is the Autobahn
*Richtgeschwindigkeit*, the advisory speed that also serves as the free-flow reference on
unrestricted stretches — and they are the same defaults OSM applies to a way with no `maxspeed`.

``RoadClass.unknown`` is deliberately absent: there is no honest reference speed for a stretch
whose class is not known, so those rows are reported as ``excluded_samples`` rather than being
compared against a number somebody made up.
"""

_CONGESTION_LABELS: Final[dict[int, TrafficSeverity]] = {
    1: TrafficSeverity.low,
    2: TrafficSeverity.moderate,
    3: TrafficSeverity.high,
    4: TrafficSeverity.severe,
}


_REFERENCE_SPEED_SUMMARY: Final[str] = ", ".join(
    f"{road_class.value} {speed:g} km/h" for road_class, speed in _ROAD_CLASS_REFERENCE_KMH.items()
)
"""The reference table rendered for `method_de`/`method_en`, so the published sentence cannot
fall out of step with the numbers the query actually divides by."""

_TEMPERATURE_BAND_C: Final[float] = (_TEMPERATURE_MAX_C - _TEMPERATURE_MIN_C) / _TEMPERATURE_BUCKETS
_SPEED_BAND_KMH: Final[float] = (_SPEED_MAX_KMH - _SPEED_MIN_KMH) / _SPEED_BUCKETS


@dataclass(frozen=True, slots=True)
class _DimensionSpec:
    """How one dimension turns a telemetry row into a bucket."""

    unit: str
    bucket_index: sa.ColumnElement[Any]
    """1-based bucket number, or NULL for a row this dimension cannot place."""

    label: Callable[[int], tuple[str, float | None, float | None]]
    """Bucket number → (human label, inclusive lower edge, exclusive upper edge)."""

    method_de: str
    method_en: str


def _linear_label(
    *,
    minimum: float,
    maximum: float,
    buckets: int,
    unit: str,
) -> Callable[[int], tuple[str, float | None, float | None]]:
    """Label factory for a ``width_bucket`` over a fixed range.

    ``width_bucket`` answers 0 for a value below ``minimum`` and ``buckets + 1`` for one at or
    above ``maximum``; both are open-ended and are labelled as such rather than being folded
    into the neighbouring bucket, which would quietly overstate the edge of the distribution.
    """
    width = (maximum - minimum) / buckets

    def label(index: int) -> tuple[str, float | None, float | None]:
        if index <= 0:
            return (f"< {minimum:g} {unit}", None, minimum)
        if index > buckets:
            return (f">= {maximum:g} {unit}", maximum, None)
        lower = minimum + (index - 1) * width
        upper = lower + width
        return (f"{lower:g} to {upper:g} {unit}", lower, upper)

    return label


def _congestion_label(index: int) -> tuple[str, float | None, float | None]:
    """Label factory for the derived congestion dimension."""
    severity = _CONGESTION_LABELS.get(index, TrafficSeverity.low)
    edges = {
        1: (_CONGESTION_FREE_FLOW, None),
        2: (_CONGESTION_MODERATE, _CONGESTION_FREE_FLOW),
        3: (_CONGESTION_HIGH, _CONGESTION_MODERATE),
        4: (0.0, _CONGESTION_HIGH),
    }
    lower, upper = edges.get(index, (None, None))
    return (severity.value, lower, upper)


def _usable_condition() -> sa.ColumnElement[bool]:
    """Rows a consumption figure may be read off.

    A stationary vehicle has a kWh/100 km of whatever its auxiliaries draw divided by zero
    distance; including those rows would not add noise, it would add nonsense. ``state`` and a
    positive speed together describe a vehicle that is actually covering ground.
    """
    return sa.and_(Telemetry.state == VehicleState.driving, Telemetry.speed_kmh > 0.0)


def _dimension_spec(dimension: EnergyDimension) -> _DimensionSpec:
    """Build the bucketing rule for one dimension.

    Rows that cannot be placed get a NULL bucket instead of being filtered out, so a single
    grouped query yields both the buckets and the count of what it had to leave out — and the
    response can say how much of the data it is *not* describing.
    """
    usable = _usable_condition()
    if dimension is EnergyDimension.temperature:
        return _DimensionSpec(
            unit="°C",
            bucket_index=sa.case(
                (
                    usable,
                    sa.func.width_bucket(
                        Telemetry.outside_temperature_c,
                        _TEMPERATURE_MIN_C,
                        _TEMPERATURE_MAX_C,
                        _TEMPERATURE_BUCKETS,
                    ),
                ),
                else_=sa.null(),
            ),
            label=_linear_label(
                minimum=_TEMPERATURE_MIN_C,
                maximum=_TEMPERATURE_MAX_C,
                buckets=_TEMPERATURE_BUCKETS,
                unit="°C",
            ),
            method_de=(
                f"Außentemperatur in {_TEMPERATURE_BAND_C:g}-°C-Klassen von "
                f"{_TEMPERATURE_MIN_C:g} °C bis {_TEMPERATURE_MAX_C:g} °C; "
                "nur fahrende Fahrzeuge mit Geschwindigkeit über 0 km/h."
            ),
            method_en=(
                f"Outside temperature in {_TEMPERATURE_BAND_C:g} °C bands "
                f"from {_TEMPERATURE_MIN_C:g} °C to {_TEMPERATURE_MAX_C:g} °C; only moving "
                "vehicles are counted."
            ),
        )
    if dimension is EnergyDimension.speed:
        return _DimensionSpec(
            unit="km/h",
            bucket_index=sa.case(
                (
                    usable,
                    sa.func.width_bucket(
                        Telemetry.speed_kmh,
                        _SPEED_MIN_KMH,
                        _SPEED_MAX_KMH,
                        _SPEED_BUCKETS,
                    ),
                ),
                else_=sa.null(),
            ),
            label=_linear_label(
                minimum=_SPEED_MIN_KMH,
                maximum=_SPEED_MAX_KMH,
                buckets=_SPEED_BUCKETS,
                unit="km/h",
            ),
            method_de=(
                f"Geschwindigkeit in {_SPEED_BAND_KMH:g}-km/h-Klassen; nur fahrende Fahrzeuge."
            ),
            method_en=(
                f"Speed in {_SPEED_BAND_KMH:g} km/h bands; only moving vehicles are counted."
            ),
        )

    # Derived dimension: the telemetry table carries no traffic severity, so congestion is read
    # off the speed the vehicle actually achieved against the speed the road would allow — the
    # posted limit where the row carries one, the road-class reference otherwise.
    reference_speed = sa.func.coalesce(
        sa.func.nullif(Telemetry.speed_limit_kmh, 0.0),
        sa.case(
            *(
                (Telemetry.road_class == road_class, speed)
                for road_class, speed in _ROAD_CLASS_REFERENCE_KMH.items()
            ),
            else_=sa.null(),
        ),
    )
    ratio = Telemetry.speed_kmh / reference_speed
    return _DimensionSpec(
        unit="severity",
        bucket_index=sa.case(
            (
                sa.and_(usable, reference_speed.is_not(None)),
                sa.case(
                    (ratio >= _CONGESTION_FREE_FLOW, 1),
                    (ratio >= _CONGESTION_MODERATE, 2),
                    (ratio >= _CONGESTION_HIGH, 3),
                    else_=4,
                ),
            ),
            else_=sa.null(),
        ),
        label=_congestion_label,
        method_de=(
            "Verkehrslage abgeleitet aus dem Verhältnis von gefahrener Geschwindigkeit zur "
            "Referenzgeschwindigkeit (Tempolimit der Telemetrie, sonst Richtwert je Straßenklasse: "
            f"{_REFERENCE_SPEED_SUMMARY}): >= {_CONGESTION_FREE_FLOW:.0%} = low, "
            f">= {_CONGESTION_MODERATE:.0%} = moderate, >= {_CONGESTION_HIGH:.0%} = high, "
            "darunter severe. Abschnitte ohne bekannte Straßenklasse bleiben unberücksichtigt."
        ),
        method_en=(
            "Congestion derived from achieved speed as a fraction of the reference speed (the "
            "posted limit when the telemetry carries one, otherwise the road-class reference: "
            f"{_REFERENCE_SPEED_SUMMARY}): >= {_CONGESTION_FREE_FLOW:.0%} = low, "
            f">= {_CONGESTION_MODERATE:.0%} = moderate, >= {_CONGESTION_HIGH:.0%} = high, "
            "below that severe. Stretches of unknown road class are excluded."
        ),
    )


@router.get(
    "/energy",
    response_model=EnergyAnalysis,
    summary="Energy consumption by temperature, speed or traffic",
    description=(
        "Consumption in kWh/100 km bucketed against one explanatory dimension, with the sample "
        "count of every bucket so that a curve built from eleven rows cannot be mistaken for "
        "one built from eleven thousand.\n\n"
        "**The source is the simulated telemetry table** (BUILD_SPEC §3.2): every row was "
        "produced by AutoTwin's vehicle simulation, never measured on a real car. The payload "
        "repeats that in `data_origin`, `is_simulated` and a bilingual disclaimer, and a chart "
        "built from it must show the label.\n\n"
        "`traffic` is a **derived** dimension — the telemetry table carries no traffic column, "
        "so congestion is read off achieved speed against the posted limit. `method_en` states "
        "the thresholds."
    ),
)
async def energy(
    session: DbSession,
    dimension: Annotated[
        EnergyDimension,
        Query(description="What to bucket consumption against."),
    ] = EnergyDimension.temperature,
    since: Annotated[
        datetime | None,
        Query(description="Only telemetry recorded at or after this instant (ISO 8601, UTC)."),
    ] = None,
) -> EnergyAnalysis:
    """Bucket simulated telemetry and report the distribution of consumption in each bucket."""
    spec = _dimension_spec(dimension)
    consumption = Telemetry.energy_consumption_kwh_100km
    bucket_index = spec.bucket_index.label("bucket_index")

    statement = sa.select(
        bucket_index,
        sa.func.count().label("sample_count"),
        sa.func.avg(consumption).label("mean_kwh"),
        sa.func.percentile_cont(0.5).within_group(consumption).label("median_kwh"),
        sa.func.percentile_cont(0.1).within_group(consumption).label("p10_kwh"),
        sa.func.percentile_cont(0.9).within_group(consumption).label("p90_kwh"),
        sa.func.avg(Telemetry.speed_kmh).label("mean_speed_kmh"),
        sa.func.avg(Telemetry.outside_temperature_c).label("mean_temperature_c"),
    )
    if since is not None:
        statement = statement.where(Telemetry.recorded_at >= since)
    statement = statement.group_by(sa.text("bucket_index")).order_by(sa.text("bucket_index"))

    rows = (await session.execute(statement)).all()

    buckets: list[EnergyBucket] = []
    included = 0
    excluded = 0
    weighted_sum = 0.0
    for row in rows:
        count = int(row.sample_count)
        if row.bucket_index is None:
            excluded += count
            continue
        included += count
        mean = float(row.mean_kwh)
        weighted_sum += mean * count
        label, lower, upper = spec.label(int(row.bucket_index))
        buckets.append(
            EnergyBucket(
                bucket=label,
                lower_bound=lower,
                upper_bound=upper,
                sample_count=count,
                mean_kwh_per_100km=round(mean, 2),
                median_kwh_per_100km=round(float(row.median_kwh), 2),
                p10_kwh_per_100km=round(float(row.p10_kwh), 2),
                p90_kwh_per_100km=round(float(row.p90_kwh), 2),
                mean_speed_kmh=round(float(row.mean_speed_kmh), 1),
                mean_temperature_c=round(float(row.mean_temperature_c), 1),
            )
        )

    return EnergyAnalysis(
        dimension=dimension,
        dimension_unit=spec.unit,
        buckets=buckets,
        sample_count=included,
        excluded_samples=excluded,
        overall_mean_kwh_per_100km=round(weighted_sum / included, 2) if included else None,
        data_origin=DataOrigin.simulated,
        is_simulated=True,
        disclaimer_de=_DISCLAIMER_DE,
        disclaimer_en=_DISCLAIMER_EN,
        method_de=spec.method_de,
        method_en=spec.method_en,
        generated_at=utc_now(),
    )


# ---------------------------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------------------------


async def _connectors_by_bundesland(session: AsyncSession) -> dict[Bundesland, tuple[int, int]]:
    """Connector totals per state as ``(all connectors, connectors >= 50 kW)``.

    Counted on ``charging_points`` rather than on ``charging_stations.charging_points_count``,
    because a 300 kW site with one CCS plug and three 22 kW plugs has four connectors and one
    fast one, and the station-level count cannot tell the two apart.
    """
    statement = (
        sa.select(
            ChargingStation.bundesland.label("bundesland"),
            sa.func.count().label("points"),
            sa.func.count()
            .filter(ChargingPoint.power_kw >= FAST_CHARGER_THRESHOLD_KW)
            .label("fast_points"),
        )
        .select_from(ChargingPoint)
        .join(ChargingStation, ChargingStation.id == ChargingPoint.station_id)
        .where(ChargingStation.bundesland.is_not(None))
        .group_by(ChargingStation.bundesland)
    )
    return {
        Bundesland(row.bundesland): (int(row.points), int(row.fast_points))
        for row in (await session.execute(statement)).all()
    }


@router.get(
    "/regions",
    response_model=RegionComparison,
    summary="Charging infrastructure by federal state",
    description=(
        "All sixteen federal states side by side: sites, connectors, installed power and the "
        "densities that make them comparable — sites per 1 000 km² is what stops the ranking "
        "from being a list of which states are large.\n\n"
        "A state with no charging site appears with zeros rather than disappearing. Sites whose "
        "federal state the register's spelling could not be resolved to are counted in "
        "`stations_without_bundesland` instead of being shown as a seventeenth region."
    ),
)
async def regions(session: DbSession) -> RegionComparison:
    """Rank the federal states by charging-site density."""
    mode = await register_data_mode(session)
    if mode is not None:
        set_data_mode(mode)

    power = sa.func.coalesce(ChargingStation.total_power_kw, ChargingStation.max_power_kw)
    statement = (
        sa.select(
            ChargingStation.bundesland.label("bundesland"),
            sa.func.count().label("stations"),
            sa.func.count().filter(ChargingStation.is_fast_charger.is_(True)).label("fast"),
            sa.func.count()
            .filter(ChargingStation.charging_category == ChargingCategory.ultra_fast)
            .label("ultra_fast"),
            sa.func.count(sa.distinct(ChargingStation.operator)).label("operators"),
            sa.func.coalesce(sa.func.sum(power), 0.0).label("installed_power_kw"),
            sa.func.max(ChargingStation.max_power_kw).label("max_power_kw"),
            sa.func.percentile_cont(0.5)
            .within_group(ChargingStation.max_power_kw)
            .label("median_power_kw"),
        )
        .where(ChargingStation.bundesland.is_not(None))
        .group_by(ChargingStation.bundesland)
    )
    rows = {Bundesland(row.bundesland): row for row in (await session.execute(statement)).all()}
    connectors = await _connectors_by_bundesland(session)

    totals = (
        await session.execute(
            sa.select(
                sa.func.count().label("stations_total"),
                sa.func.count(ChargingStation.bundesland).label("resolved"),
            )
        )
    ).one()
    resolved = int(totals.resolved)

    stats: list[RegionStat] = []
    for state in Bundesland:
        area_km2 = _BUNDESLAND_AREA_KM2[state]
        row = rows.get(state)
        points, fast_points = connectors.get(state, (0, 0))
        stations = int(row.stations) if row is not None else 0
        fast_stations = int(row.fast) if row is not None else 0
        per_area = _PER_AREA_UNIT_KM2 / area_km2
        stats.append(
            RegionStat(
                bundesland=state,
                label_de=state.label_de,
                area_km2=area_km2,
                stations=stations,
                fast_stations=fast_stations,
                ultra_fast_stations=int(row.ultra_fast) if row is not None else 0,
                charging_points=points,
                fast_charging_points=fast_points,
                installed_power_kw=round(float(row.installed_power_kw), 1) if row else 0.0,
                operators=int(row.operators) if row is not None else 0,
                max_power_kw=float(row.max_power_kw) if row is not None else None,
                median_station_power_kw=(
                    round(float(row.median_power_kw), 1)
                    if row is not None and row.median_power_kw is not None
                    else None
                ),
                stations_per_1000_km2=round(stations * per_area, 2),
                fast_stations_per_1000_km2=round(fast_stations * per_area, 2),
                charging_points_per_1000_km2=round(points * per_area, 2),
                share_of_resolved_stations_percent=(
                    round(100.0 * stations / resolved, 2) if resolved else 0.0
                ),
            )
        )

    stats.sort(key=lambda stat: stat.stations_per_1000_km2, reverse=True)
    return RegionComparison(
        regions=stats,
        stations_total=int(totals.stations_total),
        stations_with_resolved_bundesland=resolved,
        stations_without_bundesland=int(totals.stations_total) - resolved,
        data_origin=DataOrigin.official,
        area_source=_AREA_SOURCE,
        generated_at=utc_now(),
    )
