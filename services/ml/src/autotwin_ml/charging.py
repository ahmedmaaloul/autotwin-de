"""Charging-curve model and corridor charging optimiser (BUILD_SPEC §10.3, §7.4).

Two halves that belong together:

1. **The curve.** :func:`charging_power_kw` and :func:`charge_time_minutes` answer "how long
   does 10 % → 80 % take on this station with this pack at this temperature?". Charging an EV
   is not a constant-power process — the power tapers as the cells fill and collapses when they
   are cold — and a planner that ignores the taper underestimates every stop by a third.
2. **The search.** :func:`optimise_charging` picks *which* stations to stop at and *how full*
   to charge at each, minimising the objective of BUILD_SPEC §10.3::

       objective = driving_time + charging_time + 2 · detour_time + range_risk_penalty

   by beam search, beam width 8, at most 3 stops. Pure Python — no solver dependency, as the
   specification requires, and the problem is small enough (a few dozen corridor stations,
   three stops) that a well-pruned beam finds the same answer a MILP would.

**Determinism is a hard requirement**, not a nicety: the API caches plans, the frontend
snapshots them, and a plan that changes between two identical requests is a bug report. Every
ordering in this module is an explicit sort with a total tie-break key, every float that enters
a comparison is rounded first, and nothing iterates a ``set`` or a ``dict`` whose insertion
order depends on float comparisons.

See ``docs/ml/charging-optimisation.md`` for the assumptions and for what a production planner
would need that this one does not have.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from autotwin_contracts.enums import ChargingCategory
from autotwin_contracts.geo import Coordinate
from autotwin_contracts.records import ChargingStationRecord
from autotwin_contracts.vehicles import VehicleProfile
from autotwin_core.errors import ValidationError
from autotwin_ml.constants import piecewise_linear
from autotwin_ml.types import RouteEnergySegment

__all__ = [
    "CHARGING_OBJECTIVE",
    "DEFAULT_BEAM_WIDTH",
    "DEFAULT_CHARGE_TARGETS_PERCENT",
    "DEFAULT_MAX_STOPS",
    "DEFAULT_MIN_SOC_PERCENT",
    "ChargingCandidate",
    "ChargingPlan",
    "ChargingStop",
    "charge_time_minutes",
    "charging_power_kw",
    "optimise_charging",
]

# --------------------------------------------------------------------------------------
# Charging-curve assumptions
# --------------------------------------------------------------------------------------

SOC_TAPER_CURVE: Final[tuple[tuple[float, float], ...]] = (
    (0.0, 1.0),
    (50.0, 1.0),
    (80.0, 0.55),
    (100.0, 0.18),
)
"""``f(soc)`` — the fraction of peak power available at a given state of charge.

Exactly the shape BUILD_SPEC §10.3 prescribes: flat at 1.0 below 50 %, linear 1.0 → 0.55 across
50-80 %, linear 0.55 → 0.18 across 80-100 %.

**Real OEM charging curves are proprietary and none of them look like this.** A measured curve
has a ramp at the start while the pack is conditioned, plateaus and steps where the battery
management system switches current limits, and a taper whose shape depends on cell chemistry,
pack age and the preceding drive. What this piecewise-linear stand-in does capture is the one
fact that dominates trip planning: the last 20 % of the battery takes as long as the first 50,
which is why the optimiser prefers two short stops to one long one.
"""

TEMPERATURE_DERATE_CURVE: Final[tuple[tuple[float, float], ...]] = (
    (-20.0, 0.15),
    (-10.0, 0.25),
    (0.0, 0.50),
    (10.0, 1.00),
    (45.0, 1.00),
    (50.0, 0.70),
    (55.0, 0.40),
    (60.0, 0.25),
)
"""Fraction of peak power available at a given pack temperature, in °C.

Lithium-ion cells cannot accept high current when cold — plating risk — so a management system
cuts the limit hard below roughly 10 °C and lets it recover only as the pack warms, which is
why preconditioning on the way to a fast charger matters so much in a German winter. Above
about 45 °C the limit drops again to protect cell life and to stay inside the cooling system's
capacity.

