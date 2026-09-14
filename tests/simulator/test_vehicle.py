"""Physics invariants of one simulated vehicle.

Every number asserted here is derived from something outside the implementation: from the
constants the module documents (pack heat capacity, auxiliary base load, charger power), from
the closed-form solution of the differential equation the code integrates, or from the shape of
the road-load equation. A test that pinned the numbers the simulator happens to produce today
would pass a rewrite that quietly doubled the drag term.

The invariants themselves come from BUILD_SPEC §13 and ADR 004: a simulator whose state of
charge can rise while driving, whose odometer can go backwards, or whose acceleration reaches
20 m/s² is not simulating a car, and the ML model trained on its output would be learning
nothing about electric mobility.
"""

from __future__ import annotations

import math
import statistics
from datetime import timedelta
from itertools import pairwise

import pytest

from autotwin_contracts import (
    DataOrigin,
    RoadClass,
    TelemetryEvent,
    TrafficSeverity,
    TripEventType,
    VehicleState,
    WeatherCondition,
)
from autotwin_ml.constants import DEFAULT_PHYSICS
from autotwin_simulator.driver import (
    CRUISE_SPEED_KMH,
    WEATHER_SPEED_FACTORS,
    Driver,
    DriverProfile,
    sample_driver_profile,
)
from autotwin_simulator.environment import RoadConditions, RouteEnvironment, WeatherMode
from autotwin_simulator.vehicle import (
    CRITICAL_SOC_PERCENT,
    FALLBACK_CHARGER_POWER_KW,
    PACK_AMBIENT_TIME_CONSTANT_S,
    PACK_HEATING_C_PER_KWH,
)
from autotwin_streaming.sinks import telemetry_row

from . import (
    SIMULATION_START,
    build_environment,
    build_vehicle,
    charging_candidate,
    drive,
)

# The comfort limits `sample_driver_profile` clamps to. A measured acceleration can never exceed
# them, because the speed is integrated from an acceleration that is clipped to them and the
# reported value is recomputed from the speed change.
MAX_COMFORT_ACCELERATION_MS2 = 2.2
MAX_COMFORT_DECELERATION_MS2 = 3.0

# BUILD_SPEC §13 / the brief: "a simulator emitting 20 m/s² is not simulating a car."
PLAUSIBLE_ACCELERATION_MS2 = 4.0

# Head-room allowed between a driver's target speed and the speed actually reached. The
# acceleration carries an Ornstein-Uhlenbeck noise term of sigma = 0.18 m/s^2 with an 8 s
# correlation time, fed through a first-order lag of tau <= 12 s; the stationary spread of the
# speed is of order sigma * tau * sqrt(tau_n / (tau + tau_n)), about 1.2 m/s or 4 km/h, so four
# standard deviations is ~16 km/h. Anything beyond that is not noise, it is a broken speed model.
SPEED_NOISE_HEADROOM_KMH = 16.0


@pytest.fixture
def motorway() -> RouteEnvironment:
    """100 km of level, unrestricted-limit-130 motorway at +20 °C in free-flowing traffic."""
    return build_environment(length_km=100.0)


