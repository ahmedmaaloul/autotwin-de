"""Deterministic energy-explanation generator (BUILD_SPEC §11).

Answers the one question a route-analysis page exists to answer: *why* does this trip cost
more (or less) than the vehicle's nominal consumption, and which driver is responsible for how
much of the difference.

**No LLM is involved anywhere in this module, and none ever will be.** The numbers are produced
by re-running the physical model of :mod:`autotwin_ml.baseline` under altered conditions and
reading the differences. A language model asked the same question would produce fluent prose
that is unfalsifiable and different on every call; this produces a number a reader can check by
re-running the model themselves, identical on every call.

The method
----------

A **sequential counterfactual ladder**. Start from what the vehicle's datasheet promises for
this distance, then switch on one group of real conditions at a time and record what each
switch costs::

    E_nominal    nominal consumption · distance                  the datasheet promise
      + speed_profile    → drive this route's actual speeds, flat, 20 °C, free-flowing, no aux
      + auxiliary        → switch on the 0.35 kW base load
      + gradient         → put the real elevation profile back
      + traffic          → slow down to the traffic-delayed speeds
      + temperature      → apply the real air and pack temperatures (HVAC, density, cold pack)
      + wind             → apply the real headwind component
      + model_correction → whatever separates the caller's energies from the physical model
    = E_actual

Every rung is one full re-run of the physical model over every segment, and every delta is the
difference between two adjacent rungs, so **the drivers sum exactly to the total difference**.
That is the property a naive "change one factor at a time from the actual conditions" scheme
does *not* have: single-factor counterfactuals drop all interaction terms, their deltas do not
add up, and the leftover silently becomes a rounding error nobody notices. A ladder assigns
every interaction term to the rung that came later, which is a choice — a different order
produces different splits, exactly as in a Shapley decomposition evaluated along one
permutation — and the order used here is fixed, documented and justified below.

**Order rationale.** Structural properties of the route come first (how fast, how hilly),
because they hold on every day of the year. Then the day-specific conditions (traffic, then
weather), because those are what a driver could have chosen differently by leaving at another
time. Putting temperature last means the winter penalty is charged against the already
slower, hillier trip, which is the conservative direction: it reports the *marginal* cost of
the cold on this trip rather than its cost on an idealised one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from autotwin_contracts.enums import RoadClass, TrafficSeverity, WeatherCondition
from autotwin_contracts.records import TrafficEventRecord, WeatherRecord
from autotwin_contracts.vehicles import VehicleProfile
from autotwin_core.errors import ValidationError
from autotwin_ml.baseline import PhysicalEnergyModel
from autotwin_ml.types import EnergyResult, SegmentConditions

__all__ = [
    "REFERENCE_TEMPERATURE_C",
    "DriverDirection",
    "EnergyDriver",
    "EnergyExplanation",
    "TrafficContext",
    "WeatherContext",
    "explain_route_energy",
]

REFERENCE_TEMPERATURE_C: Final[float] = 20.0
"""The neutral temperature of the ladder: HVAC load is zero and the cold-battery factor is 1.0.

Chosen because it is the point where the model's own temperature terms vanish, not because it
is a German annual mean (which is closer to 10 °C). A reference where the terms are exactly
zero makes the temperature driver read as "the cost of not being at the model's neutral
point", which is a statement about the model rather than about the climate."""

_MIN_DELTA_PERCENT: Final[float] = 0.5
"""Drivers smaller than half a percentage point are noise and are not shown."""

_NEUTRAL_TOTAL_PERCENT: Final[float] = 3.0
"""Below this the headline says "as expected" instead of naming a direction."""


class DriverDirection(StrEnum):
    """Which way a driver pushed the consumption — the sign, as a rendering hint.

    A string enum rather than the raw sign so the frontend can switch on it for the arrow icon
    and the colour token without re-deriving the comparison, and so ``neutral`` is a first-class
    answer instead of "a delta that happens to be near zero".
    """

    increase = "increase"
    """Raised consumption above the nominal reference."""

    decrease = "decrease"
    """Lowered consumption below the nominal reference."""

    neutral = "neutral"
    """Moved consumption by less than the reporting threshold."""