Same status as :data:`SOC_TAPER_CURVE`: an order-of-magnitude envelope, clamped at both ends,
calibrated against nothing but published class-level observations.
"""

FULL_SOC_PERCENT: Final[float] = 100.0
"""A full pack. SOC in this project is always expressed against *usable* capacity."""

_MIN_CHARGING_POWER_KW: Final[float] = 1.0
"""Below this the taper has effectively stopped the session; used to bound the integration."""


def charging_power_kw(
    profile: VehicleProfile,
    soc_percent: float,
    battery_temp_c: float = 20.0,
) -> float:
    """Peak DC power in kW this vehicle can accept at ``soc_percent`` and ``battery_temp_c``.

    ``P(soc, T) = P_max · f(soc) · d(T)`` with :data:`SOC_TAPER_CURVE` and
    :data:`TEMPERATURE_DERATE_CURVE`. The station's own rating is **not** applied here — the
    caller takes ``min(station_power, this)`` — so that the vehicle-side and infrastructure-side
    limits stay separately inspectable in an explanation ("limited by the car, not the pillar").

    Returns ``0.0`` at or above 100 % SOC: the session is over.
    """
    if soc_percent >= FULL_SOC_PERCENT:
        return 0.0
    if soc_percent < 0.0:
        msg = f"soc_percent must be within 0-100, got {soc_percent!r}"
        raise ValidationError(msg, details={"field": "soc_percent"})
    taper = piecewise_linear(SOC_TAPER_CURVE, soc_percent)
    derate = piecewise_linear(TEMPERATURE_DERATE_CURVE, battery_temp_c)
    return profile.max_dc_power_kw * taper * derate


def charge_time_minutes(
    profile: VehicleProfile,
    soc_from: float,
    soc_to: float,
    station_power_kw: float,
    battery_temp_c: float = 20.0,
    *,
    soc_step_percent: float = 0.25,
) -> float:
    """Minutes to charge from ``soc_from`` to ``soc_to``, integrating the taper numerically.

    The energy for one SOC step is exact — ``usable_capacity · dsoc / 100`` — while the power
    is only constant *within* the step, so the integration is a midpoint rule over SOC::

        t = sum over steps of  E_step / min(station_power, P_vehicle(soc_mid, T))

    A 0.25 pp step gives 280 evaluations for a 10 → 80 % session, which is microseconds and
    converges to well under a second of error against a 0.01 pp step. The midpoint rather than
    the left edge matters: on the 80-100 % ramp a left-edge rule overestimates the available
    power in every step and would shave minutes off the slowest, most decision-relevant part of
    a stop.

    Returns ``0.0`` when ``soc_to <= soc_from``. Raises
    :class:`~autotwin_core.errors.ValidationError` for a non-positive station power or an SOC
    outside 0-100, both of which mean the caller has a bug rather than a hard charging problem.
    """
    if station_power_kw <= 0.0:
        msg = f"station_power_kw must be positive, got {station_power_kw!r}"
        raise ValidationError(msg, details={"field": "station_power_kw"})
    for name, value in (("soc_from", soc_from), ("soc_to", soc_to)):
        if not 0.0 <= value <= FULL_SOC_PERCENT:
            msg = f"{name} must be within 0-100, got {value!r}"
            raise ValidationError(msg, details={"field": name})
    if soc_step_percent <= 0.0:
        msg = f"soc_step_percent must be positive, got {soc_step_percent!r}"
        raise ValidationError(msg, details={"field": "soc_step_percent"})
    if soc_to <= soc_from:
        return 0.0

    capacity_kwh = profile.usable_capacity_kwh
    steps = max(1, math.ceil((soc_to - soc_from) / soc_step_percent))
    step_percent = (soc_to - soc_from) / steps
    energy_per_step_kwh = capacity_kwh * step_percent / 100.0

    minutes = 0.0
    for index in range(steps):
        soc_mid = soc_from + (index + 0.5) * step_percent
        vehicle_kw = charging_power_kw(profile, soc_mid, battery_temp_c)
        available_kw = min(station_power_kw, vehicle_kw)
        if available_kw < _MIN_CHARGING_POWER_KW:
            # The taper (or a severe derate) has stalled the session. Charging the remaining
            # SOC would take hours; report the time at the floor power rather than returning
            # infinity, so the optimiser can still rank the option and reject it on cost.
            available_kw = _MIN_CHARGING_POWER_KW
        minutes += energy_per_step_kwh / available_kw * 60.0
    return minutes


# --------------------------------------------------------------------------------------
# Plan value objects (BUILD_SPEC §7.4)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChargingCandidate:
    """A charging site the optimiser may stop at, already projected onto the route.

    Deliberately *not* a database model and *not* the API's ``ChargingStationSummary``: the
    optimiser needs six numbers and a stable id, and keeping the contract this narrow is what
    lets it be unit-tested without a database and reused for a corridor that was never
    persisted. The API maps this back onto its own station DTO when it renders the plan.
    """

    station_id: str
    """Stable identifier — the ``charging_stations.id`` or the source ``external_id``.

    Doubles as the deterministic tie-break key of the search, so it must be unique among the
    candidates handed to one call."""

    name: str
    """Human label for the rationale text, e.g. ``"Autohof Hasselberg (EnBW)"``."""

    coordinate: Coordinate
    """Site location, carried through so the API can render the stop without a second lookup."""

    offset_km: float
    """Distance from the route origin to the point on the route nearest this site."""

    detour_km: float
    """Extra distance driven because of this stop — off the route **and back on**, not
    one-way. A site directly on a motorway service area has a detour near zero."""

    max_power_kw: float
    """Strongest connector at the site, in kW; the infrastructure-side power limit."""

    operator: str | None = None
    """Betreiber, for the rationale text."""

    charging_category: ChargingCategory | None = None
    """Power class; derived from :attr:`max_power_kw` when not given."""

    def __post_init__(self) -> None:
        """Reject a candidate whose numbers cannot describe a real site."""
        if self.max_power_kw <= 0.0:
            msg = f"candidate {self.station_id!r} has non-positive max_power_kw"
            raise ValidationError(msg, details={"station_id": self.station_id})
        if self.offset_km < 0.0 or self.detour_km < 0.0:
            msg = f"candidate {self.station_id!r} has a negative offset or detour"
            raise ValidationError(msg, details={"station_id": self.station_id})

    @property
    def category(self) -> ChargingCategory:
        """Power class, derived on demand so callers never have to pass a redundant field."""
        if self.charging_category is not None:
            return self.charging_category
        return ChargingCategory.from_power(self.max_power_kw)

    @classmethod
    def from_station_record(
        cls,
        record: ChargingStationRecord,
        *,
        offset_km: float,
        detour_km: float,
    ) -> ChargingCandidate:
        """Build a candidate from an ingested Ladesäulenregister site.

        ``offset_km`` and ``detour_km`` come from the corridor query (PostGIS
        ``ST_LineLocatePoint`` / ``ST_Distance``), which is why they are arguments rather than
        something this constructor could derive.
        """
        return cls(
            station_id=record.external_id,
            name=record.operator or record.city or record.external_id,
            coordinate=record.coordinate,
            offset_km=offset_km,
            detour_km=detour_km,
            max_power_kw=record.max_power_kw or 0.0,
            operator=record.operator,
            charging_category=record.charging_category,
        )


@dataclass(frozen=True, slots=True)
class ChargingStop:
    """One stop in a plan — the payload of ``ChargingPlan.stops`` in BUILD_SPEC §7.4."""

    candidate: ChargingCandidate
    """The site. The API resolves it to a full ``ChargingStationSummary`` for the response."""

    arrival_soc_percent: float
    """State of charge on arrival, after the detour."""

    departure_soc_percent: float
    """State of charge when the plan leaves — the target the optimiser chose."""

    detour_km: float
    """Extra distance for this stop, off the route and back."""

    charge_time_min: float
    """Minutes plugged in, from the integrated charging curve."""

    energy_added_kwh: float
    """Energy delivered into the pack, in kWh."""

    max_power_kw: float
    """Highest power actually reached during this session — ``min(station, vehicle)`` at the
    arrival SOC, which is where the curve is highest."""

    avg_power_kw: float
    """Session average, ``energy_added / charge_time``. Always below :attr:`max_power_kw`
    because of the taper; the gap between the two is what a driver actually feels."""

    offset_km: float
    """Distance from the route origin to this stop."""

    rationale_de: str
    """One German sentence, generated from this stop's own numbers."""

    rationale_en: str
    """The same sentence in English."""


