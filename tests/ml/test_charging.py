"""Charging curve and corridor charging optimiser — BUILD_SPEC §10.3, §7.4.

The charging-time assertions are checked against a **closed-form integral of the taper**, which
is an independent computation: the implementation integrates numerically with a midpoint rule
over SOC, and the analytic antiderivative of the same piecewise-linear curve is worked out in
the comments. Agreement to a fraction of a percent means the quadrature is right; a naive
constant-power model would be out by a third.

The taper of BUILD_SPEC §10.3, ``f(soc)``::

    f = 1.00                       soc < 50
    f = 1.00 → 0.55 linearly       50 ≤ soc ≤ 80     f(s) = 1.00 - 0.015·(s-50)
    f = 0.55 → 0.18 linearly       80 ≤ soc ≤ 100    f(s) = 0.55 - 0.0185·(s-80)

so, with usable capacity ``C`` and peak power ``P``::

    t(a→b) = C/100/P · ∫ ds/f(s)   hours
    ∫ over 10-50 = 40            ∫ over 50-80 = (30/0.45)·ln(1/0.55)   = 39.856
    ∫ over 80-100 = (20/0.37)·ln(0.55/0.18) = 60.376
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from itertools import pairwise

import pytest

from autotwin_contracts.enums import ChargingCategory, DataOrigin, SourceSystem
from autotwin_contracts.geo import Coordinate
from autotwin_contracts.records import ChargingStationRecord, ProvenanceInfo
from autotwin_contracts.vehicles import VehicleProfile, get_vehicle_profile
from autotwin_core.errors import ValidationError
from autotwin_ml.baseline import PhysicalEnergyModel
from autotwin_ml.charging import (
    CHARGING_OBJECTIVE,
    DEFAULT_MIN_SOC_PERCENT,
    ChargingCandidate,
    ChargingPlan,
    charge_time_minutes,
    charging_power_kw,
    optimise_charging,
)
from autotwin_ml.types import RouteEnergySegment, SegmentConditions

# ``∫ ds/f(s)`` over the three bands of the documented taper, in percentage points.
INTEGRAL_10_TO_50 = 40.0
INTEGRAL_50_TO_80 = (30.0 / 0.45) * math.log(1.0 / 0.55)
INTEGRAL_80_TO_100 = (20.0 / 0.37) * math.log(0.55 / 0.18)


@pytest.fixture(scope="module")
def sedan() -> VehicleProfile:
    """``sedan_ev``: 74 kWh usable, 205 kW peak DC, 16.5 kWh/100 km nominal."""
    return get_vehicle_profile("sedan_ev")


def analytic_minutes(profile: VehicleProfile, integral: float) -> float:
    """Closed-form charging time in minutes for a vehicle-limited session."""
    return profile.usable_capacity_kwh / 100.0 / profile.max_dc_power_kw * integral * 60.0


def motorway_route(
    *, length_km: float, speed_kmh: float = 120.0, segment_km: float = 5.0
) -> tuple[RouteEnergySegment, ...]:
    """A synthetic Autobahn corridor, energies produced by the physical model.

    Built from the real model rather than from invented numbers so the SOC arithmetic in the
    optimiser is exercised against energies a route analysis could actually produce.
    """
    model = PhysicalEnergyModel()
    profile = get_vehicle_profile("sedan_ev")
    segments: list[RouteEnergySegment] = []
    offset = 0.0
    while offset < length_km - 1e-9:
        distance = min(segment_km, length_km - offset)
        result = model.segment_energy(
            profile, SegmentConditions(speed_kmh=speed_kmh, distance_km=distance)
        )
        segments.append(RouteEnergySegment.from_energy_result(offset, result))
        offset += distance
    return tuple(segments)


def candidate(
    station_id: str, offset_km: float, max_power_kw: float, detour_km: float = 1.0
) -> ChargingCandidate:
    """A corridor charging site at a given offset."""
    return ChargingCandidate(
        station_id=station_id,
        name=f"Rasthof {station_id}",
        coordinate=Coordinate(latitude=50.0, longitude=9.0),
        offset_km=offset_km,
        detour_km=detour_km,
        max_power_kw=max_power_kw,
    )


@pytest.fixture(scope="module")
def long_route() -> tuple[RouteEnergySegment, ...]:
    """700 km at 120 km/h — about 123 kWh, far beyond one charge of a 74 kWh pack."""
    return motorway_route(length_km=700.0)


@pytest.fixture(scope="module")
def one_stop_route() -> tuple[RouteEnergySegment, ...]:
    """400 km at 120 km/h — about 70 kWh, which one mid-route stop comfortably covers."""
    return motorway_route(length_km=400.0)


@pytest.fixture(scope="module")
def corridor_candidates() -> tuple[ChargingCandidate, ...]:
    """Four sites spread along the 700 km corridor, two HPC and two 150 kW."""
    return (
        candidate("a", 150.0, 150.0),
        candidate("b", 300.0, 300.0),
        candidate("c", 480.0, 150.0),
        candidate("d", 600.0, 300.0),
    )


class TestChargingPowerCurve:
    """``P(soc, T) = P_max · f(soc) · d(T)`` — the vehicle-side limit only."""

    @pytest.mark.parametrize("soc_percent", [0.0, 10.0, 25.0, 49.0, 50.0])
    def test_full_power_below_fifty_percent(
        self, sedan: VehicleProfile, soc_percent: float
    ) -> None:
        """BUILD_SPEC §10.3 puts the taper flat at 1.0 below 50 % SOC."""
        assert charging_power_kw(sedan, soc_percent) == pytest.approx(sedan.max_dc_power_kw)

    @pytest.mark.parametrize(
        ("soc_percent", "expected_fraction"),
        [
            (50.0, 1.0),
            (65.0, 0.775),  # halfway across the 50-80 ramp: 1.00 - 0.5·0.45
            (80.0, 0.55),
            (90.0, 0.365),  # halfway across the 80-100 ramp: 0.55 - 0.5·0.37
            (100.0, 0.0),  # the session is over
        ],
    )
    def test_taper_hits_the_documented_breakpoints(
        self, sedan: VehicleProfile, soc_percent: float, expected_fraction: float
    ) -> None:
        """The two ramps are linear between the four points the specification names.

        A curve that bent the other way — or that started tapering at 80 % instead of 50 % —
        would make every corridor plan optimistic by minutes per stop.
        """
        assert charging_power_kw(sedan, soc_percent) == pytest.approx(
            sedan.max_dc_power_kw * expected_fraction, rel=1e-9
        )

    def test_power_is_monotonically_non_increasing_above_fifty_percent(
        self, sedan: VehicleProfile
    ) -> None:
        """Every additional percentage point can only make charging slower, never faster.

        Sampled at 0.5 pp across the whole 50-100 band, boundaries included.
        """
        socs = [50.0 + 0.5 * step for step in range(101)]
        powers = [charging_power_kw(sedan, soc) for soc in socs]
        assert powers == sorted(powers, reverse=True)

    @pytest.mark.parametrize(
        "code", ["compact_ev", "sedan_ev", "performance_ev", "suv_ev", "van_ev"]
    )
    def test_never_exceeds_the_vehicle_maximum(self, code: str) -> None:
        """The vehicle-side limit is a ceiling by construction: both factors are ≤ 1.

        Swept over the whole SOC range and a temperature span from a January morning to a
        thermally stressed summer pack. Exceeding ``max_dc_power_kw`` anywhere would have the
        planner promise a session the car cannot accept; a negative value would make the
        integration in ``charge_time_minutes`` produce a negative time.
        """
        profile = get_vehicle_profile(code)
        for soc_percent in (0.0, 15.0, 35.0, 50.0, 55.0, 75.0, 80.0, 95.0, 100.0):
            for battery_temp_c in (-25.0, -20.0, -5.0, 10.0, 20.0, 45.0, 55.0, 70.0):
                power_kw = charging_power_kw(profile, soc_percent, battery_temp_c)
                assert 0.0 <= power_kw <= profile.max_dc_power_kw + 1e-9

    @pytest.mark.parametrize(
        ("battery_temp_c", "expected_fraction"),
        [(-20.0, 0.15), (-10.0, 0.25), (0.0, 0.50), (10.0, 1.00), (45.0, 1.00), (55.0, 0.40)],
    )
    def test_temperature_derate_hits_its_breakpoints(
        self, sedan: VehicleProfile, battery_temp_c: float, expected_fraction: float
    ) -> None:
        """Cold cells cannot accept current (plating risk); hot cells are protected."""
        assert charging_power_kw(sedan, 20.0, battery_temp_c) == pytest.approx(
            sedan.max_dc_power_kw * expected_fraction, rel=1e-9
        )

    def test_derated_at_both_temperature_extremes(self, sedan: VehicleProfile) -> None:
        """A German winter morning and a summer afternoon both cost DC power.

        The cold side bites far harder — 15 % of peak at -20 °C against 40 % at +55 °C — which
        is why preconditioning on the way to a fast charger matters so much.
        """
        mild = charging_power_kw(sedan, 20.0, 20.0)
        assert charging_power_kw(sedan, 20.0, -20.0) < mild
        assert charging_power_kw(sedan, 20.0, 55.0) < mild
        assert charging_power_kw(sedan, 20.0, -20.0) < charging_power_kw(sedan, 20.0, 55.0)

    def test_derate_curve_is_clamped_outside_its_range(self, sedan: VehicleProfile) -> None:
        """At -40 °C an extrapolated derate would go negative — a negative charging power."""
        assert charging_power_kw(sedan, 20.0, -40.0) == charging_power_kw(sedan, 20.0, -20.0)
        assert charging_power_kw(sedan, 20.0, 90.0) == charging_power_kw(sedan, 20.0, 60.0)
        assert charging_power_kw(sedan, 20.0, -40.0) > 0.0

    def test_full_pack_accepts_nothing(self, sedan: VehicleProfile) -> None:
        assert charging_power_kw(sedan, 100.0) == 0.0

    def test_negative_soc_is_a_caller_bug(self, sedan: VehicleProfile) -> None:
        with pytest.raises(ValidationError):
            charging_power_kw(sedan, -1.0)


class TestChargeTime:
    """``charge_time_minutes`` — the numerical integration of the taper."""

    @pytest.mark.parametrize(
        ("soc_from", "soc_to", "integral"),
        [
            (10.0, 50.0, INTEGRAL_10_TO_50),
            (50.0, 80.0, INTEGRAL_50_TO_80),
            (80.0, 100.0, INTEGRAL_80_TO_100),
            (10.0, 80.0, INTEGRAL_10_TO_50 + INTEGRAL_50_TO_80),
        ],
    )
    def test_matches_the_closed_form_integral_of_the_taper(
        self, sedan: VehicleProfile, soc_from: float, soc_to: float, integral: float
    ) -> None:
        """Independent check of the quadrature against the analytic antiderivative.

        ``∫ ds/(m·s + c) = ln(m·s + c)/m`` — the taper is piecewise linear, so its reciprocal
        integrates to a logarithm in closed form. The station is rated well above the vehicle
        so the vehicle side binds throughout and the comparison is clean.
        """
        expected = analytic_minutes(sedan, integral)
        assert charge_time_minutes(sedan, soc_from, soc_to, 350.0) == pytest.approx(
            expected, rel=0.002
        )

    def test_ten_to_eighty_takes_about_seventeen_minutes(self, sedan: VehicleProfile) -> None:
        """A 205 kW-capable 74 kWh sedan on an unconstrained HPC pillar.

        51.8 kWh moved in ~17 min is an average of ~180 kW, which is the right order for a
        car of this class on a 300 kW+ pillar and the number a driver would recognise.
        """
        minutes = charge_time_minutes(sedan, 10.0, 80.0, 350.0)
        assert 15.0 <= minutes <= 20.0

    @pytest.mark.parametrize("station_power_kw", [11.0, 50.0, 100.0, 150.0, 350.0])
    @pytest.mark.parametrize(
        "code", ["compact_ev", "sedan_ev", "performance_ev", "suv_ev", "van_ev"]
    )
    def test_never_beats_the_constant_power_floor(self, code: str, station_power_kw: float) -> None:
        """You cannot move energy faster than the pillar delivers it.

        ``t >= E / P_station`` is the hardest lower bound there is; a charging model that
        violated it would promise stops no infrastructure can deliver.
        """
        profile = get_vehicle_profile(code)
        energy_kwh = profile.usable_capacity_kwh * 0.7
        floor_min = energy_kwh / station_power_kw * 60.0
        assert charge_time_minutes(profile, 10.0, 80.0, station_power_kw) >= floor_min - 1e-9

    def test_a_weak_station_is_the_binding_constraint(self, sedan: VehicleProfile) -> None:
        """On a 50 kW pillar the sedan never reaches its own limit below 80 % SOC.

        The vehicle can take 205·f(soc) kW, which only falls below 50 kW at about 96 % SOC, so
        across 10 → 80 % the pillar alone decides and the answer must equal the constant-power
        floor exactly: 74·0.7 / 50 · 60 = 62.16 min.
        """
        expected = sedan.usable_capacity_kwh * 0.7 / 50.0 * 60.0
        assert expected == pytest.approx(62.16, abs=0.01)
        assert charge_time_minutes(sedan, 10.0, 80.0, 50.0) == pytest.approx(expected, rel=1e-4)

    def test_time_is_monotone_in_the_energy_added(self, sedan: VehicleProfile) -> None:
        """Charging further always takes longer, from the same starting SOC."""
        times = [charge_time_minutes(sedan, 20.0, target, 150.0) for target in range(25, 101, 5)]
        assert times == sorted(times)
        assert all(later > earlier for earlier, later in pairwise(times))

    def test_time_is_monotone_in_the_starting_soc(self, sedan: VehicleProfile) -> None:
        """Starting higher on the curve means less energy *and* less power — but never more
        time, because the energy shrinks faster than the power does on this taper."""
        times = [charge_time_minutes(sedan, start, 90.0, 150.0) for start in range(10, 90, 10)]
        assert times == sorted(times, reverse=True)

    def test_splitting_a_session_in_two_costs_the_same(self, sedan: VehicleProfile) -> None:
        """Additivity over SOC: the integral does not care where it is cut.

        Not a tautology — the midpoint rule is evaluated on different sub-intervals in the two
        cases, so agreement to 0.1 % is evidence the step size is fine enough.
        """
        whole = charge_time_minutes(sedan, 15.0, 85.0, 200.0)
        halves = charge_time_minutes(sedan, 15.0, 50.0, 200.0) + charge_time_minutes(
            sedan, 50.0, 85.0, 200.0
        )
        assert whole == pytest.approx(halves, rel=0.001)

    def test_a_finer_step_does_not_move_the_answer(self, sedan: VehicleProfile) -> None:
        """The documented convergence claim: 0.25 pp is already well inside a second."""
        coarse = charge_time_minutes(sedan, 10.0, 100.0, 350.0)
        fine = charge_time_minutes(sedan, 10.0, 100.0, 350.0, soc_step_percent=0.01)
        assert abs(coarse - fine) < 1.0 / 60.0

    def test_no_charge_requested_takes_no_time(self, sedan: VehicleProfile) -> None:
        assert charge_time_minutes(sedan, 80.0, 80.0, 150.0) == 0.0
        assert charge_time_minutes(sedan, 80.0, 50.0, 150.0) == 0.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"soc_from": 10.0, "soc_to": 80.0, "station_power_kw": 0.0},
            {"soc_from": 10.0, "soc_to": 80.0, "station_power_kw": -50.0},
            {"soc_from": -5.0, "soc_to": 80.0, "station_power_kw": 50.0},
            {"soc_from": 10.0, "soc_to": 120.0, "station_power_kw": 50.0},
        ],
        ids=["zero-power", "negative-power", "negative-soc", "soc-above-100"],
    )
    def test_impossible_arguments_raise(
        self, sedan: VehicleProfile, kwargs: dict[str, float]
    ) -> None:
        """These all mean the caller has a bug, not that charging is hard."""
        with pytest.raises(ValidationError):
            charge_time_minutes(sedan, **kwargs)

    def test_zero_step_size_raises(self, sedan: VehicleProfile) -> None:
        """A zero step would loop forever or divide by zero, depending on the rounding."""
        with pytest.raises(ValidationError):
            charge_time_minutes(sedan, 10.0, 80.0, 50.0, soc_step_percent=0.0)


class TestTheTailIsExpensive:
    """Why real EV routing charges to 80 % and not to 100 %."""

    def test_eighty_to_hundred_costs_most_of_what_ten_to_eighty_costs(
        self, sedan: VehicleProfile
    ) -> None:
        """14.8 kWh in the tail against 51.8 kWh in the bulk — and nearly as much clock.

        From the closed-form integrals: the bulk is 79.86 SOC-units of ``∫ds/f`` and the tail
        alone is 60.38, i.e. 76 % of the time for 29 % of the energy. If this relationship ever
        inverted, the optimiser would start recommending 100 % stops and every corridor plan
        would get slower while looking cheaper.
        """
        bulk_min = charge_time_minutes(sedan, 10.0, 80.0, 350.0)
        tail_min = charge_time_minutes(sedan, 80.0, 100.0, 350.0)
        bulk_kwh = sedan.usable_capacity_kwh * 0.70
        tail_kwh = sedan.usable_capacity_kwh * 0.20

        assert tail_kwh < 0.35 * bulk_kwh  # far less energy…
        assert tail_min > 0.50 * bulk_min  # …for most of the time
        assert tail_min / bulk_min == pytest.approx(
            INTEGRAL_80_TO_100 / (INTEGRAL_10_TO_50 + INTEGRAL_50_TO_80), rel=0.005
        )

    def test_the_last_kilowatt_hour_is_several_times_dearer_than_the_first(
        self, sedan: VehicleProfile
    ) -> None:
        """Minutes per kWh: ~0.33 across 10-80 %, ~0.88 across 80-100 %."""
        bulk_rate = charge_time_minutes(sedan, 10.0, 80.0, 350.0) / (
            sedan.usable_capacity_kwh * 0.70
        )
        tail_rate = charge_time_minutes(sedan, 80.0, 100.0, 350.0) / (
            sedan.usable_capacity_kwh * 0.20
        )
        assert tail_rate > 2.5 * bulk_rate

    @pytest.mark.parametrize(
        "code", ["compact_ev", "sedan_ev", "performance_ev", "suv_ev", "van_ev"]
    )
    def test_the_relationship_holds_for_every_profile(self, code: str) -> None:
        """The taper is vehicle-independent in shape, so the conclusion must be too."""
        profile = get_vehicle_profile(code)
        station_kw = profile.max_dc_power_kw * 2.0
        bulk = charge_time_minutes(profile, 10.0, 80.0, station_kw)
        tail = charge_time_minutes(profile, 80.0, 100.0, station_kw)
        assert tail > 0.5 * bulk


class TestChargingCandidate:
    """The optimiser's narrow view of a charging site."""

    @pytest.mark.parametrize(
        ("max_power_kw", "expected"),
        [
            (11.0, ChargingCategory.normal),
            (22.0, ChargingCategory.fast),
            (149.9, ChargingCategory.fast),
            (150.0, ChargingCategory.ultra_fast),
            (350.0, ChargingCategory.ultra_fast),
        ],
    )
    def test_category_is_derived_from_the_power(
        self, max_power_kw: float, expected: ChargingCategory
    ) -> None:
        """The BUILD_SPEC §2 thresholds, checked exactly on the boundaries."""
        assert candidate("x", 10.0, max_power_kw).category is expected

    def test_an_explicit_category_wins_over_the_derivation(self) -> None:
        """A site whose source already classified it is not reclassified."""
        site = ChargingCandidate(
            station_id="x",
            name="x",
            coordinate=Coordinate(latitude=50.0, longitude=9.0),
            offset_km=10.0,
            detour_km=0.0,
            max_power_kw=50.0,
            charging_category=ChargingCategory.ultra_fast,
        )
        assert site.category is ChargingCategory.ultra_fast

    @pytest.mark.parametrize(
        ("max_power_kw", "offset_km", "detour_km"),
        [(0.0, 10.0, 1.0), (-50.0, 10.0, 1.0), (150.0, -1.0, 1.0), (150.0, 10.0, -1.0)],
        ids=["zero-power", "negative-power", "negative-offset", "negative-detour"],
    )
    def test_impossible_sites_are_refused(
        self, max_power_kw: float, offset_km: float, detour_km: float
    ) -> None:
        """None of these can describe a real site, and each would poison the SOC arithmetic."""
        with pytest.raises(ValidationError):
            candidate("x", offset_km, max_power_kw, detour_km)

    def test_built_from_an_ingested_station_record(self) -> None:
        """The Ladesäulenregister adapter path, including the ``max_power_kw`` NULL the
        register really does emit — a site with no rating must not become a negative power."""
        record = ChargingStationRecord(
            external_id="bnetza:deadbeef",
            operator="EnBW mobility+ AG",
            city="Karlsruhe",
            coordinate=Coordinate(latitude=49.0069, longitude=8.4037),
            max_power_kw=300.0,
            charging_category=ChargingCategory.from_power(300.0),
            provenance=ProvenanceInfo(
                source=SourceSystem.bundesnetzagentur,
                data_origin=DataOrigin.official,
                ingested_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
            ),
        )
        site = ChargingCandidate.from_station_record(record, offset_km=42.0, detour_km=0.8)
        assert site.station_id == "bnetza:deadbeef"
        assert site.name == "EnBW mobility+ AG"
        assert site.max_power_kw == 300.0
        assert site.category is ChargingCategory.ultra_fast