@dataclass(frozen=True, slots=True)
class EnergyDriver:
    """One entry of ``RouteAnalysis.explanation.drivers`` (BUILD_SPEC §7.3).

    ``factor``, ``delta_percent``, ``direction``, ``label_de`` and ``label_en`` are the five
    fields the API contract names. :attr:`delta_kwh`, :attr:`detail_de` and :attr:`detail_en`
    are additions: the kWh because a percentage without its base is unauditable, the details
    because a driver that says "temperature: +18 %" without saying *which* temperature invites
    exactly the mistrust a deterministic generator exists to avoid.
    """

    factor: str
    """Stable snake_case id — the key the frontend maps to an icon and a translation."""

    delta_percent: float
    """Contribution in percentage points of the nominal energy. Signed."""

    direction: DriverDirection
    """Sign of :attr:`delta_percent`, as a rendering hint."""

    label_de: str
    """Short German label, e.g. ``"Temperatur & Klimatisierung"``."""

    label_en: str
    """Short English label."""

    delta_kwh: float
    """Contribution in kWh over the whole route. Signed."""

    detail_de: str
    """One German clause naming the numbers behind the delta."""

    detail_en: str
    """The same clause in English."""


@dataclass(frozen=True, slots=True)
class WeatherContext:
    """Route-level weather summary used in the explanation text.

    Separate from the per-segment :class:`~autotwin_ml.types.SegmentConditions` because the
    explanation talks about the *trip* ("average 2 °C, rain on 40 km"), and reconstructing that
    from segments would silently weight a 1 km urban segment like a 12 km motorway one.
    """

    mean_temperature_c: float | None = None
    """Distance-weighted mean air temperature along the route, in °C."""

    min_temperature_c: float | None = None
    """Coldest observation along the route, in °C — what range planning has to survive."""

    precipitation_mm: float = 0.0
    """Total precipitation observed along the route, in mm."""

    mean_wind_speed_ms: float = 0.0
    """Mean scalar wind speed along the route, in m/s."""

    condition: WeatherCondition = WeatherCondition.unknown
    """Coarse condition of the corridor as a whole."""

    @classmethod
    def from_records(cls, records: Sequence[WeatherRecord]) -> WeatherContext:
        """Summarise the DWD observations attached to a route analysis.

        Missing values are skipped rather than treated as zero: a station that reports no
        precipitation is not the same as a station reporting 0.0 mm, and averaging the
        difference away is how a sensor outage turns into a confident wrong statement.
        """
        temperatures = [r.temperature_c for r in records if r.temperature_c is not None]
        precipitation = [r.precipitation_mm for r in records if r.precipitation_mm is not None]
        winds = [r.wind_speed_ms for r in records if r.wind_speed_ms is not None]
        conditions = [r.condition for r in records if r.condition is not WeatherCondition.unknown]
        return cls(
            mean_temperature_c=(sum(temperatures) / len(temperatures)) if temperatures else None,
            min_temperature_c=min(temperatures) if temperatures else None,
            precipitation_mm=sum(precipitation),
            mean_wind_speed_ms=(sum(winds) / len(winds)) if winds else 0.0,
            condition=_dominant_condition(conditions),
        )


@dataclass(frozen=True, slots=True)
class TrafficContext:
    """Route-level traffic summary used in the explanation text."""

    event_count: int = 0
    """Number of traffic events matched to the corridor."""

    worst_severity: TrafficSeverity | None = None
    """Highest severity among them, or ``None`` when the corridor is clear."""

    blocked_count: int = 0
    """How many of the events are full closures (Vollsperrungen)."""

    @classmethod
    def from_records(cls, records: Sequence[TrafficEventRecord]) -> TrafficContext:
        """Summarise the Autobahn GmbH events attached to a route analysis."""
        if not records:
            return cls()
        worst = max(records, key=lambda event: event.severity.ordinal).severity
        return cls(
            event_count=len(records),
            worst_severity=worst,
            blocked_count=sum(1 for event in records if event.is_blocked),
        )


@dataclass(frozen=True, slots=True)
class EnergyExplanation:
    """The ``explanation`` object of ``RouteAnalysis`` (BUILD_SPEC §7.3)."""

    headline: str
    """One sentence in the default locale (German, per BUILD_SPEC §12)."""

    headline_de: str
    """German headline."""

    headline_en: str
    """English headline."""

    drivers: tuple[EnergyDriver, ...]
    """Drivers ranked by absolute contribution, largest first."""

    nominal_kwh_100km: float
    """The vehicle's datasheet consumption — the reference the drivers are measured against."""

    actual_kwh_100km: float
    """Consumption actually predicted for this route."""

    total_delta_percent: float
    """``actual / nominal - 1``, in percent. The drivers sum to this."""

    distance_km: float
    """Route distance the explanation covers, in kilometres."""

    method: str
    """One-line statement of how the numbers were produced, for the UI's methodology note."""