@dataclass(frozen=True, slots=True)
class ChargingPlan:
    """The result of :func:`optimise_charging` — BUILD_SPEC §7.4.

    ``total_time_min`` is real wall-clock time and counts a detour **once**. The objective
    weights detour time **twice**, which is a stated *preference* — drivers dislike leaving the
    corridor more than the clock alone justifies — not a claim about elapsed time. Keeping the
    two apart is why a plan can be reported honestly and still be chosen for its comfort.
    """

    feasible: bool
    """Whether a plan exists that finishes the route without dropping below the minimum SOC."""

    reason: str | None
    """``None`` when feasible; otherwise a bilingual explanation, German first."""

    reason_de: str | None
    """German half of :attr:`reason`, for a UI that renders one language."""

    reason_en: str | None
    """English half of :attr:`reason`."""

    stops: tuple[ChargingStop, ...]
    """The chosen stops in route order; empty when the route needs no charging."""

    total_time_min: float
    """Driving + charging + detour time, in minutes. Wall clock."""

    driving_time_min: float
    """On-route driving time in minutes, traffic delays already in the segment durations."""

    charging_time_min: float
    """Total time plugged in, in minutes."""

    detour_km_total: float
    """Sum of the stops' detours in kilometres."""

    detour_time_min: float
    """Time spent on detours in minutes, counted once."""

    arrival_soc_percent: float
    """State of charge at the destination."""

    min_soc_percent_reached: float
    """Lowest state of charge anywhere in the plan — the range-anxiety number."""

    alternatives_considered: int
    """How many partial plans the beam search evaluated. Reported so the UI can say the answer
    was searched for rather than guessed."""

    objective: str
    """Human-readable form of the objective that was minimised."""

    objective_value: float | None
    """Value of that objective for this plan, in minutes; ``None`` when no plan exists.

    ``None`` rather than infinity because the value is serialised into a JSON response, and
    ``Infinity`` is not valid JSON — a detail that only surfaces in production."""

    @classmethod
    def infeasible(
        cls,
        *,
        reason_de: str,
        reason_en: str,
        driving_time_min: float,
        alternatives_considered: int,
    ) -> ChargingPlan:
        """An honest "no plan exists" answer, carrying the reason in both languages."""
        return cls(
            feasible=False,
            reason=f"{reason_de} / {reason_en}",
            reason_de=reason_de,
            reason_en=reason_en,
            stops=(),
            total_time_min=driving_time_min,
            driving_time_min=driving_time_min,
            charging_time_min=0.0,
            detour_km_total=0.0,
            detour_time_min=0.0,
            arrival_soc_percent=0.0,
            min_soc_percent_reached=0.0,
            alternatives_considered=alternatives_considered,
            objective=CHARGING_OBJECTIVE,
            objective_value=None,
        )