class TestStateOfCharge:
    """SOC is the integral of energy over usable capacity, so it moves one way at a time."""

    def test_never_leaves_zero_to_one_hundred(self, motorway: RouteEnvironment) -> None:
        """The wire contract bounds it, and a violation would fail Pydantic before PostGIS did."""
        vehicle = build_vehicle(motorway, soc_percent=6.0)
        for step in drive(vehicle, motorway, ticks=400):
            assert 0.0 <= step.sample.event.battery_soc_percent <= 100.0

    def test_never_rises_while_driving(self, motorway: RouteEnvironment) -> None:
        """A downhill stretch cannot refill the pack.

        ``PhysicalEnergyModel.segment_energy`` floors a segment at zero net energy, so recovered
        energy shows up as *less consumption*, never as charge. A rising SOC in a driving sample
        would mean the simulator had invented energy.
        """
        vehicle = build_vehicle(motorway, soc_percent=80.0)
        samples = [
            step.sample.event
            for step in drive(vehicle, motorway, ticks=300)
            if step.sample.event.state is VehicleState.driving
        ]
        assert len(samples) > 100  # the corridor is long enough for this to mean something
        for previous, current in pairwise(samples):
            assert current.battery_soc_percent <= previous.battery_soc_percent + 1e-9

    def test_strictly_decreases_over_a_trip(self, motorway: RouteEnvironment) -> None:
        """100 km of motorway must cost a measurable share of a 74 kWh pack.

        A sedan at ~16.5 kWh/100 km nominal spends of the order of 20 % of its usable capacity
        on 100 km; the bound is deliberately loose (5-60 %) because the point is to catch a
        simulator that consumes nothing or empties the battery, not to pin the model.
        """
        vehicle = build_vehicle(motorway, soc_percent=95.0)
        drive(vehicle, motorway, ticks=400)
        drop = 95.0 - vehicle.soc_percent
        assert 5.0 < drop < 60.0

    def test_rises_while_charging_and_stops_at_the_driver_s_departure_soc(self) -> None:
        """Charging is the one state where SOC increases, and it ends where the driver says."""
        environment = build_environment(
            length_km=100.0,
            charging_candidates=(charging_candidate(offset_km=30.0),),
        )
        vehicle = build_vehicle(environment, soc_percent=20.0, seed=3)
        steps = drive(vehicle, environment, ticks=400)
        charging = [
            step.sample.event for step in steps if step.sample.event.state is VehicleState.charging
        ]
        assert charging, "a vehicle starting at 20 % must stop to charge on a 100 km corridor"
        for previous, current in pairwise(charging):
            assert current.battery_soc_percent >= previous.battery_soc_percent - 1e-9
        # The session ends at the driver's own departure SOC, not at 100 %.
        target = vehicle.driver.profile.departure_soc_percent
        assert charging[-1].battery_soc_percent == pytest.approx(target, abs=0.5)

    def test_parked_vehicle_drains_only_the_twelve_volt_base_load(self) -> None:
        """One hour parked costs `auxiliary_base_kw` kWh — 0.35 kWh, and nothing else.

        The HVAC envelope is zero at +20 °C, so the whole idle draw is the base load. Expressed
        as a percentage of the sedan's 74 kWh usable pack that is 0.473 points an hour, which is
        the number this asserts: a parked car that loses several percent an hour would make every
        overnight figure in the demo wrong.
        """
        environment = build_environment()
        vehicle = build_vehicle(environment, soc_percent=50.0)
        assert vehicle.state is VehicleState.idle

        for index in range(360):  # 360 ticks of 10 s = 1 h
            vehicle.advance(
                start_time=SIMULATION_START + timedelta(seconds=10 * index),
                duration_s=10.0,
                physics_step_s=1.0,
            )

        base_kwh = DEFAULT_PHYSICS.auxiliary_base_kw  # kW for one hour = kWh
        expected_drop = base_kwh / vehicle.profile.usable_capacity_kwh * 100.0
        assert 50.0 - vehicle.soc_percent == pytest.approx(expected_drop, rel=1e-6)


class TestTripCounters:
    """Odometer and cumulative energy are trip-scoped totals, so they only ever grow."""

    def test_odometer_and_energy_are_non_decreasing(self, motorway: RouteEnvironment) -> None:
        """They are what the streaming consumer assigns to ``trips``; a dip would rewrite history.

        ``_UPDATE_TRIP_AGGREGATES`` guards with ``incoming.distance_m >= t.distance_m``, so a
        vehicle whose odometer went backwards would silently stop updating its own trip row.
        """
        vehicle = build_vehicle(motorway, soc_percent=80.0)
        events = [step.sample.event for step in drive(vehicle, motorway, ticks=300)]
        for previous, current in pairwise(events):
            assert current.odometer_m >= previous.odometer_m - 1e-9
            assert current.cumulative_energy_kwh >= previous.cumulative_energy_kwh - 1e-9

    def test_odometer_ends_at_the_corridor_length(self, motorway: RouteEnvironment) -> None:
        """The vehicle integrates along the polyline, so a completed trip has driven its length.

        Within the arrival tolerance of 25 m; the corridor is ~100 km, so this also catches an
        offset that advanced at the wrong scale (metres read as kilometres and the like).
        """
        vehicle = build_vehicle(motorway, soc_percent=95.0)
        drive(vehicle, motorway, ticks=600)
        assert vehicle.state is VehicleState.idle  # the trip finished
        assert vehicle.odometer_m == pytest.approx(motorway.route.length_m, abs=30.0)

    def test_begin_trip_resets_the_trip_counters_but_not_the_car(
        self, motorway: RouteEnvironment
    ) -> None:
        """A fleet whose batteries reset between trips would never charge and never show winter."""
        vehicle = build_vehicle(motorway, soc_percent=80.0)
        drive(vehicle, motorway, ticks=60)
        soc_before = vehicle.soc_percent
        pack_before = vehicle.battery_temperature_c
        assert vehicle.odometer_m > 0.0

        vehicle.begin_trip(motorway, SIMULATION_START)

        assert vehicle.odometer_m == 0.0
        assert vehicle.cumulative_energy_kwh == 0.0
        assert vehicle.offset_m == 0.0
        assert vehicle.soc_percent == soc_before
        assert vehicle.battery_temperature_c == pack_before

    def test_trip_ids_are_unique_per_trip(self, motorway: RouteEnvironment) -> None:
        """``trips.trip_id`` is a natural key; two trips sharing one would merge in the database."""
        vehicle = build_vehicle(motorway, soc_percent=80.0)
        first = vehicle.begin_trip(motorway, SIMULATION_START).trip_id
        second = vehicle.begin_trip(motorway, SIMULATION_START).trip_id
        assert first != second
        assert first.startswith("ATW-0001-test-")