class TestOptimiserDeterminism:
    """A plan that changes between two identical requests is a bug report (BUILD_SPEC §10.3)."""

    def test_the_same_inputs_produce_an_identical_plan(
        self,
        long_route: tuple[RouteEnergySegment, ...],
        corridor_candidates: tuple[ChargingCandidate, ...],
        sedan: VehicleProfile,
    ) -> None:
        first = optimise_charging(long_route, sedan, 90.0, candidates=corridor_candidates)
        second = optimise_charging(long_route, sedan, 90.0, candidates=corridor_candidates)
        assert first == second

    def test_the_candidate_order_handed_in_does_not_matter(
        self,
        long_route: tuple[RouteEnergySegment, ...],
        corridor_candidates: tuple[ChargingCandidate, ...],
        sedan: VehicleProfile,
    ) -> None:
        """Candidates are sorted once by ``(offset_km, station_id)``.

        The corridor query returns rows in whatever order PostGIS produced them; if that order
        leaked into the plan, two identical API requests could return different stops.
        """
        forward = optimise_charging(long_route, sedan, 90.0, candidates=corridor_candidates)
        backward = optimise_charging(
            long_route, sedan, 90.0, candidates=tuple(reversed(corridor_candidates))
        )
        assert forward == backward

    def test_the_segment_order_handed_in_does_not_matter(
        self,
        long_route: tuple[RouteEnergySegment, ...],
        corridor_candidates: tuple[ChargingCandidate, ...],
        sedan: VehicleProfile,
    ) -> None:
        """A shuffled segment list must not silently mis-integrate the route energy."""
        shuffled = tuple(sorted(long_route, key=lambda segment: -segment.start_offset_km))
        assert optimise_charging(
            shuffled, sedan, 90.0, candidates=corridor_candidates
        ) == optimise_charging(long_route, sedan, 90.0, candidates=corridor_candidates)