# --------------------------------------------------------------------------------------
# Optimiser defaults
# --------------------------------------------------------------------------------------

CHARGING_OBJECTIVE: Final[str] = (
    "driving_time_min + charging_time_min + 2 * detour_time_min + range_risk_penalty_min"
)
"""The minimised quantity, verbatim from BUILD_SPEC §10.3, surfaced in every plan."""

DEFAULT_MIN_SOC_PERCENT: Final[float] = 10.0
"""Reserve the plan never goes below. 10 % of a usable pack is 30-50 km — enough to reach an
alternative if a pillar is occupied or broken, which German HPC sites regularly are."""

DEFAULT_TARGET_ARRIVAL_SOC_PERCENT: Final[float] = 10.0
"""State of charge the plan aims to arrive with."""

DEFAULT_MAX_STOPS: Final[int] = 3
"""Beam depth (BUILD_SPEC §10.3). Three stops covers any German corridor for any profile in
§9: even a van_ev at 23 kWh/100 km reaches ~700 km with three stops."""

DEFAULT_BEAM_WIDTH: Final[int] = 8
"""Partial plans kept per depth (BUILD_SPEC §10.3)."""

DEFAULT_CHARGE_TARGETS_PERCENT: Final[tuple[float, ...]] = (60.0, 70.0, 80.0, 90.0)
"""Departure SOCs the search may choose from.

A coarse grid rather than a continuous variable, because the taper makes the objective nearly
flat between neighbouring targets while a continuous search would multiply the state space for
no measurable gain. 80 % is the classic corridor stop; 90 % exists for the last leg of a long
route; nothing above 90 % is offered, since the 90-100 % band is the slowest energy on the
curve and is almost never worth the clock. The search additionally always evaluates the exact
"just enough to finish" target — see :func:`optimise_charging`."""

DEFAULT_DETOUR_SPEED_KMH: Final[float] = 60.0
"""Speed assumed on the way to and from a site: an exit ramp, a roundabout and a car park."""

DEFAULT_RISK_BUFFER_PERCENT: Final[float] = 5.0
"""Comfort band above the hard minimum SOC. Arriving below it is penalised but not forbidden."""

DEFAULT_RISK_PENALTY_MIN_PER_PERCENT: Final[float] = 2.0
"""Minutes of objective charged per percentage point inside the comfort band.

Calibrated so that a plan arriving right at the hard minimum costs 10 objective-minutes more
than one arriving with a 5 pp reserve: enough to break a tie in favour of the safer plan,
not enough to add a whole extra stop for it."""

_MIN_LEG_KM: Final[float] = 5.0
"""Two stops closer together than this are never both worth making."""

_SOC_EPSILON: Final[float] = 1e-6
"""Guards the strict comparisons that decide reachability against floating-point dust."""

_MIN_USEFUL_CHARGE_PERCENT: Final[float] = 2.0
"""A stop that adds less than this is pure overhead; the search does not generate it."""


# --------------------------------------------------------------------------------------
# Route integration helpers
# --------------------------------------------------------------------------------------


def _ordered_segments(segments: Sequence[RouteEnergySegment]) -> tuple[RouteEnergySegment, ...]:
    """Segments sorted by offset, with a deterministic tie-break on distance.

    Sorting defensively rather than trusting the caller: the route analyser produces them in
    order, but a plan that silently mis-integrates because a list arrived shuffled would be
    almost impossible to spot in a response body.
    """
    return tuple(
        sorted(segments, key=lambda segment: (segment.start_offset_km, segment.distance_km))
    )


def _integrate(
    segments: Sequence[RouteEnergySegment],
    from_km: float,
    to_km: float,
) -> tuple[float, float]:
    """Energy in kWh and duration in seconds between two route offsets.

    Segments only partially inside the interval are prorated linearly by distance. That is the
    same homogeneity assumption the physical model already makes inside a segment, so the
    proration introduces no error the model did not already accept.
    """
    if to_km <= from_km:
        return (0.0, 0.0)
    energy_kwh = 0.0
    duration_s = 0.0
    for segment in segments:
        overlap_start = max(from_km, segment.start_offset_km)
        overlap_end = min(to_km, segment.end_offset_km)
        overlap_km = overlap_end - overlap_start
        if overlap_km <= 0.0:
            continue
        if segment.distance_km <= 0.0:
            # A zero-length segment still holds time (a standstill window); take it whole once.
            energy_kwh += segment.energy_kwh
            duration_s += segment.duration_s
            continue
        share = overlap_km / segment.distance_km
        energy_kwh += segment.energy_kwh * share
        duration_s += segment.duration_s * share
    return (energy_kwh, duration_s)