_METHOD_DESCRIPTION: Final[str] = (
    "Sequential counterfactual ladder over the physical road-load model: each driver is the "
    "difference between two consecutive re-runs of the model, so the drivers sum exactly to "
    "the total deviation from nominal consumption. No language model is involved."
)

_LABELS: Final[dict[str, tuple[str, str]]] = {
    "speed_profile": ("Geschwindigkeits- und Streckenprofil", "Speed and road profile"),
    "auxiliary": ("Nebenverbraucher (Grundlast)", "Auxiliary base load"),
    "gradient": ("Steigungsprofil", "Elevation profile"),
    "traffic": ("Verkehrslage", "Traffic conditions"),
    "temperature": ("Temperatur und Klimatisierung", "Temperature and climate control"),
    "wind": ("Gegenwind", "Headwind"),
    "model_correction": ("Modellkorrektur (ML)", "Model correction (ML)"),
}
"""Bilingual labels per factor id. One table so a wording change touches one line."""


def explain_route_energy(
    profile: VehicleProfile,
    segments: Sequence[SegmentConditions],
    energy_results: Sequence[EnergyResult],
    baseline_nominal: float | None = None,
    weather: WeatherContext | None = None,
    traffic: TrafficContext | None = None,
    *,
    model: PhysicalEnergyModel | None = None,
    min_delta_percent: float = _MIN_DELTA_PERCENT,
) -> EnergyExplanation:
    """Explain a route's energy against the vehicle's nominal consumption.

    Args:
        profile: The vehicle. Supplies the mass, drag area and — unless ``baseline_nominal``
            overrides it — the nominal consumption the drivers are measured against.
        segments: The conditions the route was analysed under, in route order.
        energy_results: The energy per segment, index-aligned with ``segments``. These may come
            from the physical model or from the ML regressor; whatever separates them from the
            physical model surfaces as the ``model_correction`` driver instead of being hidden.
        baseline_nominal: Reference consumption in kWh/100 km. Defaults to the profile's
            ``nominal_consumption_kwh_100km``.
        weather: Optional corridor weather summary, used only to enrich the detail text.
        traffic: Optional corridor traffic summary, used only to enrich the detail text.
        model: Physical model to run the counterfactuals with. A fresh default instance is
            used when omitted; pass one to explain a route under tuned constants.
        min_delta_percent: Drivers below this absolute contribution are dropped as noise. The
            single largest driver is always kept, so the list is never empty.

    Raises:
        ValidationError: If the two sequences have different lengths (an index-alignment bug
            that would otherwise produce a confidently wrong explanation) or if the route has
            no distance to normalise against.
    """
    if len(segments) != len(energy_results):
        msg = (
            f"segments and energy_results must be index-aligned; "
            f"got {len(segments)} and {len(energy_results)}"
        )
        raise ValidationError(msg, details={"segments": len(segments)})

    distance_km = sum(segment.distance_km for segment in segments)
    if distance_km <= 0.0:
        msg = "cannot explain the energy of a route with no distance"
        raise ValidationError(msg, details={"field": "distance_km"})

    nominal_kwh_100km = (
        baseline_nominal if baseline_nominal is not None else profile.nominal_consumption_kwh_100km
    )
    if nominal_kwh_100km <= 0.0:
        msg = f"baseline_nominal must be positive, got {nominal_kwh_100km!r}"
        raise ValidationError(msg, details={"field": "baseline_nominal"})

    engine = model if model is not None else PhysicalEnergyModel()
    actual = EnergyResult.sum(energy_results)
    nominal_kwh = nominal_kwh_100km * distance_km / 100.0

    rungs = _ladder_energies(engine, profile, segments)
    reference = rungs["reference"]

    # Rung 1 and 2 split the reference run: the traction cost of driving this route at all,
    # then the standing auxiliary load. The split is read straight off the decomposition,
    # because the auxiliary buckets are strictly additive and independent of the road load.
    reference_standing_kwh = reference.auxiliary_kwh + reference.hvac_kwh
    reference_traction_kwh = reference.kwh - reference_standing_kwh

    deltas: list[tuple[str, float]] = [
        ("speed_profile", reference_traction_kwh - nominal_kwh),
        ("auxiliary", reference_standing_kwh),
        ("gradient", rungs["gradient"].kwh - reference.kwh),
        ("traffic", rungs["traffic"].kwh - rungs["gradient"].kwh),
        ("temperature", rungs["temperature"].kwh - rungs["traffic"].kwh),
        ("wind", rungs["wind"].kwh - rungs["temperature"].kwh),
        ("model_correction", actual.kwh - rungs["wind"].kwh),
    ]

    drivers = tuple(
        _build_driver(
            factor=factor,
            delta_kwh=delta_kwh,
            nominal_kwh=nominal_kwh,
            profile=profile,
            segments=segments,
            actual=actual,
            reference=reference,
            rungs=rungs,
            weather=weather,
            traffic=traffic,
        )
        for factor, delta_kwh in deltas
    )
    drivers = _rank_and_filter(drivers, min_delta_percent=min_delta_percent)

    actual_kwh_100km = actual.kwh / distance_km * 100.0
    total_delta_percent = (actual.kwh - nominal_kwh) / nominal_kwh * 100.0
    headline_de, headline_en = _headlines(
        actual_kwh_100km=actual_kwh_100km,
        nominal_kwh_100km=nominal_kwh_100km,
        total_delta_percent=total_delta_percent,
        drivers=drivers,
    )
    return EnergyExplanation(
        headline=headline_de,
        headline_de=headline_de,
        headline_en=headline_en,
        drivers=drivers,
        nominal_kwh_100km=nominal_kwh_100km,
        actual_kwh_100km=actual_kwh_100km,
        total_delta_percent=total_delta_percent,
        distance_km=distance_km,
        method=_METHOD_DESCRIPTION,
    )