@pytest.fixture(scope="module")
def plan(
    long_route: tuple[RouteEnergySegment, ...],
    corridor_candidates: tuple[ChargingCandidate, ...],
    sedan: VehicleProfile,
) -> ChargingPlan:
    """The reference plan: 700 km, a 74 kWh sedan starting at 90 %, four corridor sites."""
    return optimise_charging(long_route, sedan, 90.0, candidates=corridor_candidates)


class TestOptimiserFeasiblePlans:
    """What a usable plan has to look like."""

    def test_a_700_km_corridor_is_planned(self, plan: ChargingPlan) -> None:
        """123 kWh of demand against a 74 kWh pack cannot be done without stopping."""
        assert plan.feasible is True
        assert plan.reason is None
        assert len(plan.stops) >= 1

    def test_stops_come_back_in_increasing_offset_order(self, plan: ChargingPlan) -> None:
        """Route order, not search order — a UI renders them down the corridor."""
        offsets = [stop.offset_km for stop in plan.stops]
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == len(offsets)  # no station visited twice

    def test_the_state_of_charge_never_drops_below_the_minimum(self, plan: ChargingPlan) -> None:
        """The reserve is the whole point of ``min_soc``: 10 % of a usable pack is 30-50 km."""
        for stop in plan.stops:
            assert stop.arrival_soc_percent >= DEFAULT_MIN_SOC_PERCENT
            assert stop.departure_soc_percent > stop.arrival_soc_percent
        assert plan.arrival_soc_percent >= DEFAULT_MIN_SOC_PERCENT
        assert plan.min_soc_percent_reached >= DEFAULT_MIN_SOC_PERCENT

    def test_reported_times_are_consistent_with_each_other(self, plan: ChargingPlan) -> None:
        """``total_time_min`` is wall clock and counts a detour once; the objective weights it
        twice as a stated *preference*. Conflating the two would let the page report a travel
        time no clock would agree with."""
        assert plan.total_time_min == pytest.approx(
            plan.driving_time_min + plan.charging_time_min + plan.detour_time_min, rel=1e-9
        )
        assert plan.charging_time_min == pytest.approx(
            sum(stop.charge_time_min for stop in plan.stops), rel=1e-9
        )
        assert plan.detour_km_total == pytest.approx(
            sum(stop.detour_km for stop in plan.stops), rel=1e-9
        )
        assert plan.objective_value is not None
        assert plan.objective_value >= plan.total_time_min

    def test_driving_time_matches_the_analysed_segments(
        self, plan: ChargingPlan, long_route: tuple[RouteEnergySegment, ...]
    ) -> None:
        """700 km at 120 km/h is 350 minutes; traffic delays would already be in the durations."""
        expected = sum(segment.duration_s for segment in long_route) / 60.0
        assert plan.driving_time_min == pytest.approx(expected, rel=1e-9)
        assert expected == pytest.approx(350.0, rel=1e-6)

    def test_every_stop_carries_its_own_energy_arithmetic(
        self, plan: ChargingPlan, sedan: VehicleProfile
    ) -> None:
        """``energy_added`` must be the SOC delta against the *usable* capacity, and the
        session average must sit below the peak because of the taper."""
        for stop in plan.stops:
            delta = stop.departure_soc_percent - stop.arrival_soc_percent
            assert stop.energy_added_kwh == pytest.approx(
                delta / 100.0 * sedan.usable_capacity_kwh, rel=1e-9
            )
            assert stop.avg_power_kw == pytest.approx(
                stop.energy_added_kwh / (stop.charge_time_min / 60.0), rel=1e-9
            )
            assert 0.0 < stop.avg_power_kw <= stop.max_power_kw + 1e-9
            assert stop.max_power_kw <= min(sedan.max_dc_power_kw, stop.candidate.max_power_kw)

    def test_every_rationale_is_bilingual_and_quotes_real_numbers(self, plan: ChargingPlan) -> None:
        """Generated from the stop's own figures, never templated from a phrase bank.

        The German text uses a comma decimal separator (BUILD_SPEC §14) and the English a point;
        both must name the site, so a canned "a good place to charge" cannot pass.
        """
        for stop in plan.stops:
            assert stop.candidate.name in stop.rationale_de
            assert stop.candidate.name in stop.rationale_en
            assert "kWh" in stop.rationale_de
            assert "SOC" in stop.rationale_de
            assert "Umweg" in stop.rationale_de
            assert "detour" in stop.rationale_en
            assert f"{stop.charge_time_min:.0f} min" in stop.rationale_en

    def test_the_objective_is_reported_verbatim(self, plan: ChargingPlan) -> None:
        """The UI says what was minimised; the string is the specification's own wording."""
        assert plan.objective == CHARGING_OBJECTIVE
        assert "2 * detour_time_min" in plan.objective

    def test_the_search_reports_how_much_it_looked_at(self, plan: ChargingPlan) -> None:
        """So the page can say the answer was searched for rather than guessed."""
        assert plan.alternatives_considered > 1

    def test_a_short_route_needs_no_stop_at_all(
        self, corridor_candidates: tuple[ChargingCandidate, ...], sedan: VehicleProfile
    ) -> None:
        """50 km on a full-ish pack: the objective is then pure driving time.

        The no-stop plan is evaluated at depth 0, before any expansion, which is why an
        unnecessary stop can never sneak into a short route.
        """
        plan = optimise_charging(
            motorway_route(length_km=50.0), sedan, 90.0, candidates=corridor_candidates
        )
        assert plan.feasible is True
        assert plan.stops == ()
        assert plan.charging_time_min == 0.0
        assert plan.objective_value == pytest.approx(plan.driving_time_min, rel=1e-9)

    def test_no_candidates_are_needed_when_none_are_used(self, sedan: VehicleProfile) -> None:
        """An empty corridor is only a problem if the route actually needs a charge."""
        plan = optimise_charging(motorway_route(length_km=50.0), sedan, 80.0, candidates=())
        assert plan.feasible is True
        assert plan.stops == ()

    def test_an_empty_route_is_trivially_feasible(self, sedan: VehicleProfile) -> None:
        """A route with no segments is an empty route, not an error."""
        plan = optimise_charging((), sedan, 50.0)
        assert plan.feasible is True
        assert plan.driving_time_min == 0.0
        assert plan.arrival_soc_percent == pytest.approx(50.0)

    def test_a_colder_pack_makes_the_same_plan_slower(
        self,
        long_route: tuple[RouteEnergySegment, ...],
        corridor_candidates: tuple[ChargingCandidate, ...],
        sedan: VehicleProfile,
    ) -> None:
        """At 0 °C the pack accepts half the current, so every stop takes longer.

        The route energy is unchanged in this comparison — only the charging side moves — so a
        plan that did not get slower would mean the temperature derate never reached the
        optimiser at all.
        """
        mild = optimise_charging(
            long_route, sedan, 90.0, candidates=corridor_candidates, battery_temp_c=20.0
        )
        cold = optimise_charging(
            long_route, sedan, 90.0, candidates=corridor_candidates, battery_temp_c=0.0
        )
        assert cold.charging_time_min > mild.charging_time_min