class TestSpeedEnvelope:
    """What the driver wants, and what the vehicle actually does."""

    @pytest.mark.parametrize(
        ("road_class", "speed_limit_kmh", "severity", "condition"),
        [
            (RoadClass.motorway, None, TrafficSeverity.low, WeatherCondition.clear),
            (RoadClass.motorway, 120.0, TrafficSeverity.low, WeatherCondition.clear),
            (RoadClass.motorway, 130.0, TrafficSeverity.severe, WeatherCondition.clear),
            (RoadClass.trunk, 100.0, TrafficSeverity.moderate, WeatherCondition.rain),
            (RoadClass.primary, None, TrafficSeverity.high, WeatherCondition.snow),
            (RoadClass.residential, 50.0, TrafficSeverity.low, WeatherCondition.fog),
            (RoadClass.service, 30.0, TrafficSeverity.low, WeatherCondition.clear),
            (RoadClass.unknown, None, TrafficSeverity.low, WeatherCondition.clouds),
        ],
    )
    def test_target_speed_matches_the_documented_formula(
        self,
        road_class: RoadClass,
        speed_limit_kmh: float | None,
        severity: TrafficSeverity,
        condition: WeatherCondition,
    ) -> None:
        """Recomputed from the published constants, not from the implementation's output.

        cruise(class)·aggressiveness, capped at limit·compliance, divided by the severity's delay
        factor (a factor that multiplies travel time divides speed — the identity the insight
        generator inverts for its no-traffic counterfactual), times the weather factor.
        """
        driver = Driver(sample_driver_profile(11, "ATW-0011"), seed=11, vehicle_id="ATW-0011")
        conditions = RoadConditions(
            offset_m=0.0,
            road_class=road_class,
            speed_limit_kmh=speed_limit_kmh,
            gradient_percent=0.0,
            heading_deg=180.0,
            temperature_c=10.0,
            precipitation_mm=0.0,
            wind_speed_ms=0.0,
            wind_direction_deg=240.0,
            traffic_severity=severity,
            condition=condition,
        )

        expected = CRUISE_SPEED_KMH[road_class] * driver.profile.aggressiveness
        if speed_limit_kmh is not None:
            expected = min(expected, speed_limit_kmh * driver.profile.limit_compliance)
        expected /= severity.delay_factor
        expected *= WEATHER_SPEED_FACTORS[condition]
        expected = max(5.0, expected)

        assert driver.target_speed_kmh(conditions) == pytest.approx(expected)

    def test_target_never_falls_to_a_crawl(self) -> None:
        """Severe traffic in snow on a service road still leaves a floor of 5 km/h.

        Without the floor a pathological combination divides the target far enough down that the
        vehicle never reaches the end of its corridor and the run fills with stuck cars.
        """
        driver = Driver(sample_driver_profile(11, "ATW-0011"), seed=11, vehicle_id="ATW-0011")
        conditions = RoadConditions(
            offset_m=0.0,
            road_class=RoadClass.service,
            speed_limit_kmh=5.0,
            gradient_percent=0.0,
            heading_deg=180.0,
            temperature_c=-5.0,
            precipitation_mm=2.0,
            wind_speed_ms=0.0,
            wind_direction_deg=240.0,
            traffic_severity=TrafficSeverity.severe,
            condition=WeatherCondition.snow,
        )
        assert driver.target_speed_kmh(conditions) == pytest.approx(5.0)

    @pytest.mark.parametrize(
        ("road_class", "speed_limit_kmh"),
        [
            (RoadClass.motorway, 130.0),
            (RoadClass.motorway, None),
            (RoadClass.primary, 100.0),
            (RoadClass.residential, 50.0),
        ],
    )
    def test_realised_speed_tracks_the_target(
        self, road_class: RoadClass, speed_limit_kmh: float | None
    ) -> None:
        """The cruise speed averages the target and never runs far above it.

        Both halves matter. A *mean* that drifted from the target would bias every consumption
        figure the training set is built from; a *maximum* far above it would mean the noise
        term is driving the vehicle rather than texturing it.
        """
        environment = build_environment(
            length_km=120.0,
            road_class=road_class,
            speed_limit_kmh=speed_limit_kmh,
        )
        vehicle = build_vehicle(environment, soc_percent=95.0)
        target = vehicle.driver.target_speed_kmh(
            environment.conditions_at(1_000.0, SIMULATION_START)
        )
        speeds = [
            step.sample.event.speed_kmh
            for step in drive(vehicle, environment, ticks=900, tick_seconds=2.0)
        ]
        cruising = [speed for speed in speeds if speed > 0.6 * target]

        assert min(speeds) >= 0.0
        assert statistics.fmean(cruising) == pytest.approx(target, rel=0.03)
        assert max(speeds) <= target + SPEED_NOISE_HEADROOM_KMH

    def test_a_posted_limit_holds_a_fast_driver_back(self) -> None:
        """An 80 km/h limit must beat the 130 km/h motorway cruising speed.

        Identical corridors but for the sign: the limited one has to be materially slower, or the
        posted limit is not reaching the driver model at all.
        """
        unrestricted = build_environment(length_km=60.0, speed_limit_kmh=None)
        limited = build_environment(length_km=60.0, speed_limit_kmh=80.0)
        conditions_free = unrestricted.conditions_at(1_000.0, SIMULATION_START)
        conditions_limited = limited.conditions_at(1_000.0, SIMULATION_START)
        driver = build_vehicle(unrestricted).driver

        assert driver.target_speed_kmh(conditions_limited) < 0.75 * driver.target_speed_kmh(
            conditions_free
        )