# --------------------------------------------------------------------------------------
# Ladder construction
# --------------------------------------------------------------------------------------


def _ladder_energies(
    engine: PhysicalEnergyModel,
    profile: VehicleProfile,
    segments: Sequence[SegmentConditions],
) -> dict[str, EnergyResult]:
    """Run the physical model once per rung of the counterfactual ladder.

    Each rung restores exactly one group of real conditions on top of the rung before it, so
    the last rung's condition set is identical to the caller's segments field for field. That
    identity is what makes the ``model_correction`` residual meaningful: anything left over is
    a genuine difference between the caller's energies and the physical model, not an artefact
    of a condition this function forgot to restore.
    """
    reference = [
        segment.at_reference_conditions(reference_temperature_c=REFERENCE_TEMPERATURE_C)
        for segment in segments
    ]
    gradient = [
        replace(rung, gradient_percent=segment.gradient_percent)
        for rung, segment in zip(reference, segments, strict=True)
    ]
    traffic = [
        # ``duration_s`` is restored HERE, with speed, and not on a later rung. The reference
        # rung nulls it so the free-flow rung derives its own duration from distance/speed; an
        # explicit duration supplied by the caller — which is exactly what a telemetry window
        # is — describes the same physical fact as the real speed. Restoring it any later would
        # credit the whole duration correction to whichever rung happened to carry it, and a
        # 600 s window over a 180 s free-flow segment would surface to the driver as a
        # +200 % "Gegenwind" driver.
        replace(
            rung,
            speed_kmh=segment.speed_kmh,
            traffic_severity=segment.traffic_severity,
            duration_s=segment.duration_s,
        )
        for rung, segment in zip(gradient, segments, strict=True)
    ]
    temperature = [
        replace(
            rung,
            outside_temperature_c=segment.outside_temperature_c,
            battery_temperature_c=segment.battery_temperature_c,
        )
        for rung, segment in zip(traffic, segments, strict=True)
    ]
    wind = [
        replace(rung, headwind_ms=segment.headwind_ms)
        for rung, segment in zip(temperature, segments, strict=True)
    ]
    return {
        "reference": engine.trip_energy(profile, reference),
        "gradient": engine.trip_energy(profile, gradient),
        "traffic": engine.trip_energy(profile, traffic),
        "temperature": engine.trip_energy(profile, temperature),
        "wind": engine.trip_energy(profile, wind),
    }