class TestOptimiserInfeasiblePlans:
    """ "This car cannot do this route today" is an answer, not a server error."""

    def test_no_station_in_the_corridor(
        self, long_route: tuple[RouteEnergySegment, ...], sedan: VehicleProfile
    ) -> None:
        plan = optimise_charging(long_route, sedan, 90.0, candidates=())
        assert plan.feasible is False
        assert plan.stops == ()
        assert plan.objective_value is None  # Infinity is not valid JSON
        assert plan.reason_de and plan.reason_en
        assert plan.reason == f"{plan.reason_de} / {plan.reason_en}"
        # The wording must name a figure the driver can act on, not just "no plan found".
        assert "keine passende Ladesäule" in plan.reason_de
        assert "km" in plan.reason_de and "kWh" in plan.reason_de

    def test_the_first_station_is_out_of_range(
        self, long_route: tuple[RouteEnergySegment, ...], sedan: VehicleProfile
    ) -> None:
        """Starting at 20 % SOC, ~42 km of the 700 are reachable; the pillar sits at km 350."""
        plan = optimise_charging(
            long_route, sedan, 20.0, candidates=(candidate("z", 350.0, 300.0),)
        )
        assert plan.feasible is False
        assert plan.stops == ()
        assert plan.reason_de is not None
        assert "erste erreichbare Ladesäule" in plan.reason_de
        assert plan.reason_en is not None
        assert "the first station sits at km 350" in plan.reason_en

    def test_the_gaps_between_stations_are_too_large(
        self, long_route: tuple[RouteEnergySegment, ...], sedan: VehicleProfile
    ) -> None:
        """One pillar at km 20 is reachable but leaves 680 km on one charge — impossible."""
        plan = optimise_charging(long_route, sedan, 90.0, candidates=(candidate("p", 20.0, 150.0),))
        assert plan.feasible is False
        assert plan.stops == ()
        assert plan.reason_de is not None
        assert "Ladestopps nicht zu schaffen" in plan.reason_de
        assert sedan.display_name in plan.reason_de

    def test_the_three_reasons_are_distinguishable(
        self, long_route: tuple[RouteEnergySegment, ...], sedan: VehicleProfile
    ) -> None:
        """Three causes, three messages — a single generic string would be useless to a user."""
        reasons = {
            optimise_charging(long_route, sedan, 90.0, candidates=()).reason_de,
            optimise_charging(
                long_route, sedan, 20.0, candidates=(candidate("z", 350.0, 300.0),)
            ).reason_de,
            optimise_charging(
                long_route, sedan, 90.0, candidates=(candidate("p", 20.0, 150.0),)
            ).reason_de,
        }
        assert len(reasons) == 3

    def test_forbidding_every_stop_makes_a_long_route_infeasible(
        self,
        long_route: tuple[RouteEnergySegment, ...],
        corridor_candidates: tuple[ChargingCandidate, ...],
        sedan: VehicleProfile,
    ) -> None:
        """``max_stops=0`` is the legal boundary of the beam depth, not an error."""
        plan = optimise_charging(
            long_route, sedan, 90.0, candidates=corridor_candidates, max_stops=0
        )
        assert plan.feasible is False
        assert plan.driving_time_min > 0.0  # still reports what the drive would cost

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"start_soc": 101.0},
            {"start_soc": -1.0},
            {"start_soc": 50.0, "min_soc": 120.0},
            {"start_soc": 50.0, "beam_width": 0},
            {"start_soc": 50.0, "max_stops": -1},
        ],
        ids=["soc-above-100", "negative-soc", "min-soc-above-100", "zero-beam", "negative-depth"],
    )
    def test_impossible_arguments_raise_rather_than_returning_a_plan(
        self,
        long_route: tuple[RouteEnergySegment, ...],
        sedan: VehicleProfile,
        kwargs: dict[str, float],
    ) -> None:
        """A malformed request is a 422, not an "infeasible route"."""
        with pytest.raises(ValidationError):
            optimise_charging(long_route, sedan, **kwargs)  # type: ignore[arg-type]