class TestAcceleration:
    """The acceleration trace has to be something a car could have produced."""

    def test_stays_within_plausible_bounds(self, motorway: RouteEnvironment) -> None:
        """|a| below 4 m/s², and in fact below the driver's own comfort limits."""
        vehicle = build_vehicle(motorway, soc_percent=90.0)
        accelerations = [
            step.sample.event.acceleration_ms2 for step in drive(vehicle, motorway, ticks=400)
        ]
        assert accelerations
        assert max(abs(value) for value in accelerations) < PLAUSIBLE_ACCELERATION_MS2
        assert max(accelerations) <= MAX_COMFORT_ACCELERATION_MS2 + 1e-9
        assert min(accelerations) >= -MAX_COMFORT_DECELERATION_MS2 - 1e-9

    def test_acceleration_integrates_to_the_speed_change(self) -> None:
        """a = Δv/Δt exactly, when one sample covers exactly one physics step.

        The driver returns the acceleration *measured* from the speed change rather than the one
        it requested, and the two differ whenever the speed floors at zero. A trace whose
        acceleration does not integrate to its speed is one nobody can validate against a real
        vehicle's CAN log.
        """
        environment = build_environment(length_km=40.0)
        vehicle = build_vehicle(environment, soc_percent=90.0)
        steps = drive(
            vehicle,
            environment,
            ticks=300,
            tick_seconds=1.0,
            physics_step_s=1.0,
        )
        previous_speed = 0.0
        for step in steps:
            event = step.sample.event
            measured = (event.speed_kmh - previous_speed) / 3.6  # km/h over 1 s -> m/s²
            assert event.acceleration_ms2 == pytest.approx(measured, abs=1e-9)
            previous_speed = event.speed_kmh