def _soc_delta_percent(energy_kwh: float, profile: VehicleProfile) -> float:
    """Percentage points of usable capacity that ``energy_kwh`` represents."""
    return energy_kwh / profile.usable_capacity_kwh * 100.0


def _detour_energy_kwh(detour_km: float, profile: VehicleProfile) -> float:
    """Energy for a detour, costed at the vehicle's nominal consumption.

    The detour is off the analysed corridor, so there is no segment geometry, no weather and no
    gradient for it — the physical model has nothing to work with. Nominal consumption is the
    honest fallback and the error is bounded by the detour being short; a 3 km detour at
    16.5 kWh/100 km is half a kilowatt-hour, well inside the model's own uncertainty.
    """
    return detour_km * profile.nominal_consumption_kwh_100km / 100.0


def _detour_time_min(detour_km: float, detour_speed_kmh: float) -> float:
    """Minutes spent on a detour of ``detour_km`` at ``detour_speed_kmh``."""
    if detour_speed_kmh <= 0.0:
        msg = f"detour_speed_kmh must be positive, got {detour_speed_kmh!r}"
        raise ValidationError(msg, details={"field": "detour_speed_kmh"})
    return detour_km / detour_speed_kmh * 60.0


# --------------------------------------------------------------------------------------
# Search state
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PlanState:
    """A partial plan: where we are, how full we are, and what it has cost so far."""

    offset_km: float
    soc_percent: float
    charge_time_min: float
    detour_km: float
    detour_time_min: float
    risk_penalty_min: float
    min_soc_percent: float
    stops: tuple[ChargingStop, ...]
    visited: frozenset[str]

    @property
    def station_key(self) -> tuple[str, ...]:
        """The visited stations in plan order — the deterministic tie-break key."""
        return tuple(stop.candidate.station_id for stop in self.stops)

    @property
    def incurred_cost_min(self) -> float:
        """Objective contribution already committed, excluding the fixed driving time."""
        return self.charge_time_min + 2.0 * self.detour_time_min + self.risk_penalty_min


def _format_de(value: float, digits: int = 1) -> str:
    """German number formatting: comma as the decimal separator."""
    return f"{value:.{digits}f}".replace(".", ",")


def _format_en(value: float, digits: int = 1) -> str:
    """English number formatting: point as the decimal separator."""
    return f"{value:.{digits}f}"


def _build_stop(
    candidate: ChargingCandidate,
    *,
    profile: VehicleProfile,
    arrival_soc: float,
    departure_soc: float,
    battery_temp_c: float,
) -> ChargingStop:
    """Evaluate one charging session and write its bilingual rationale from the real numbers.

    The rationale is generated, never templated from a fixed phrase bank: it names the power
    that was actually available at the arrival SOC, the detour that was actually driven, and
    the energy that was actually added. A canned string ("a good place to charge") would be
    indistinguishable from a plan that had not been computed at all.
    """
    session_power_kw = min(
        candidate.max_power_kw,
        charging_power_kw(profile, arrival_soc, battery_temp_c),
    )
    charge_time = charge_time_minutes(
        profile,
        arrival_soc,
        departure_soc,
        candidate.max_power_kw,
        battery_temp_c,
    )
    energy_added = (departure_soc - arrival_soc) / 100.0 * profile.usable_capacity_kwh
    avg_power = energy_added / (charge_time / 60.0) if charge_time > 0.0 else 0.0

    limited_by_vehicle = charging_power_kw(profile, arrival_soc, battery_temp_c) < (
        candidate.max_power_kw - _SOC_EPSILON
    )
    limit_de = "fahrzeugseitig begrenzt" if limited_by_vehicle else "Säulenleistung"
    limit_en = "vehicle-limited" if limited_by_vehicle else "pillar-limited"

    rationale_de = (
        f"{candidate.name} bei km {_format_de(candidate.offset_km, 0)}: "
        f"Ankunft mit {_format_de(arrival_soc, 0)} % SOC, Laden auf "
        f"{_format_de(departure_soc, 0)} % — {_format_de(energy_added)} kWh in "
        f"{_format_de(charge_time, 0)} min bei {_format_de(session_power_kw, 0)} kW "
        f"({limit_de}, Ø {_format_de(avg_power, 0)} kW), "
        f"{_format_de(candidate.detour_km)} km Umweg."
    )
    rationale_en = (
        f"{candidate.name} at km {_format_en(candidate.offset_km, 0)}: "
        f"arrive at {_format_en(arrival_soc, 0)} % SOC, charge to "
        f"{_format_en(departure_soc, 0)} % — {_format_en(energy_added)} kWh in "
        f"{_format_en(charge_time, 0)} min at {_format_en(session_power_kw, 0)} kW "
        f"({limit_en}, avg {_format_en(avg_power, 0)} kW), "
        f"{_format_en(candidate.detour_km)} km detour."
    )
    return ChargingStop(
        candidate=candidate,
        arrival_soc_percent=arrival_soc,
        departure_soc_percent=departure_soc,
        detour_km=candidate.detour_km,
        charge_time_min=charge_time,
        energy_added_kwh=energy_added,
        max_power_kw=session_power_kw,
        avg_power_kw=avg_power,
        offset_km=candidate.offset_km,
        rationale_de=rationale_de,
        rationale_en=rationale_en,
    )