def _rank_and_filter(
    drivers: Sequence[EnergyDriver],
    *,
    min_delta_percent: float,
) -> tuple[EnergyDriver, ...]:
    """Sort by absolute contribution and drop the noise, keeping at least one driver.

    The sort key ends in the factor id, so two drivers of identical magnitude always come back
    in the same order — the determinism requirement applies to the explanation as much as to
    the charging plan.
    """
    ranked = sorted(
        drivers,
        key=lambda driver: (-round(abs(driver.delta_percent), 6), driver.factor),
    )
    kept = tuple(driver for driver in ranked if abs(driver.delta_percent) >= min_delta_percent)
    if kept:
        return kept
    return tuple(ranked[:1])


def _build_driver(
    *,
    factor: str,
    delta_kwh: float,
    nominal_kwh: float,
    profile: VehicleProfile,
    segments: Sequence[SegmentConditions],
    actual: EnergyResult,
    reference: EnergyResult,
    rungs: dict[str, EnergyResult],
    weather: WeatherContext | None,
    traffic: TrafficContext | None,
) -> EnergyDriver:
    """Assemble one driver, including the bilingual detail clause behind its number."""
    delta_percent = delta_kwh / nominal_kwh * 100.0
    label_de, label_en = _LABELS[factor]
    detail_de, detail_en = _detail_text(
        factor=factor,
        profile=profile,
        segments=segments,
        actual=actual,
        reference=reference,
        rungs=rungs,
        weather=weather,
        traffic=traffic,
    )
    return EnergyDriver(
        factor=factor,
        delta_percent=delta_percent,
        direction=_direction(delta_percent),
        label_de=label_de,
        label_en=label_en,
        delta_kwh=delta_kwh,
        detail_de=detail_de,
        detail_en=detail_en,
    )


def _direction(delta_percent: float) -> DriverDirection:
    """Map a signed contribution onto the rendering hint."""
    if delta_percent > _MIN_DELTA_PERCENT:
        return DriverDirection.increase
    if delta_percent < -_MIN_DELTA_PERCENT:
        return DriverDirection.decrease
    return DriverDirection.neutral


# --------------------------------------------------------------------------------------
# Detail text — every clause is generated from a number that was actually computed
# --------------------------------------------------------------------------------------


def _detail_text(
    *,
    factor: str,
    profile: VehicleProfile,
    segments: Sequence[SegmentConditions],
    actual: EnergyResult,
    reference: EnergyResult,
    rungs: dict[str, EnergyResult],
    weather: WeatherContext | None,
    traffic: TrafficContext | None,
) -> tuple[str, str]:
    """Produce the German and English detail clause for one factor."""
    if factor == "speed_profile":
        mean_speed = _distance_weighted_speed_kmh(segments)
        motorway_share = _motorway_share_percent(segments)
        return (
            f"Ø {_de(mean_speed, 0)} km/h, {_de(motorway_share, 0)} % Autobahnanteil; "
            f"Normverbrauch {_de(profile.nominal_consumption_kwh_100km)} kWh/100 km",
            f"avg {_en(mean_speed, 0)} km/h, {_en(motorway_share, 0)} % motorway; "
            f"nominal {_en(profile.nominal_consumption_kwh_100km)} kWh/100 km",
        )

    if factor == "auxiliary":
        hours = reference.duration_s / 3600.0
        return (
            f"{_de(reference.auxiliary_kwh)} kWh Grundlast über {_de(hours)} h Fahrzeit",
            f"{_en(reference.auxiliary_kwh)} kWh base load over {_en(hours)} h of driving",
        )

    if factor == "gradient":
        climb_m, descent_m = _elevation_change_m(segments)
        regen_kwh = abs(rungs["gradient"].regen_kwh)
        return (
            f"{_de(climb_m, 0)} m Anstieg, {_de(descent_m, 0)} m Gefälle; "
            f"{_de(regen_kwh)} kWh rekuperiert",
            f"{_en(climb_m, 0)} m of climb, {_en(descent_m, 0)} m of descent; "
            f"{_en(regen_kwh)} kWh recuperated",
        )

    if factor == "traffic":
        extra_min = (rungs["traffic"].duration_s - rungs["gradient"].duration_s) / 60.0
        affected_km = sum(
            segment.distance_km
            for segment in segments
            if segment.traffic_severity is not TrafficSeverity.low
        )
        events = traffic.event_count if traffic is not None else 0
        worst = traffic.worst_severity.value if traffic and traffic.worst_severity else "—"
        return (
            f"{_de(affected_km, 0)} km betroffen, {events} Meldung(en), Stufe {worst}; "
            f"{_de(extra_min, 0)} min Mehrzeit",
            f"{_en(affected_km, 0)} km affected, {events} report(s), severity {worst}; "
            f"{_en(extra_min, 0)} min of extra time",
        )

    if factor == "temperature":
        mean_temp = _mean_temperature_c(segments, weather)
        hvac_kwh = rungs["temperature"].hvac_kwh
        cold_factor = _cold_factor_at(segments)
        return (
            f"Ø {_de(mean_temp, 0)} °C: {_de(hvac_kwh)} kWh Heizung/Klima, "
            f"Batteriekälte-Faktor {_de(cold_factor, 2)}",
            f"avg {_en(mean_temp, 0)} °C: {_en(hvac_kwh)} kWh climate control, "
            f"cold-battery factor {_en(cold_factor, 2)}",
        )

    if factor == "wind":
        mean_headwind = _distance_weighted_headwind_ms(segments)
        scalar = _mean_wind_speed_ms(segments, weather)
        return (
            f"Ø {_de(mean_headwind)} m/s Gegenwindkomponente "
            f"(gemessene Windgeschwindigkeit Ø {_de(scalar)} m/s)",
            f"avg {_en(mean_headwind)} m/s headwind component "
            f"(measured wind speed avg {_en(scalar)} m/s)",
        )

    # model_correction
    delta = actual.kwh - rungs["wind"].kwh
    return (
        f"{_de(delta)} kWh Unterschied zwischen Modellprognose und physikalischer Referenz",
        f"{_en(delta)} kWh between the served prediction and the physical reference",
    )