class TestStandstills:
    """Congestion is modelled as halts, because that is what a jam costs in energy.

    Dividing the target speed by a delay factor alone yields a vehicle gliding along a jammed
    Autobahn at a steady 65 km/h. The accelerate-brake-accelerate cycle is exactly the signal the
    physical baseline cannot see and the learned model can, so its *rate* is part of the contract.
    """

    HORIZON_S = 18_000.0
    """Five simulated hours — long enough for the counts below to be several sigma apart."""

    MEAN_HALT_S = 27.5
    """Midpoint of the 8-50 s uniform halt duration; dead time between draws."""

    @staticmethod
    def _conditions(severity: TrafficSeverity) -> RoadConditions:
        """A motorway point that differs only in its traffic state."""
        return RoadConditions(
            offset_m=0.0,
            road_class=RoadClass.motorway,
            speed_limit_kmh=130.0,
            gradient_percent=0.0,
            heading_deg=180.0,
            temperature_c=20.0,
            precipitation_mm=0.0,
            wind_speed_ms=0.0,
            wind_direction_deg=240.0,
            traffic_severity=severity,
            condition=WeatherCondition.clear,
        )

    def _count_halts(self, severity: TrafficSeverity, *, dt_s: float, seed: int) -> int:
        """Drive a lone driver for :data:`HORIZON_S` and count how often it came to a stop."""
        driver = Driver(sample_driver_profile(seed, "ATW-0001"), seed=seed, vehicle_id="ATW-0001")
        conditions = self._conditions(severity)
        speed_kmh = 100.0
        halts = 0
        was_halted = False
        for _ in range(int(self.HORIZON_S / dt_s)):
            speed_kmh, _acceleration = driver.update(speed_kmh, conditions, dt_s)
            if driver.is_halted and not was_halted:
                halts += 1
            was_halted = driver.is_halted
        return halts

    @pytest.mark.parametrize("severity", [TrafficSeverity.low, TrafficSeverity.moderate])
    def test_free_flowing_and_merely_dense_traffic_never_halts(
        self, severity: TrafficSeverity
    ) -> None:
        """A busy Autobahn is not a stopped one; the rate for these two levels is exactly zero."""
        assert self._count_halts(severity, dt_s=1.0, seed=1) == 0

    @pytest.mark.parametrize(
        ("severity", "rate_per_s"),
        [(TrafficSeverity.high, 1.0 / 900.0), (TrafficSeverity.severe, 1.0 / 180.0)],
    )
    def test_halt_frequency_matches_the_documented_rate(
        self, severity: TrafficSeverity, rate_per_s: float
    ) -> None:
        """One halt per 15 minutes in `high`, one per 3 minutes in `severe`.

        A halt blocks further draws for its own duration, so the expected count over a horizon T
        is T / (1/rate + mean halt length) — 19 halts in five hours of `high` traffic and 86 in
        `severe`. The tolerance is three standard deviations of the Poisson count.
        """
        expected = self.HORIZON_S / (1.0 / rate_per_s + self.MEAN_HALT_S)
        tolerance = 3.0 * math.sqrt(expected)
        halts = self._count_halts(severity, dt_s=1.0, seed=1)
        assert expected - tolerance <= halts <= expected + tolerance

    def test_halt_frequency_is_independent_of_the_physics_step(self) -> None:
        """Doubling the speed factor must not double the number of jams.

        The per-step probability is ``1 - exp(-rate·dt)`` rather than ``rate·dt``, so five hours
        of `severe` traffic contains the same number of halts whether it is integrated in 1 s or
        5 s steps. Without that, a run at 20x would look like a different country from the same
        run at 10x.
        """
        expected = self.HORIZON_S / (180.0 + self.MEAN_HALT_S)
        tolerance = 3.0 * math.sqrt(expected)
        for dt_s in (1.0, 5.0):
            halts = self._count_halts(TrafficSeverity.severe, dt_s=dt_s, seed=2)
            assert expected - tolerance <= halts <= expected + tolerance


class TestBatteryThermal:
    """The pack warms under load and relaxes toward ambient at rest."""

    def test_warms_under_load(self) -> None:
        """Ohmic loss is 0.63 K per kWh through the pack, so 100 km must warm it measurably.

        Starting at ambient means every Kelvin gained came from the load: passive exchange can
        only pull the pack back toward the air it started at.
        """
        environment = build_environment(length_km=100.0)
        vehicle = build_vehicle(environment, soc_percent=95.0, ambient_temperature_c=20.0)
        temperatures = [
            step.sample.event.battery_temperature_c
            for step in drive(vehicle, environment, ticks=400)
        ]
        assert max(temperatures) > 20.0 + 3.0
        # 20 kWh through a 0.111 kWh/K pack at 7 % loss is ~13 K before the ambient lag pulls
        # back; the equilibrium sits well under that. Anything above +30 K is not a battery.
        assert max(temperatures) < 20.0 + 30.0

    def test_relaxes_toward_ambient_at_rest(self) -> None:
        """One hour parked follows the closed-form solution of the first-order lag.

        T(t) = T_air + (T0 - T_air)·exp(-t/τ) with τ = 1800 s, plus the self-heating of the 12 V
        base load: 0.35 kWh over the hour at 0.63 K/kWh ≈ 0.22 K. Both terms are asserted, so a
        regression in either the time constant or the heating coefficient shows up here.
        """
        environment = build_environment(weather_mode=WeatherMode.cold)  # pinned to -10 °C
        vehicle = build_vehicle(environment, soc_percent=60.0, ambient_temperature_c=25.0)
        assert vehicle.state is VehicleState.idle

        for index in range(360):
            vehicle.advance(
                start_time=SIMULATION_START + timedelta(seconds=10 * index),
                duration_s=10.0,
                physics_step_s=1.0,
            )

        ambient_c = -10.0
        decay = math.exp(-3600.0 / PACK_AMBIENT_TIME_CONSTANT_S)
        self_heating_k = PACK_HEATING_C_PER_KWH * DEFAULT_PHYSICS.auxiliary_base_kw
        expected = ambient_c + (25.0 - ambient_c) * decay + self_heating_k
        assert vehicle.battery_temperature_c == pytest.approx(expected, abs=0.15)

    def test_active_cooling_keeps_a_hard_charge_out_of_thermal_runaway(self) -> None:
        """Above 35 °C the cooling branch engages, so a 150 kW session cannot cook the pack."""
        environment = build_environment(
            length_km=100.0,
            weather_mode=WeatherMode.hot,  # +35 °C ambient
            charging_candidates=(charging_candidate(offset_km=20.0, max_power_kw=300.0),),
        )
        vehicle = build_vehicle(environment, soc_percent=15.0, ambient_temperature_c=35.0, seed=4)
        temperatures = [
            step.sample.event.battery_temperature_c
            for step in drive(vehicle, environment, ticks=600)
        ]
        assert max(temperatures) < 60.0