def optimise_charging(
    route_segments_energy: Sequence[RouteEnergySegment],
    vehicle: VehicleProfile,
    start_soc: float,
    min_soc: float = DEFAULT_MIN_SOC_PERCENT,
    candidates: Sequence[ChargingCandidate] = (),
    *,
    target_arrival_soc: float = DEFAULT_TARGET_ARRIVAL_SOC_PERCENT,
    battery_temp_c: float = 20.0,
    max_stops: int = DEFAULT_MAX_STOPS,
    beam_width: int = DEFAULT_BEAM_WIDTH,
    charge_targets_percent: Sequence[float] = DEFAULT_CHARGE_TARGETS_PERCENT,
    detour_speed_kmh: float = DEFAULT_DETOUR_SPEED_KMH,
    risk_buffer_percent: float = DEFAULT_RISK_BUFFER_PERCENT,
    risk_penalty_min_per_percent: float = DEFAULT_RISK_PENALTY_MIN_PER_PERCENT,
) -> ChargingPlan:
    """Plan the charging stops for one analysed route (BUILD_SPEC §10.3).

    **Search.** Beam search over at most ``max_stops`` stops, keeping ``beam_width`` partial
    plans per depth. A partial plan expands into every (reachable station, departure SOC) pair
    ahead of it; at every depth — including depth 0, the no-stop plan — the search also tries to
    run straight to the destination and records the result if it is feasible. The best complete
    plan by objective wins.

    **Departure SOCs.** ``charge_targets_percent`` plus one adaptive target: exactly the SOC
    needed to reach the destination with ``target_arrival_soc`` left. Without it the grid would
    force a driver to 60 % when 43 % would have finished the trip, and the objective would
    reward a needlessly long stop.

    **Ranking of partial plans** is ``committed cost + admissible estimate of the charging time
    still required`` — an A*-flavoured score, so the beam is not dominated by plans that merely
    postponed their charging. The estimate assumes the remaining energy deficit is charged at
    60 % of the vehicle's peak DC power, which no real session beats over a meaningful SOC span.

    **Determinism.** Candidates are sorted once by ``(offset_km, station_id)``; every beam and
    every best-plan comparison uses ``(rounded objective, stop count, station-id tuple)``, a
    total order. Two identical calls return identical plans, byte for byte.

    Returns a plan with ``feasible=False`` and a bilingual ``reason`` when no stop sequence
    finishes the route — never an exception, because "this car cannot do this route today" is a
    legitimate answer the UI has to render, not a server error.
    """
    if not 0.0 <= start_soc <= FULL_SOC_PERCENT:
        msg = f"start_soc must be within 0-100, got {start_soc!r}"
        raise ValidationError(msg, details={"field": "start_soc"})
    if not 0.0 <= min_soc <= FULL_SOC_PERCENT:
        msg = f"min_soc must be within 0-100, got {min_soc!r}"
        raise ValidationError(msg, details={"field": "min_soc"})
    if beam_width < 1 or max_stops < 0:
        msg = f"beam_width must be >= 1 and max_stops >= 0, got {beam_width!r}/{max_stops!r}"
        raise ValidationError(msg, details={"field": "beam_width"})

    segments = _ordered_segments(route_segments_energy)
    route_km = segments[-1].end_offset_km if segments else 0.0
    total_energy_kwh, total_duration_s = _integrate(segments, 0.0, route_km)
    driving_time_min = total_duration_s / 60.0
    comfort_soc = min(FULL_SOC_PERCENT, min_soc + risk_buffer_percent)

    ordered_candidates = tuple(
        sorted(
            (c for c in candidates if c.offset_km <= route_km + _MIN_LEG_KM),
            key=lambda c: (round(c.offset_km, 6), c.station_id),
        )
    )

    initial = _PlanState(
        offset_km=0.0,
        soc_percent=start_soc,
        charge_time_min=0.0,
        detour_km=0.0,
        detour_time_min=0.0,
        risk_penalty_min=0.0,
        min_soc_percent=start_soc,
        stops=(),
        visited=frozenset(),
    )

    def finish(state: _PlanState) -> ChargingPlan | None:
        """Drive from ``state`` to the destination; return the complete plan if it holds up."""
        energy_kwh, _ = _integrate(segments, state.offset_km, route_km)
        arrival_soc = state.soc_percent - _soc_delta_percent(energy_kwh, vehicle)
        if arrival_soc < min_soc - _SOC_EPSILON:
            return None
        if arrival_soc < target_arrival_soc - _SOC_EPSILON:
            return None
        risk = state.risk_penalty_min + max(0.0, comfort_soc - arrival_soc) * (
            risk_penalty_min_per_percent
        )
        objective_value = driving_time_min + state.charge_time_min + 2.0 * state.detour_time_min
        objective_value += risk
        return ChargingPlan(
            feasible=True,
            reason=None,
            reason_de=None,
            reason_en=None,
            stops=state.stops,
            total_time_min=driving_time_min + state.charge_time_min + state.detour_time_min,
            driving_time_min=driving_time_min,
            charging_time_min=state.charge_time_min,
            detour_km_total=state.detour_km,
            detour_time_min=state.detour_time_min,
            arrival_soc_percent=arrival_soc,
            min_soc_percent_reached=min(state.min_soc_percent, arrival_soc),
            alternatives_considered=0,
            objective=CHARGING_OBJECTIVE,
            objective_value=objective_value,
        )

    def plan_sort_key(plan: ChargingPlan) -> tuple[float, int, tuple[str, ...]]:
        """Total order over complete plans: cost, then fewer stops, then station ids.

        Only ever called on plans built by ``finish``, which always sets an objective value;
        the ``or 0.0`` keeps the type checker honest about the infeasible-plan case.
        """
        return (
            round(plan.objective_value or 0.0, 6),
            len(plan.stops),
            tuple(stop.candidate.station_id for stop in plan.stops),
        )

    def remaining_charge_estimate_min(state: _PlanState) -> float:
        """Admissible-ish lower bound on the charging time still needed from ``state``."""
        energy_kwh, _ = _integrate(segments, state.offset_km, route_km)
        usable_fraction = (state.soc_percent - target_arrival_soc) / 100.0
        available_kwh = usable_fraction * vehicle.usable_capacity_kwh
        deficit_kwh = energy_kwh - available_kwh
        if deficit_kwh <= 0.0:
            return 0.0
        optimistic_kw = vehicle.max_dc_power_kw * 0.6
        return deficit_kwh / optimistic_kw * 60.0

    def state_sort_key(state: _PlanState) -> tuple[float, int, tuple[str, ...]]:
        """Total order over partial plans: A* score, then fewer stops, then station ids."""
        score = state.incurred_cost_min + remaining_charge_estimate_min(state)
        return (round(score, 6), len(state.stops), state.station_key)

    best_plan = finish(initial)
    alternatives_considered = 1
    beam: tuple[_PlanState, ...] = (initial,)

    for _depth in range(max_stops):
        expansions: list[_PlanState] = []
        for state in beam:
            for candidate in ordered_candidates:
                if candidate.station_id in state.visited:
                    continue
                if candidate.offset_km <= state.offset_km + _MIN_LEG_KM:
                    continue
                leg_energy_kwh, _ = _integrate(segments, state.offset_km, candidate.offset_km)
                soc_on_route = state.soc_percent - _soc_delta_percent(leg_energy_kwh, vehicle)
                detour_kwh = _detour_energy_kwh(candidate.detour_km, vehicle)
                detour_soc = _soc_delta_percent(detour_kwh, vehicle)
                arrival_soc = soc_on_route - detour_soc
                if arrival_soc < min_soc - _SOC_EPSILON:
                    continue

                energy_to_end_kwh, _ = _integrate(segments, candidate.offset_km, route_km)
                needed_soc = _soc_delta_percent(energy_to_end_kwh, vehicle) + target_arrival_soc
                targets = sorted(
                    {
                        round(value, 6)
                        for value in (*charge_targets_percent, needed_soc)
                        if arrival_soc + _MIN_USEFUL_CHARGE_PERCENT <= value <= FULL_SOC_PERCENT
                    }
                )
                stop_detour_time = _detour_time_min(candidate.detour_km, detour_speed_kmh)
                leg_risk = max(0.0, comfort_soc - arrival_soc) * risk_penalty_min_per_percent
                for target in targets:
                    stop = _build_stop(
                        candidate,
                        profile=vehicle,
                        arrival_soc=arrival_soc,
                        departure_soc=target,
                        battery_temp_c=battery_temp_c,
                    )
                    expansions.append(
                        _PlanState(
                            offset_km=candidate.offset_km,
                            soc_percent=target,
                            charge_time_min=state.charge_time_min + stop.charge_time_min,
                            detour_km=state.detour_km + candidate.detour_km,
                            detour_time_min=state.detour_time_min + stop_detour_time,
                            risk_penalty_min=state.risk_penalty_min + leg_risk,
                            min_soc_percent=min(state.min_soc_percent, arrival_soc),
                            stops=(*state.stops, stop),
                            visited=state.visited | {candidate.station_id},
                        )
                    )
        if not expansions:
            break
        alternatives_considered += len(expansions)
        for state in expansions:
            plan = finish(state)
            if plan is None:
                continue
            if best_plan is None or plan_sort_key(plan) < plan_sort_key(best_plan):
                best_plan = plan
        beam = tuple(sorted(expansions, key=state_sort_key)[:beam_width])

    if best_plan is None:
        reason_de, reason_en = _infeasibility_reason(
            segments=segments,
            candidates=ordered_candidates,
            vehicle=vehicle,
            start_soc=start_soc,
            min_soc=min_soc,
            route_km=route_km,
            total_energy_kwh=total_energy_kwh,
            max_stops=max_stops,
        )
        return ChargingPlan.infeasible(
            reason_de=reason_de,
            reason_en=reason_en,
            driving_time_min=driving_time_min,
            alternatives_considered=alternatives_considered,
        )

    return ChargingPlan(
        feasible=True,
        reason=None,
        reason_de=None,
        reason_en=None,
        stops=best_plan.stops,
        total_time_min=best_plan.total_time_min,
        driving_time_min=best_plan.driving_time_min,
        charging_time_min=best_plan.charging_time_min,
        detour_km_total=best_plan.detour_km_total,
        detour_time_min=best_plan.detour_time_min,
        arrival_soc_percent=best_plan.arrival_soc_percent,
        min_soc_percent_reached=best_plan.min_soc_percent_reached,
        alternatives_considered=alternatives_considered,
        objective=CHARGING_OBJECTIVE,
        objective_value=best_plan.objective_value,
    )