class TestOptimiserObjectiveMonotonicity:
    """More options must never make the answer worse."""

    @pytest.mark.parametrize(
        "subset_ids",
        [("a", "c"), ("b",), ("a", "b"), ("c", "d"), ("a", "b", "c")],
    )
    def test_a_superset_of_candidates_is_never_worse(
        self,
        long_route: tuple[RouteEnergySegment, ...],
        corridor_candidates: tuple[ChargingCandidate, ...],
        sedan: VehicleProfile,
        subset_ids: tuple[str, ...],
    ) -> None:
        """Adding a station to the corridor cannot raise the minimised objective.

        ``max_stops=2`` with ``beam_width=32`` makes the search **exhaustive** over these
        candidate sets: at most 4 sites x 5 departure targets = 20 expansions at depth 1, all of
        which survive a beam of 32, and every depth-2 expansion is passed through ``finish``
        before any pruning. So the property is a theorem here rather than a lucky beam — which
        is exactly how it should be tested, since a beam search that pruned the optimum would
        show up as a violation.
        """
        subset = tuple(c for c in corridor_candidates if c.station_id in subset_ids)
        with_fewer = optimise_charging(
            long_route, sedan, 90.0, candidates=subset, max_stops=2, beam_width=32
        )
        with_more = optimise_charging(
            long_route, sedan, 90.0, candidates=corridor_candidates, max_stops=2, beam_width=32
        )
        assert with_more.feasible is True
        if with_fewer.feasible:
            assert with_fewer.objective_value is not None
            assert with_more.objective_value is not None
            assert with_more.objective_value <= with_fewer.objective_value + 1e-9

    def test_allowing_more_stops_is_never_worse(
        self,
        long_route: tuple[RouteEnergySegment, ...],
        corridor_candidates: tuple[ChargingCandidate, ...],
        sedan: VehicleProfile,
    ) -> None:
        """Depth 3 explores every depth-2 plan on the way, so it cannot come out behind."""
        two = optimise_charging(
            long_route, sedan, 90.0, candidates=corridor_candidates, max_stops=2, beam_width=32
        )
        three = optimise_charging(
            long_route, sedan, 90.0, candidates=corridor_candidates, max_stops=3, beam_width=32
        )
        assert two.objective_value is not None
        assert three.objective_value is not None
        assert three.objective_value <= two.objective_value + 1e-9

    def test_a_detour_free_site_beats_an_identical_one_off_the_corridor(
        self, one_stop_route: tuple[RouteEnergySegment, ...], sedan: VehicleProfile
    ) -> None:
        """Detour time is weighted twice in the objective, so it must be able to decide.

        Two sites at the same offset with the same power: the one on the service area wins, and
        the gap must be about 2 x 12 km / 60 km/h = 24 objective-minutes plus the extra energy
        the 12 km detour costs.
        """
        near = candidate("near", 200.0, 300.0, detour_km=0.0)
        far = candidate("far", 200.0, 300.0, detour_km=12.0)
        on_route = optimise_charging(one_stop_route, sedan, 90.0, candidates=(near,))
        off_route = optimise_charging(one_stop_route, sedan, 90.0, candidates=(far,))
        assert on_route.objective_value is not None
        assert off_route.objective_value is not None
        assert off_route.objective_value - on_route.objective_value > 20.0

    def test_a_stronger_pillar_beats_a_weaker_one_at_the_same_place(
        self, one_stop_route: tuple[RouteEnergySegment, ...], sedan: VehicleProfile
    ) -> None:
        """Charging time is the other half of the objective; 300 kW must beat 50 kW."""
        strong = optimise_charging(
            one_stop_route, sedan, 90.0, candidates=(candidate("hpc", 200.0, 300.0),)
        )
        weak = optimise_charging(
            one_stop_route, sedan, 90.0, candidates=(candidate("slow", 200.0, 50.0),)
        )
        assert strong.objective_value is not None
        assert weak.objective_value is not None
        assert strong.objective_value < weak.objective_value


class TestRegressions:
    """Defects found while writing this suite, now fixed and pinned."""

    def test_a_sub_kilowatt_station_must_not_beat_its_constant_power_floor(
        self, sedan: VehicleProfile
    ) -> None:
        """``t >= E / P_station`` must hold for *every* positive station power.

        Reachable from real data: ``ChargingCandidate`` accepts any ``max_power_kw > 0``, and the
        Ladesäulenregister does publish malformed power columns, so a corrupt 0.5 kW row would
        make the optimiser believe in a stop that takes half as long as it can.
        """
        station_kw = 0.5
        energy_kwh = sedan.usable_capacity_kwh * 0.7
        floor_min = energy_kwh / station_kw * 60.0
        assert charge_time_minutes(sedan, 10.0, 80.0, station_kw) >= floor_min - 1e-9