class TestConsumptionVersusSpeed:
    """Aerodynamic drag grows with the square of speed, and the simulator has to show it."""

    def test_consumption_rises_monotonically_across_a_speed_sweep(self) -> None:
        """Identical corridors differing only in the posted limit.

        The sweep starts at 80 km/h: below roughly 50 km/h the auxiliary load spread over fewer
        kilometres per hour starts to dominate and the curve turns back up, so the relation is
        only monotone on the motorway half of the range. Stating the range is the point — an
        implementation that got the drag term wrong would flatten or invert this.
        """
        consumptions: list[float] = []
        for limit in (80.0, 100.0, 120.0, 130.0):
            environment = build_environment(length_km=100.0, speed_limit_kmh=limit, seed=11)
            vehicle = build_vehicle(environment, soc_percent=95.0, seed=11)
            drive(vehicle, environment, ticks=800)
            assert vehicle.odometer_m > 50_000.0
            consumptions.append(vehicle.cumulative_energy_kwh / (vehicle.odometer_m / 100_000.0))

        for slower, faster in pairwise(consumptions):
            assert faster > slower
        # 130 km/h against 80 km/h: drag is (130/80)^2, about 2.6 times larger, but rolling
        # resistance and the auxiliary load do not scale with speed, so the *total* rises by
        # tens of percent rather than by 160 %.
        assert 1.2 < consumptions[-1] / consumptions[0] < 2.2

    def test_cold_weather_costs_more_than_mild(self) -> None:
        """The winter penalty is the single most interesting property of an EV energy model.

        Same corridor, same seed, same driver: only the fixed-temperature mode differs, so the
        whole difference is the HVAC envelope plus the cold-battery resistance term.
        """
        results: dict[WeatherMode, float] = {}
        for mode in (WeatherMode.cold, WeatherMode.mild):
            environment = build_environment(length_km=80.0, weather_mode=mode, seed=13)
            vehicle = build_vehicle(
                environment,
                soc_percent=95.0,
                seed=13,
                ambient_temperature_c=-10.0 if mode is WeatherMode.cold else 20.0,
            )
            drive(vehicle, environment, ticks=800)
            results[mode] = vehicle.cumulative_energy_kwh / (vehicle.odometer_m / 100_000.0)

        assert results[WeatherMode.cold] > results[WeatherMode.mild] * 1.05