def _headlines(
    *,
    actual_kwh_100km: float,
    nominal_kwh_100km: float,
    total_delta_percent: float,
    drivers: Sequence[EnergyDriver],
) -> tuple[str, str]:
    """Compose the German and English headline from the top drivers.

    Deliberately dull and numeric. The headline's job is to be the true summary of the list
    below it, not to be interesting.
    """
    consumption_de = f"{_de(actual_kwh_100km)} kWh/100 km"
    consumption_en = f"{_en(actual_kwh_100km)} kWh/100 km"
    if abs(total_delta_percent) < _NEUTRAL_TOTAL_PERCENT:
        return (
            f"{consumption_de} — praktisch auf Normniveau ({_de(nominal_kwh_100km)} kWh/100 km).",
            f"{consumption_en} — essentially at the nominal figure "
            f"({_en(nominal_kwh_100km)} kWh/100 km).",
        )

    direction_de = "über" if total_delta_percent > 0.0 else "unter"
    direction_en = "above" if total_delta_percent > 0.0 else "below"
    top = [driver for driver in drivers if driver.direction is not DriverDirection.neutral][:2]
    if top:
        reasons_de = " und ".join(
            f"{driver.label_de} ({_signed_de(driver.delta_percent)} %)" for driver in top
        )
        reasons_en = " and ".join(
            f"{driver.label_en} ({_signed_en(driver.delta_percent)} %)" for driver in top
        )
        return (
            f"{consumption_de} — {_de(abs(total_delta_percent), 0)} % {direction_de} "
            f"Normverbrauch, vor allem {reasons_de}.",
            f"{consumption_en} — {_en(abs(total_delta_percent), 0)} % {direction_en} "
            f"nominal, mainly {reasons_en}.",
        )
    return (
        f"{consumption_de} — {_de(abs(total_delta_percent), 0)} % {direction_de} Normverbrauch.",
        f"{consumption_en} — {_en(abs(total_delta_percent), 0)} % {direction_en} nominal.",
    )


# --------------------------------------------------------------------------------------
# Small numeric helpers
# --------------------------------------------------------------------------------------


def _de(value: float, digits: int = 1) -> str:
    """German number formatting — comma decimal separator."""
    return f"{value:.{digits}f}".replace(".", ",")


def _en(value: float, digits: int = 1) -> str:
    """English number formatting — point decimal separator."""
    return f"{value:.{digits}f}"


def _signed_de(value: float, digits: int = 0) -> str:
    """German formatting with an explicit sign, for driver contributions."""
    return f"{value:+.{digits}f}".replace(".", ",")