def _infeasibility_reason(
    *,
    segments: Sequence[RouteEnergySegment],
    candidates: Sequence[ChargingCandidate],
    vehicle: VehicleProfile,
    start_soc: float,
    min_soc: float,
    route_km: float,
    total_energy_kwh: float,
    max_stops: int,
) -> tuple[str, str]:
    """Explain *why* no plan exists, naming the number that made it impossible.

    Three distinguishable causes, in the order a driver would ask about them. The wording
    always contains a figure the user can act on — the reachable distance, the offset of the
    nearest pillar, the number of stops allowed — because "no plan found" on its own is the
    kind of dead end that makes people distrust a planner.
    """
    usable_kwh = (start_soc - min_soc) / 100.0 * vehicle.usable_capacity_kwh
    reachable_km = _reachable_offset_km(segments, usable_kwh, route_km)

    if not candidates:
        return (
            f"Kein Ladeplan möglich: im Korridor liegt keine passende Ladesäule. "
            f"Mit {_format_de(start_soc, 0)} % SOC sind rund "
            f"{_format_de(reachable_km, 0)} km der {_format_de(route_km, 0)} km erreichbar "
            f"(Bedarf {_format_de(total_energy_kwh)} kWh).",
            f"No charging plan possible: no suitable station in the corridor. "
            f"At {_format_en(start_soc, 0)} % SOC about {_format_en(reachable_km, 0)} km of "
            f"{_format_en(route_km, 0)} km are reachable "
            f"(demand {_format_en(total_energy_kwh)} kWh).",
        )

    first_offset = min(candidate.offset_km for candidate in candidates)
    if first_offset > reachable_km:
        return (
            f"Kein Ladeplan möglich: die erste erreichbare Ladesäule liegt bei km "
            f"{_format_de(first_offset, 0)}, mit {_format_de(start_soc, 0)} % SOC reicht die "
            f"Reichweite aber nur bis km {_format_de(reachable_km, 0)} "
            f"(Mindest-SOC {_format_de(min_soc, 0)} %).",
            f"No charging plan possible: the first station sits at km "
            f"{_format_en(first_offset, 0)}, but at {_format_en(start_soc, 0)} % SOC the range "
            f"ends at km {_format_en(reachable_km, 0)} "
            f"(minimum SOC {_format_en(min_soc, 0)} %).",
        )

    return (
        f"Kein Ladeplan möglich: die Strecke ist mit höchstens {max_stops} Ladestopps nicht "
        f"zu schaffen — die Lücken zwischen den {len(candidates)} Ladesäulen im Korridor sind "
        f"für {vehicle.display_name} zu groß (Bedarf {_format_de(total_energy_kwh)} kWh auf "
        f"{_format_de(route_km, 0)} km).",
        f"No charging plan possible: the route cannot be completed with at most {max_stops} "
        f"stops — the gaps between the {len(candidates)} corridor stations are too large for "
        f"the {vehicle.display_name} (demand {_format_en(total_energy_kwh)} kWh over "
        f"{_format_en(route_km, 0)} km).",
    )


def _reachable_offset_km(
    segments: Sequence[RouteEnergySegment],
    usable_kwh: float,
    route_km: float,
) -> float:
    """How far along the route ``usable_kwh`` gets, in kilometres.

    Walks the segments and prorates inside the one where the energy runs out, so the answer is
    a real distance on this route rather than a nominal-consumption estimate.
    """
    if usable_kwh <= 0.0:
        return 0.0
    remaining = usable_kwh
    for segment in segments:
        if segment.energy_kwh <= 0.0:
            continue
        if remaining >= segment.energy_kwh:
            remaining -= segment.energy_kwh
            continue
        share = remaining / segment.energy_kwh
        return segment.start_offset_km + share * segment.distance_km
    return route_km