class TestChargingBehaviour:
    """Where a vehicle stops, why, and what it says about it."""

    def test_books_a_real_site_and_reports_its_external_id(self) -> None:
        """A stop at an ingested site carries that site's ``external_id`` on the trip event."""
        environment = build_environment(
            length_km=100.0,
            charging_candidates=(charging_candidate(offset_km=25.0, station_id="bnetza:4711"),),
        )
        vehicle = build_vehicle(environment, soc_percent=18.0, seed=3)
        steps = drive(vehicle, environment, ticks=400)
        events = [event for step in steps for event in step.trip_events]
        started = [e for e in events if e.event_type is TripEventType.charging_started]
        finished = [e for e in events if e.event_type is TripEventType.charging_finished]

        assert [e.station_id for e in started] == ["bnetza:4711"]
        assert [e.reason for e in started] == ["soc_below_reserve"]
        assert [e.station_id for e in finished] == ["bnetza:4711"]
        assert [e.reason for e in finished] == ["target_soc_reached"]

    def test_exactly_at_the_critical_soc_triggers_the_synthetic_rescue_charge(self) -> None:
        """The threshold is ``soc <= 3 %``, so a vehicle sitting exactly on it charges.

        The fallback is deliberately distinguishable from a real stop: ``station_id`` is null, so
        nothing downstream can count this session as infrastructure that exists.
        """
        environment = build_environment(length_km=100.0)  # no ingested sites at all
        vehicle = build_vehicle(environment, soc_percent=CRITICAL_SOC_PERCENT)
        step = drive(vehicle, environment, ticks=1, tick_seconds=1.0)[0]

        assert vehicle.state is VehicleState.charging
        assert [(e.event_type, e.station_id, e.reason) for e in step.trip_events] == [
            (TripEventType.charging_started, None, "soc_depleted")
        ]

    def test_just_above_the_critical_soc_keeps_driving(self) -> None:
        """The other side of the same threshold — 3.5 % is low, not stranded."""
        environment = build_environment(length_km=100.0)
        vehicle = build_vehicle(environment, soc_percent=CRITICAL_SOC_PERCENT + 0.5)
        step = drive(vehicle, environment, ticks=1, tick_seconds=1.0)[0]

        assert vehicle.state is VehicleState.driving
        assert step.trip_events == ()

    def test_fallback_charger_draws_its_documented_power(self) -> None:
        """50 kW at the plug, minus the 0.35 kW auxiliary base load the charger also carries.

        Reported as *negative* instantaneous power, because the pack is being filled rather than
        emptied — the sign convention every chart in the frontend depends on.
        """
        environment = build_environment(length_km=100.0)
        vehicle = build_vehicle(environment, soc_percent=CRITICAL_SOC_PERCENT)
        step = drive(vehicle, environment, ticks=1, tick_seconds=1.0)[0]

        expected_kw = FALLBACK_CHARGER_POWER_KW - DEFAULT_PHYSICS.auxiliary_power_kw(20.0)
        assert step.sample.event.instantaneous_power_kw == pytest.approx(-expected_kw, rel=1e-6)

    def test_charging_is_capped_by_the_weaker_of_car_and_charger(self) -> None:
        """An 11 kW site cannot push 205 kW into a sedan, and the reverse is also true."""
        environment = build_environment(
            length_km=100.0,
            charging_candidates=(charging_candidate(offset_km=20.0, max_power_kw=60.0),),
        )
        vehicle = build_vehicle(environment, soc_percent=15.0, seed=3)
        powers = [
            step.sample.event.instantaneous_power_kw
            for step in drive(vehicle, environment, ticks=500)
            if step.sample.event.state is VehicleState.charging
        ]
        assert powers
        assert min(powers) >= -60.0  # never more than the site can deliver


class TestTelemetryContract:
    """What leaves the vehicle is the wire contract, and it says it is simulated."""

    def test_every_sample_validates_against_the_pydantic_model(
        self, motorway: RouteEnvironment
    ) -> None:
        """Round-tripped through JSON, which is how it reaches Kafka and the consumer."""
        vehicle = build_vehicle(motorway, soc_percent=60.0)
        for step in drive(vehicle, motorway, ticks=120):
            event = step.sample.event
            restored = TelemetryEvent.model_validate_json(event.model_dump_json())
            assert restored == event
            assert 0.0 <= (restored.heading_deg or 0.0) < 360.0
            assert restored.recorded_at.tzinfo is not None

    def test_rows_written_from_a_sample_are_labelled_simulated(
        self, motorway: RouteEnvironment
    ) -> None:
        """ADR 004's honesty invariant, at the exact place the database row is built.

        ``TelemetryEvent`` itself has no origin field — it is the shape of the table — so the
        marker is stamped by ``telemetry_row``, which both the direct sink and the Kafka consumer
        go through. If it were ever anything but ``simulated``, the UI's SIMULIERT badge would be
        lying about real-looking data.
        """
        vehicle = build_vehicle(motorway, soc_percent=60.0)
        for step in drive(vehicle, motorway, ticks=30):
            assert telemetry_row(step.sample.event)["data_origin"] is DataOrigin.simulated

    def test_trip_id_is_present_while_driving_and_absent_while_idle(self) -> None:
        """``telemetry.trip_id`` is the join key for trip aggregates; an idle car has no trip."""
        environment = build_environment(length_km=100.0)
        vehicle = build_vehicle(environment, soc_percent=80.0)

        idle_step = vehicle.advance(
            start_time=SIMULATION_START, duration_s=10.0, physics_step_s=1.0
        )
        assert idle_step.sample.event.trip_id is None
        assert idle_step.sample.event.state is VehicleState.idle

        driving = drive(vehicle, environment, ticks=5)[-1]
        assert driving.sample.event.trip_id is not None
        assert driving.sample.event.state is VehicleState.driving

    def test_interval_statistics_sum_the_physics_steps(self, motorway: RouteEnvironment) -> None:
        """The ML feature vector needs distributional quantities the snapshot cannot carry.

        Ten one-second steps per sample, and the accumulator must hold sums (not means) so that
        windows of different lengths combine by addition in the training-set builder.
        """
        vehicle = build_vehicle(motorway, soc_percent=80.0)
        step = drive(vehicle, motorway, ticks=3, tick_seconds=10.0, physics_step_s=1.0)[-1]
        interval = step.sample.interval

        assert interval.steps == 10
        assert interval.duration_s == pytest.approx(10.0)
        assert interval.driving_steps == 10
        assert interval.speed_sum_kmh >= 0.0
        assert sum(count for _, count in interval.road_class_steps) == interval.steps

    def test_a_sub_step_tick_still_emits_one_sample(self) -> None:
        """A tick shorter than the physics step is integrated as one step, not zero.

        ``round(0.5 / 1.0)`` is 0; without the ``max(1, …)`` the loop would not run and the
        sample would describe a vehicle that never moved.
        """
        environment = build_environment(length_km=100.0)
        vehicle = build_vehicle(environment, soc_percent=80.0)
        vehicle.begin_trip(environment, SIMULATION_START)
        step = vehicle.advance(start_time=SIMULATION_START, duration_s=0.5, physics_step_s=1.0)
        assert step.sample.interval.steps == 1
        assert step.sample.interval.duration_s == pytest.approx(0.5)
        assert step.sample.event.recorded_at == SIMULATION_START + timedelta(seconds=0.5)