def _signed_en(value: float, digits: int = 0) -> str:
    """English formatting with an explicit sign."""
    return f"{value:+.{digits}f}"


def _distance_weighted_speed_kmh(segments: Sequence[SegmentConditions]) -> float:
    """Mean speed weighted by distance — the speed that actually shaped the energy."""
    distance = sum(segment.distance_km for segment in segments)
    if distance <= 0.0:
        return 0.0
    return sum(segment.speed_kmh * segment.distance_km for segment in segments) / distance


def _distance_weighted_headwind_ms(segments: Sequence[SegmentConditions]) -> float:
    """Mean headwind component weighted by distance, in m/s."""
    distance = sum(segment.distance_km for segment in segments)
    if distance <= 0.0:
        return 0.0
    return sum(segment.headwind_ms * segment.distance_km for segment in segments) / distance


def _motorway_share_percent(segments: Sequence[SegmentConditions]) -> float:
    """Share of the route driven on an Autobahn, in percent."""
    distance = sum(segment.distance_km for segment in segments)
    if distance <= 0.0:
        return 0.0
    motorway = sum(
        segment.distance_km for segment in segments if segment.road_class is RoadClass.motorway
    )
    return motorway / distance * 100.0


def _elevation_change_m(segments: Sequence[SegmentConditions]) -> tuple[float, float]:
    """Total climb and total descent in metres, from the per-segment gradients."""
    climb = sum(
        segment.gradient_percent / 100.0 * segment.distance_m
        for segment in segments
        if segment.gradient_percent > 0.0
    )
    descent = sum(
        -segment.gradient_percent / 100.0 * segment.distance_m
        for segment in segments
        if segment.gradient_percent < 0.0
    )
    return (climb, descent)


def _mean_temperature_c(
    segments: Sequence[SegmentConditions],
    weather: WeatherContext | None,
) -> float:
    """Distance-weighted mean air temperature, preferring the corridor summary when given."""
    if weather is not None and weather.mean_temperature_c is not None:
        return weather.mean_temperature_c
    distance = sum(segment.distance_km for segment in segments)
    if distance <= 0.0:
        return REFERENCE_TEMPERATURE_C
    weighted = sum(segment.outside_temperature_c * segment.distance_km for segment in segments)
    return weighted / distance


def _mean_wind_speed_ms(
    segments: Sequence[SegmentConditions],
    weather: WeatherContext | None,
) -> float:
    """Mean measured (scalar) wind speed, preferring the corridor summary when given.

    Falls back to the distance-weighted per-segment value so that a caller who fills
    ``SegmentConditions.wind_speed_ms`` but passes no :class:`WeatherContext` still gets the
    measured wind quoted next to the resolved headwind component, rather than a misleading
    ``0.0 m/s``.
    """
    if weather is not None and weather.mean_wind_speed_ms > 0.0:
        return weather.mean_wind_speed_ms
    distance = sum(segment.distance_km for segment in segments)
    if distance <= 0.0:
        return 0.0
    return sum(segment.wind_speed_ms * segment.distance_km for segment in segments) / distance


def _cold_factor_at(segments: Sequence[SegmentConditions]) -> float:
    """Cold-battery internal-resistance factor at the route's mean pack temperature."""
    distance = sum(segment.distance_km for segment in segments)
    if distance <= 0.0:
        return 1.0
    weighted = sum(
        segment.effective_battery_temperature_c * segment.distance_km for segment in segments
    )
    return PhysicalEnergyModel().constants.cold_battery_factor(weighted / distance)


def _dominant_condition(conditions: Sequence[WeatherCondition]) -> WeatherCondition:
    """The most severe condition observed, not the most frequent one.

    A single snow observation on a corridor matters more to a driver than twenty clear ones,
    so the summary reports the worst rather than the mode. Ties resolve by the fixed order
    below, which keeps the function deterministic.
    """
    if not conditions:
        return WeatherCondition.unknown
    severity_order = (
        WeatherCondition.storm,
        WeatherCondition.snow,
        WeatherCondition.fog,
        WeatherCondition.rain,
        WeatherCondition.clouds,
        WeatherCondition.clear,
    )
    observed = set(conditions)
    for condition in severity_order:
        if condition in observed:
            return condition
    return WeatherCondition.unknown