class TestShortCorridor:
    """A corridor shorter than one tick's travel is the degenerate case of the offset maths."""

    def test_finishes_and_goes_idle_without_overshooting(self) -> None:
        """450 m: the vehicle arrives, emits exactly one ``finished`` event, and stops there."""
        environment = build_environment(length_km=0.45)
        vehicle = build_vehicle(environment, soc_percent=80.0)
        steps = drive(vehicle, environment, ticks=30)
        events = [event.event_type for step in steps for event in step.trip_events]

        assert events == [TripEventType.finished]
        assert vehicle.state is VehicleState.idle
        assert vehicle.offset_m == pytest.approx(environment.route.length_m)
        assert vehicle.odometer_m <= environment.route.length_m + 1.0


class TestDriverProfileDistribution:
    """No seed may produce a driver who is not a person."""

    @pytest.mark.parametrize("index", range(25))
    def test_every_drawn_profile_is_inside_the_stated_bounds(self, index: int) -> None:
        """The clamps in ``sample_driver_profile`` are the model's claim about plausibility."""
        profile: DriverProfile = sample_driver_profile(20_260_214, f"ATW-{index:04d}")

        assert 0.80 <= profile.aggressiveness <= 1.20
        assert 0.95 <= profile.limit_compliance <= 1.18
        assert 3.0 <= profile.reaction_tau_s <= 12.0
        assert 0.6 <= profile.comfort_acceleration_ms2 <= 2.2
        assert 0.9 <= profile.comfort_deceleration_ms2 <= 3.0
        assert 8.0 <= profile.reserve_soc_percent <= 30.0
        assert 65.0 <= profile.departure_soc_percent <= 92.0
        # A driver must not want to leave with less charge than they would stop at, or the
        # vehicle would unplug straight back into a charging search.
        assert profile.departure_soc_percent > profile.reserve_soc_percent

    def test_profiles_are_reproducible_and_vehicle_specific(self) -> None:
        """Same key, same driver; different vehicle, different driver."""
        assert sample_driver_profile(42, "ATW-0001") == sample_driver_profile(42, "ATW-0001")
        assert sample_driver_profile(42, "ATW-0001") != sample_driver_profile(42, "ATW-0002")
        assert sample_driver_profile(42, "ATW-0001") != sample_driver_profile(43, "ATW-0001")


class TestEstimatedRange:
    """The dashboard number, and the division that could go wrong in it."""

    def test_is_zero_at_an_empty_pack_and_never_divides_by_zero(self) -> None:
        """A flat battery has no range; a consumption of zero must not produce infinity.

        The rolling average is floored at 1 kWh/100 km before it becomes a divisor, which is the
        guard this asserts — a vehicle coasting downhill at 0 kWh/100 km would otherwise report
        an infinite range.
        """
        environment = build_environment()
        empty = build_vehicle(environment, soc_percent=0.0)
        assert empty.estimated_range_km == 0.0
        assert math.isfinite(build_vehicle(environment, soc_percent=100.0).estimated_range_km)

    def test_starts_from_the_nominal_consumption(self) -> None:
        """Before a metre is driven the estimate is capacity ÷ nominal consumption.

        74 kWh usable at 16.5 kWh/100 km is 448 km, which is the figure a sedan of this class
        advertises — the sanity check a reader can do in their head.
        """
        vehicle = build_vehicle(build_environment(), soc_percent=100.0)
        expected_km = 74.0 / 16.5 * 100.0
        assert vehicle.estimated_range_km == pytest.approx(expected_km, rel=1e-9)
        assert 400.0 < vehicle.estimated_range_km < 500.0
