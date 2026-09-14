"""Physical baseline energy model — BUILD_SPEC §10.1.

Every expected number in this file is derived from the road-load equations on paper, from a
published class-level EV figure, or from an obviously-correct alternative computation. None of
them was read off the implementation. The derivation is written next to the assertion so a
reviewer can redo it with a calculator::

    F_roll  = c_rr · m · g · cos(theta)
    F_aero  = 0.5 · rho(T) · c_d·A · v²        rho(T) = 1.225 · 288.15 / (273.15 + T)
    F_grade = m · g · sin(theta)
    P_wheel = (F_roll + F_aero + F_grade + F_inert) · v
    P_batt  = P_wheel / 0.90 · k_cold          (traction)

The vehicle used throughout is ``sedan_ev`` (BUILD_SPEC §9): m = 2100 kg, c_rr = 0.010,
c_d·A = 0.23 · 2.30 = 0.529 m², 74 kWh usable, 16.5 kWh/100 km nominal.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from autotwin_contracts.vehicles import VehicleProfile, get_vehicle_profile
from autotwin_core.errors import ValidationError
from autotwin_ml.baseline import PhysicalEnergyModel, headwind_component_ms
from autotwin_ml.constants import DEFAULT_PHYSICS, PhysicsConstants, piecewise_linear
from autotwin_ml.types import EnergyResult, SegmentConditions

GRAVITY = 9.80665
ETA_DRIVE = 0.90
ETA_REGEN = 0.65


@pytest.fixture(scope="module")
def model() -> PhysicalEnergyModel:
    """The model under test. Stateless, so one instance serves the whole module."""
    return PhysicalEnergyModel()


@pytest.fixture(scope="module")
def sedan() -> VehicleProfile:
    """``sedan_ev`` — the reference vehicle of every hand computation below."""
    return get_vehicle_profile("sedan_ev")


def aero_power_kw(
    model: PhysicalEnergyModel, profile: VehicleProfile, conditions: SegmentConditions
) -> float:
    """Mean battery-side power of the aerodynamic bucket alone, in kW.

    Isolating a *power* from the energy decomposition is what makes the v³ law testable: the
    energy bucket over a fixed distance only grows with v², because a faster segment lasts
    proportionally less time.
    """
    result = model.segment_energy(profile, conditions)
    assert result.duration_s > 0.0
    return result.aero_kwh * 3600.0 / result.duration_s


class TestAerodynamicScaling:
    """Drag force grows with v², so drag *power* grows with v³.

    This is the single most important assertion in the file. If the exponent is wrong the whole
    model is wrong — a motorway route would be mispriced by tens of percent — and nothing else
    in the suite would notice, because every other term is linear or constant in speed.
    """

    def test_aero_power_scales_with_the_cube_of_speed(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        # (120/60)³ = 8. Everything else is held constant: same distance, same 20 °C (so the
        # air density and the cold-battery factor are identical), flat, no wind. If this ratio
        # came out near 4 the code would be integrating force instead of power; near 2 it would
        # have lost the square altogether.
        fast = aero_power_kw(model, sedan, SegmentConditions(speed_kmh=120.0, distance_km=10.0))
        slow = aero_power_kw(model, sedan, SegmentConditions(speed_kmh=60.0, distance_km=10.0))
        assert fast / slow == pytest.approx(8.0, rel=0.02)

    @pytest.mark.parametrize(("low_kmh", "high_kmh"), [(50.0, 100.0), (60.0, 90.0), (80.0, 130.0)])
    def test_cube_law_holds_across_the_speed_range(
        self,
        model: PhysicalEnergyModel,
        sedan: VehicleProfile,
        low_kmh: float,
        high_kmh: float,
    ) -> None:
        """The exponent is 3 everywhere, not only at the 60/120 pair."""
        ratio = aero_power_kw(
            model, sedan, SegmentConditions(speed_kmh=high_kmh, distance_km=10.0)
        ) / aero_power_kw(model, sedan, SegmentConditions(speed_kmh=low_kmh, distance_km=10.0))
        assert ratio == pytest.approx((high_kmh / low_kmh) ** 3, rel=0.02)

    def test_aero_power_matches_the_force_balance_on_paper(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        # rho(20 °C) = 1.225 · 288.15/293.15 = 1.20411 kg/m³
        # v = 120/3.6 = 33.333 m/s
        # F = 0.5 · 1.20411 · 0.529 · 33.333² = 353.87 N
        # P_wheel = 353.87 · 33.333 = 11 796 W  →  P_batt = /0.90 = 13.11 kW
        rho = 1.225 * 288.15 / 293.15
        speed_ms = 120.0 / 3.6
        expected_kw = 0.5 * rho * sedan.drag_area * speed_ms**2 * speed_ms / ETA_DRIVE / 1000.0
        measured = aero_power_kw(
            model, sedan, SegmentConditions(speed_kmh=120.0, distance_km=100.0)
        )
        assert measured == pytest.approx(expected_kw, rel=1e-6)

    def test_cold_air_is_denser_and_costs_more_drag(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """Isolating the ideal-gas density correction from every other temperature effect.

        The pack temperature is pinned at 20 °C in both runs, so the cold-battery factor is 1.0
        in both and the only difference left in the aero bucket is ``rho``. The expected ratio
        is then exactly ``(288.15/263.15)/(288.15/293.15) = 293.15/263.15 = 1.1140`` — about
        +11 % drag at -10 °C, which is real but far smaller than the HVAC penalty and must not
        be confused with it.
        """
        cold = model.segment_energy(
            sedan,
            SegmentConditions(
                speed_kmh=120.0,
                distance_km=10.0,
                outside_temperature_c=-10.0,
                battery_temperature_c=20.0,
            ),
        )
        warm = model.segment_energy(
            sedan,
            SegmentConditions(
                speed_kmh=120.0,
                distance_km=10.0,
                outside_temperature_c=20.0,
                battery_temperature_c=20.0,
            ),
        )
        assert cold.aero_kwh / warm.aero_kwh == pytest.approx(293.15 / 263.15, rel=1e-9)
        # Rolling resistance has no temperature term at all; if this drifted, a density factor
        # would have leaked into the wrong bucket.
        assert cold.rolling_kwh == pytest.approx(warm.rolling_kwh, rel=1e-12)


class TestRollingResistance:
    """``F_roll = c_rr · m · g · cos(theta)`` — linear in mass, independent of speed."""

    def test_rolling_energy_per_km_is_independent_of_speed(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        # The force does not depend on v, so the work over a fixed distance does not either.
        # A speed dependence here would mean the rolling term had picked up a v² factor from
        # the neighbouring aero line.
        energies = [
            model.segment_energy(
                sedan, SegmentConditions(speed_kmh=speed, distance_km=10.0)
            ).rolling_kwh
            for speed in (30.0, 60.0, 90.0, 120.0, 150.0)
        ]
        assert energies == pytest.approx([energies[0]] * len(energies), rel=1e-12)

    def test_rolling_energy_matches_the_hand_computation(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        # E = c_rr · m · g · d / eta_drive
        #   = 0.010 · 2100 · 9.80665 · 10 000 m / 0.90 = 2.288e6 J = 0.6356 kWh
        expected_kwh = 0.010 * 2100.0 * GRAVITY * 10_000.0 / ETA_DRIVE / 3.6e6
        measured = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=100.0, distance_km=10.0)
        ).rolling_kwh
        assert measured == pytest.approx(expected_kwh, rel=1e-9)
        assert expected_kwh == pytest.approx(0.6356, abs=1e-4)

    def test_rolling_energy_is_linear_in_mass(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """Doubling the kerb mass doubles the rolling work, exactly."""
        heavy = sedan.model_copy(update={"mass_kg": sedan.mass_kg * 2.0})
        conditions = SegmentConditions(speed_kmh=100.0, distance_km=10.0)
        light_kwh = model.segment_energy(sedan, conditions).rolling_kwh
        heavy_kwh = model.segment_energy(heavy, conditions).rolling_kwh
        assert heavy_kwh / light_kwh == pytest.approx(2.0, rel=1e-12)

    def test_cosine_term_shrinks_rolling_resistance_on_a_climb(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """On a 16 % ramp the tyres carry cos(atan(0.16)) = 98.75 % of the weight.

        Small, but it is the half of the trigonometry that is easy to forget: a model that uses
        the exact ``sin`` for the gradient and a flat ``1.0`` for the rolling term double-counts
        the weight on steep ground.
        """
        flat = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=60.0, distance_km=5.0)
        ).rolling_kwh
        steep = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=60.0, distance_km=5.0, gradient_percent=16.0)
        ).rolling_kwh
        assert steep / flat == pytest.approx(math.cos(math.atan(0.16)), rel=1e-9)


class TestGradient:
    """The potential-energy term, and the recuperation that a descent makes possible."""

    def test_two_percent_climb_over_five_km_costs_mgh(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        # A 5 km stretch at +2 % lifts the car 100 m.
        #   E_wheel = m · g · h = 2100 · 9.81 · 100 = 2.060e6 J
        #   E_batt  = E_wheel / 0.90 = 2.289e6 J = 0.6358 kWh
        # The model uses theta = atan(0.02) and therefore lifts 99.98 m rather than 100 m, so
        # the agreement is to 0.06 % — well inside the tolerance and itself a check that the
        # exact trigonometry was used rather than the small-angle shortcut.
        expected_kwh = 2100.0 * 9.81 * 100.0 / ETA_DRIVE / 3.6e6
        measured = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=100.0, distance_km=5.0, gradient_percent=2.0)
        ).gradient_kwh
        assert measured == pytest.approx(expected_kwh, rel=0.005)

    def test_gradient_energy_is_proportional_to_height_gained(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """Twice the grade over the same distance lifts twice as high and costs twice as much."""
        two = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=80.0, distance_km=5.0, gradient_percent=2.0)
        ).gradient_kwh
        four = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=80.0, distance_km=5.0, gradient_percent=4.0)
        ).gradient_kwh
        # sin(atan(0.04))/sin(atan(0.02)) = 1.9988, not exactly 2 — the difference is the
        # curvature of the tangent, and reproducing it is evidence the angle is exact.
        expected = math.sin(math.atan(0.04)) / math.sin(math.atan(0.02))
        assert four / two == pytest.approx(expected, rel=1e-9)

    @pytest.mark.parametrize("speed_kmh", [30.0, 50.0, 70.0, 90.0, 110.0])
    def test_descent_gives_a_gradient_credit_and_never_a_negative_total(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile, speed_kmh: float
    ) -> None:
        """A -2 % grade is always a credit in the gradient bucket, at every speed…

        …but the segment total is floored at zero: this model has no brake blending and no
        charge-acceptance limit, so an uncapped credit would let a descent refill the pack
        faster than any real vehicle (BUILD_SPEC §10.1, and the cap is documented in
        ``segment_energy``).
        """
        result = model.segment_energy(
            sedan,
            SegmentConditions(speed_kmh=speed_kmh, distance_km=5.0, gradient_percent=-2.0),
        )
        assert result.gradient_kwh < 0.0
        assert result.kwh >= 0.0

    def test_descent_at_moderate_speed_actually_recuperates(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """At 50 km/h on -2 % the grade beats rolling plus drag, so the battery gains energy.

        Hand check of the force balance at 13.89 m/s:
          F_roll  = +205.9 N,  F_aero = +60.9 N,  F_grade = -411.8 N  →  -145 N, i.e. downhill.
        A model that reported ``regen_kwh == 0`` here would be silently discarding every
        recuperation opportunity on a German Mittelgebirge descent.
        """
        result = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=50.0, distance_km=5.0, gradient_percent=-2.0)
        )
        assert result.regen_kwh < 0.0
        assert result.kwh >= 0.0

    def test_symmetric_climb_and_descent_cost_more_than_flat(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """Up-and-down is never free: 90 % out, 65 % back, and the total is capped at zero.

        Climbing 100 m costs ``mgh/0.90`` and descending it returns at most ``mgh·0.65``, so a
        hill that ends where it started still costs energy. If this ever came out equal, the
        drivetrain and regen efficiencies would have been applied symmetrically.
        """
        uphill = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=80.0, distance_km=5.0, gradient_percent=4.0)
        )
        downhill = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=80.0, distance_km=5.0, gradient_percent=-4.0)
        )
        flat = model.segment_energy(sedan, SegmentConditions(speed_kmh=80.0, distance_km=5.0))
        assert uphill.kwh + downhill.kwh > 2.0 * flat.kwh

    def test_steep_sustained_descent_lands_exactly_at_zero(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """The cap is exact, and the decomposition survives it.

        ``_cap_regen_credit`` scales the negative buckets rather than truncating them, so the
        six buckets must still sum to the (zero) total. A truncating implementation would leave
        the chart on the route page adding up to something other than what it displays.
        """
        result = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=100.0, distance_km=5.0, gradient_percent=-6.0)
        )
        assert result.kwh == pytest.approx(0.0, abs=1e-12)
        assert result.components_kwh == pytest.approx(0.0, abs=1e-9)
        assert result.gradient_kwh < 0.0

    def test_recuperation_is_floored_at_fifty_kilowatts(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """A -20 % descent at 130 km/h would deliver ~80 kW to the pack; the model allows 50.

        ``instantaneous_power_kw`` is the readout that is *not* floored at zero, so it is where
        the -50 kW battery limit is observable. The auxiliary load is added on top, which is why
        the answer sits just above -50 kW rather than exactly on it.
        """
        power_kw = model.instantaneous_power_kw(
            sedan, SegmentConditions(speed_kmh=130.0, distance_km=5.0, gradient_percent=-20.0)
        )
        aux_kw = DEFAULT_PHYSICS.auxiliary_power_kw(20.0)
        assert power_kw == pytest.approx(-50.0 + aux_kw, rel=1e-9)


class TestTemperature:
    """Consumption is U-shaped in ambient temperature with its minimum at the HVAC zero."""

    @pytest.mark.parametrize("speed_kmh", [30.0, 80.0, 130.0])
    def test_both_extremes_cost_more_than_twenty_degrees(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile, speed_kmh: float
    ) -> None:
        """-10 °C and +35 °C both exceed the 20 °C minimum, at every speed.

        20 °C is where BUILD_SPEC §10.1 puts the HVAC load at zero *and* where the cold-battery
        factor is 1.0, so it is the bottom of the curve by construction. Heating at -10 °C
        (3.5 kW) costs far more than cooling at +35 °C (2.0 kW), which is the asymmetry every
        German EV driver knows.
        """

        def consumption(temperature_c: float) -> float:
            return model.consumption_kwh_per_100km(
                sedan,
                SegmentConditions(
                    speed_kmh=speed_kmh, distance_km=50.0, outside_temperature_c=temperature_c
                ),
            )

        mild = consumption(20.0)
        assert consumption(-10.0) > mild
        assert consumption(35.0) > mild
        assert consumption(-10.0) > consumption(35.0)

    @pytest.mark.parametrize(
        "temperature_c", [-20.0, -10.0, 0.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 45.0]
    )
    def test_twenty_degrees_is_the_global_minimum(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile, temperature_c: float
    ) -> None:
        """No sampled temperature undercuts the neutral point."""
        conditions = SegmentConditions(
            speed_kmh=100.0, distance_km=50.0, outside_temperature_c=temperature_c
        )
        neutral = SegmentConditions(speed_kmh=100.0, distance_km=50.0, outside_temperature_c=20.0)
        assert (
            model.consumption_kwh_per_100km(sedan, conditions)
            >= model.consumption_kwh_per_100km(sedan, neutral) - 1e-12
        )

    def test_cold_penalty_is_proportionally_larger_at_low_speed(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """The model's most characteristic behaviour, and the one a regression would invert.

        HVAC is a roughly fixed *power* draw, so its cost *per kilometre* is inversely
        proportional to speed: 3.5 kW spread over 30 km/h is 11.7 kWh/100 km, over 130 km/h only
        2.7 kWh/100 km — while the denominator (the baseline consumption) moves the other way.
        The relative winter penalty is therefore several times larger in city traffic than on
        the Autobahn, which is exactly why a winter range estimate that scales a single
        percentage penalty is wrong.
        """

        def penalty(speed_kmh: float) -> float:
            mild = model.consumption_kwh_per_100km(
                sedan,
                SegmentConditions(
                    speed_kmh=speed_kmh, distance_km=50.0, outside_temperature_c=20.0
                ),
            )
            cold = model.consumption_kwh_per_100km(
                sedan,
                SegmentConditions(
                    speed_kmh=speed_kmh, distance_km=50.0, outside_temperature_c=-10.0
                ),
            )
            return cold / mild - 1.0

        slow_penalty = penalty(30.0)
        fast_penalty = penalty(130.0)
        assert slow_penalty > fast_penalty
        # The gap is not marginal: at 30 km/h the trip costs roughly 2.5x more extra than at
        # 130 km/h, because the fixed heater load dominates a slow kilometre.
        assert slow_penalty > 2.0 * fast_penalty

    def test_hvac_energy_equals_the_documented_curve_times_the_duration(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        # BUILD_SPEC §10.1 anchors the curve at 3.5 kW for -10 °C. 100 km at 120 km/h takes
        # 3000 s = 0.8333 h, so the HVAC bucket must hold 3.5 · 0.8333 = 2.9167 kWh.
        result = model.segment_energy(
            sedan,
            SegmentConditions(speed_kmh=120.0, distance_km=100.0, outside_temperature_c=-10.0),
        )
        assert result.hvac_kwh == pytest.approx(3.5 * 3000.0 / 3600.0, rel=1e-9)
        assert result.auxiliary_kwh == pytest.approx(0.35 * 3000.0 / 3600.0, rel=1e-9)

    def test_cold_pack_multiplies_only_the_traction_terms(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """``k_cold = 1 + max(0, 15 - T_batt)·0.008`` — at -15 °C that is exactly 1.24.

        The ambient is held at 20 °C so the HVAC bucket stays zero and the air density is
        unchanged; the only thing that may move is the battery-side traction loss.
        """
        warm = model.segment_energy(
            sedan,
            SegmentConditions(
                speed_kmh=100.0,
                distance_km=10.0,
                outside_temperature_c=20.0,
                battery_temperature_c=20.0,
            ),
        )
        cold = model.segment_energy(
            sedan,
            SegmentConditions(
                speed_kmh=100.0,
                distance_km=10.0,
                outside_temperature_c=20.0,
                battery_temperature_c=-15.0,
            ),
        )
        expected_factor = 1.0 + (15.0 - -15.0) * 0.008
        assert expected_factor == pytest.approx(1.24)
        assert cold.traction_kwh / warm.traction_kwh == pytest.approx(expected_factor, rel=1e-9)
        assert cold.auxiliary_kwh == pytest.approx(warm.auxiliary_kwh, rel=1e-12)

    def test_cold_pack_reduces_rather_than_increases_recuperation(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """The asymmetry documented in ``battery_power_w``.

        Read literally, "multiply P_batt by k_cold" would make a cold pack *recover more* on a
        descent, which is backwards: higher internal resistance and a lower charge-acceptance
        limit both cut recuperation. The credit must therefore shrink, not grow.
        """
        wheel_w = -20_000.0
        warm_w = model.battery_power_w(wheel_w, 20.0)
        cold_w = model.battery_power_w(wheel_w, -15.0)
        assert warm_w == pytest.approx(wheel_w * ETA_REGEN, rel=1e-12)
        assert cold_w > warm_w  # less negative == less energy recovered
        assert cold_w == pytest.approx(wheel_w * ETA_REGEN / 1.24, rel=1e-9)


class TestKnownValueAnchor:
    """One absolute number the whole model can be sanity-checked against."""

    def test_sedan_at_a_steady_120_lands_in_the_published_range(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """A 2.1 t, c_d·A = 0.53 m² sedan EV cruising at 120 km/h in mild weather.

        Independent reference: German long-term tests and the ADAC Ecotest routinely report
        18-21 kWh/100 km for this class at a steady 120 km/h, and the vehicle's own WLTP-style
        nominal figure is 16.5 kWh/100 km — a constant 120 must sit *above* nominal, because
        WLTP averages well below motorway speed. 17-22 kWh/100 km is therefore the band a
        credible model has to land in; outside it, either the drag area or the unit conversion
        is wrong.
        """
        consumption = model.consumption_kwh_per_100km(
            sedan,
            SegmentConditions(speed_kmh=120.0, distance_km=100.0, outside_temperature_c=20.0),
        )
        assert 17.0 <= consumption <= 22.0
        assert consumption > sedan.nominal_consumption_kwh_100km

    def test_the_anchor_reconstructs_from_its_three_terms(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        # rolling 6.356 + aero 10.922 + auxiliary 0.292 = 17.570 kWh over 100 km, computed
        # term by term from the force balance with no reference to the implementation.
        rho = 1.225 * 288.15 / 293.15
        speed_ms = 120.0 / 3.6
        duration_s = 100_000.0 / speed_ms
        rolling_kwh = 0.010 * 2100.0 * GRAVITY * 100_000.0 / ETA_DRIVE / 3.6e6
        aero_n = 0.5 * rho * sedan.drag_area * speed_ms**2
        aero_kwh = aero_n * speed_ms * duration_s / ETA_DRIVE / 3.6e6
        auxiliary_kwh = 0.35 * duration_s / 3600.0
        expected = rolling_kwh + aero_kwh + auxiliary_kwh
        assert expected == pytest.approx(17.57, abs=0.01)
        assert model.consumption_kwh_per_100km(
            sedan, SegmentConditions(speed_kmh=120.0, distance_km=100.0)
        ) == pytest.approx(expected, rel=1e-9)

    @pytest.mark.parametrize(
        "code", ["compact_ev", "sedan_ev", "performance_ev", "suv_ev", "van_ev"]
    )
    def test_every_generic_profile_is_plausible_at_motorway_speed(
        self, model: PhysicalEnergyModel, code: str
    ) -> None:
        """No profile in BUILD_SPEC §9 produces an absurd figure at a constant 120 km/h.

        A generous band (14-35 kWh/100 km) — the point is to catch a profile whose drag area or
        mass was mistyped by an order of magnitude, not to pin a value.
        """
        profile = get_vehicle_profile(code)
        consumption = model.consumption_kwh_per_100km(
            profile, SegmentConditions(speed_kmh=120.0, distance_km=100.0)
        )
        assert 14.0 <= consumption <= 35.0

    def test_heavier_and_draggier_profiles_consume_more(self, model: PhysicalEnergyModel) -> None:
        """A van must not be cheaper to drive at 120 km/h than a sedan."""
        conditions = SegmentConditions(speed_kmh=120.0, distance_km=100.0)
        sedan_kwh = model.consumption_kwh_per_100km(get_vehicle_profile("sedan_ev"), conditions)
        suv_kwh = model.consumption_kwh_per_100km(get_vehicle_profile("suv_ev"), conditions)
        van_kwh = model.consumption_kwh_per_100km(get_vehicle_profile("van_ev"), conditions)
        assert sedan_kwh < suv_kwh < van_kwh


class TestDecomposition:
    """The six consumption buckets must reconcile with the total, always."""

    @pytest.mark.parametrize(
        "conditions",
        [
            SegmentConditions(speed_kmh=120.0, distance_km=10.0),
            SegmentConditions(speed_kmh=0.0, distance_km=0.0, duration_s=300.0),
            SegmentConditions(speed_kmh=50.0, distance_km=5.0, gradient_percent=-6.0),
            SegmentConditions(speed_kmh=90.0, distance_km=8.0, gradient_percent=5.5),
            SegmentConditions(speed_kmh=70.0, distance_km=4.0, acceleration_ms2=0.8),
            SegmentConditions(speed_kmh=70.0, distance_km=4.0, acceleration_ms2=-0.8),
            SegmentConditions(speed_kmh=110.0, distance_km=9.0, outside_temperature_c=-18.0),
            SegmentConditions(speed_kmh=110.0, distance_km=9.0, headwind_ms=12.0),
            SegmentConditions(speed_kmh=110.0, distance_km=9.0, headwind_ms=-40.0),
        ],
        ids=[
            "cruise",
            "standstill",
            "steep-descent",
            "climb",
            "accelerating",
            "braking",
            "deep-cold",
            "headwind",
            "strong-tailwind",
        ],
    )
    def test_components_sum_to_the_total(
        self,
        model: PhysicalEnergyModel,
        sedan: VehicleProfile,
        conditions: SegmentConditions,
    ) -> None:
        """A decomposition that silently fails to sum is a trap for every downstream chart."""
        result = model.segment_energy(sedan, conditions)
        assert result.components_kwh == pytest.approx(result.kwh, abs=1e-12)
        assert result.kwh >= 0.0

    def test_regen_is_a_memo_and_not_a_seventh_addend(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """``regen_kwh`` is already inside the gradient/inertia buckets.

        Adding it to the six would double-count the recuperation, so the identity is checked
        *without* it and ``regen_kwh`` is asserted to be non-positive on its own.
        """
        result = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=50.0, distance_km=5.0, gradient_percent=-3.0)
        )
        assert result.regen_kwh <= 0.0
        assert result.components_kwh == pytest.approx(result.kwh, abs=1e-12)

    def test_positive_buckets_never_turn_negative(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """Rolling, auxiliary and HVAC oppose motion or are standing loads; they cannot pay."""
        result = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=60.0, distance_km=5.0, gradient_percent=-6.0)
        )
        assert result.rolling_kwh >= 0.0
        assert result.auxiliary_kwh >= 0.0
        assert result.hvac_kwh >= 0.0

    def test_trip_energy_sums_its_segments_bucket_by_bucket(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        segments = [
            SegmentConditions(speed_kmh=120.0, distance_km=5.0),
            SegmentConditions(speed_kmh=60.0, distance_km=3.0, gradient_percent=3.0),
            SegmentConditions(speed_kmh=0.0, distance_km=0.0, duration_s=120.0),
        ]
        trip = model.trip_energy(sedan, segments)
        parts = [model.segment_energy(sedan, segment) for segment in segments]
        assert trip.kwh == pytest.approx(sum(part.kwh for part in parts), rel=1e-12)
        assert trip.distance_km == pytest.approx(8.0)
        assert trip.duration_s == pytest.approx(sum(part.duration_s for part in parts))
        assert trip.components_kwh == pytest.approx(trip.kwh, abs=1e-12)

    def test_empty_trip_is_zero_not_an_error(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """A route with no segments is an empty route, not a failure."""
        assert model.trip_energy(sedan, []).kwh == 0.0
        assert EnergyResult.sum([]).kwh == 0.0


class TestEdgeCases:
    """Boundaries and failure modes: no time, no distance, no motion, absurd inputs."""

    def test_standstill_consumes_auxiliary_energy_only(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        # 10 minutes stationary at -10 °C: (0.35 + 3.5) kW · 600/3600 h = 0.6417 kWh.
        # The road-load buckets must be exactly zero — there is no motion to resist.
        result = model.segment_energy(
            sedan,
            SegmentConditions(
                speed_kmh=0.0, distance_km=0.0, duration_s=600.0, outside_temperature_c=-10.0
            ),
        )
        assert result.kwh == pytest.approx((0.35 + 3.5) * 600.0 / 3600.0, rel=1e-9)
        assert result.rolling_kwh == 0.0
        assert result.aero_kwh == 0.0
        assert result.gradient_kwh == 0.0
        assert result.inertia_kwh == 0.0

    def test_zero_speed_does_not_divide_by_zero(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """``distance / speed`` is the obvious division to get wrong when the vehicle stops."""
        result = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=0.0, distance_km=0.0, duration_s=60.0)
        )
        assert math.isfinite(result.kwh)
        assert result.kwh_per_100km == 0.0  # no distance to normalise against
        assert model.instantaneous_power_kw(
            sedan, SegmentConditions(speed_kmh=0.0, distance_km=0.0)
        ) == pytest.approx(DEFAULT_PHYSICS.auxiliary_power_kw(20.0))

    def test_stationary_window_without_a_duration_costs_nothing(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """No time, no energy — the only answer that does not invent a number."""
        result = model.segment_energy(sedan, SegmentConditions(speed_kmh=0.0, distance_km=0.0))
        assert result == EnergyResult.zero()

    def test_zero_distance_at_speed_is_a_zero_result(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """distance ÷ speed is zero seconds, so there is no energy to attribute."""
        result = model.segment_energy(sedan, SegmentConditions(speed_kmh=100.0, distance_km=0.0))
        assert result.kwh == 0.0
        assert result.duration_s == 0.0

    def test_consumption_rate_is_refused_for_a_zero_length_segment(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """A rate *per distance* over no distance is a caller error, not a 0.0.

        ``segment_energy`` still answers, because raw kWh over a standstill is well defined.
        """
        with pytest.raises(ValidationError):
            model.consumption_kwh_per_100km(
                sedan, SegmentConditions(speed_kmh=0.0, distance_km=0.0, duration_s=600.0)
            )

    def test_an_absurd_headwind_stays_finite_and_costs_more(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """40 m/s (144 km/h) of headwind — a hurricane, but the model must not blow up.

        Air speed becomes 33.3 + 40 = 73.3 m/s, so the drag term grows by (73.3/33.3)² = 4.8x.
        """
        still = model.consumption_kwh_per_100km(
            sedan, SegmentConditions(speed_kmh=120.0, distance_km=10.0)
        )
        gale = model.consumption_kwh_per_100km(
            sedan, SegmentConditions(speed_kmh=120.0, distance_km=10.0, headwind_ms=40.0)
        )
        assert math.isfinite(gale)
        assert gale > still * 2.0

    def test_a_tailwind_stronger_than_the_vehicle_pushes_instead_of_braking(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """The signed square in the aero term, tested where the sign actually flips.

        At 120 km/h with a 60 m/s tailwind the air moves past the car backwards; a naive ``v²``
        would square the sign away and report a huge *drag* force on a vehicle that is being
        pushed. The aero bucket must be negative and the segment total still non-negative.
        """
        result = model.segment_energy(
            sedan, SegmentConditions(speed_kmh=120.0, distance_km=10.0, headwind_ms=-60.0)
        )
        assert result.aero_kwh < 0.0
        assert result.kwh >= 0.0

    def test_battery_below_freezing_costs_more_but_stays_finite(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """A pack at -25 °C: k_cold = 1 + 40·0.008 = 1.32, and nothing diverges."""
        conditions = SegmentConditions(
            speed_kmh=100.0,
            distance_km=10.0,
            outside_temperature_c=20.0,
            battery_temperature_c=-25.0,
        )
        result = model.segment_energy(sedan, conditions)
        assert math.isfinite(result.kwh)
        assert DEFAULT_PHYSICS.cold_battery_factor(-25.0) == pytest.approx(1.32, rel=1e-12)

    def test_battery_temperature_falls_back_to_ambient(self) -> None:
        """The cold-soak assumption: a car parked overnight has an ambient-temperature pack."""
        conditions = SegmentConditions(speed_kmh=80.0, distance_km=5.0, outside_temperature_c=-6.0)
        assert conditions.effective_battery_temperature_c == -6.0

    def test_warm_pack_gets_no_penalty_at_all(self) -> None:
        """The factor is clamped at 1.0 above 15 °C; a hot pack is not modelled as *better*."""
        assert DEFAULT_PHYSICS.cold_battery_factor(15.0) == 1.0
        assert DEFAULT_PHYSICS.cold_battery_factor(40.0) == 1.0

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"speed_kmh": 100.0, "distance_km": -1.0},
            {"speed_kmh": 100.0, "distance_km": 5.0, "duration_s": -10.0},
            {"speed_kmh": 500.0, "distance_km": 5.0},
            {"speed_kmh": 100.0, "distance_km": 5.0, "gradient_percent": 45.0},
            {"speed_kmh": 100.0, "distance_km": 5.0, "gradient_percent": -45.0},
            {"speed_kmh": 100.0, "distance_km": 5.0, "outside_temperature_c": -999.0},
        ],
        ids=[
            "negative-distance",
            "negative-duration",
            "m/s-as-km/h",
            "up-45%",
            "down-45%",
            "dwd-sentinel",
        ],
    )
    def test_impossible_conditions_are_refused(self, kwargs: dict[str, Any]) -> None:
        """Unit mix-ups and unconverted DWD ``-999`` sentinels must raise, not be modelled.

        Silently producing a plausible-looking number from a -999 °C segment is exactly the
        failure mode BUILD_SPEC §0.4 forbids.
        """
        with pytest.raises(ValidationError):
            SegmentConditions(**kwargs)


class TestHeadwindComponent:
    """``headwind_component_ms`` resolves the DWD scalar wind onto the direction of travel."""

    @pytest.mark.parametrize(
        ("wind_from_deg", "heading_deg", "expected_ms"),
        [
            (0.0, 0.0, 10.0),  # driving north into a north wind: full headwind
            (180.0, 0.0, -10.0),  # driving north with a south wind: full tailwind
            (90.0, 0.0, 0.0),  # pure crosswind contributes nothing along the axis
            (270.0, 0.0, 0.0),
            (45.0, 0.0, 10.0 * math.cos(math.radians(45.0))),
            (0.0, 360.0, 10.0),  # the formula must be periodic in 360°
        ],
    )
    def test_projection_onto_the_heading(
        self, wind_from_deg: float, heading_deg: float, expected_ms: float
    ) -> None:
        """DWD reports the bearing the wind blows *from*; the vehicle heading is where it goes.

        Equal bearings therefore mean driving straight into the wind. Getting this convention
        backwards would turn every headwind into a tailwind and quietly lower every winter
        consumption estimate.
        """
        assert headwind_component_ms(10.0, wind_from_deg, heading_deg) == pytest.approx(
            expected_ms, abs=1e-9
        )

    def test_zero_wind_speed_is_zero_in_every_direction(self) -> None:
        assert headwind_component_ms(0.0, 137.0, 42.0) == 0.0


class TestInstantaneousPower:
    """The live readout — unlike an integrated segment, it may be negative."""

    def test_recuperating_downhill_reports_negative_power(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """The non-negativity rule is a property of an integrated segment, not of a sample."""
        power_kw = model.instantaneous_power_kw(
            sedan, SegmentConditions(speed_kmh=60.0, distance_km=5.0, gradient_percent=-6.0)
        )
        assert power_kw < 0.0

    def test_cruising_power_matches_the_segment_average(
        self, model: PhysicalEnergyModel, sedan: VehicleProfile
    ) -> None:
        """On a steady-state segment the instantaneous and mean powers must coincide.

        Two independent code paths (``instantaneous_power_kw`` and the energy integration)
        computing the same physics; if they diverged, the live telemetry and the route analysis
        would disagree about the same drive.
        """
        conditions = SegmentConditions(speed_kmh=110.0, distance_km=20.0)
        result = model.segment_energy(sedan, conditions)
        assert model.instantaneous_power_kw(sedan, conditions) == pytest.approx(
            result.average_power_kw, rel=1e-9
        )

    def test_average_power_of_a_zero_duration_result_is_zero(self) -> None:
        """No time, no average — and no division by zero."""
        assert EnergyResult.zero().average_power_kw == 0.0


class TestPhysicsConstants:
    """The constant set itself, including the two hand-drawn curves."""

    @pytest.mark.parametrize(
        ("temperature_c", "expected_kw"),
        [(-10.0, 3.5), (20.0, 0.0), (35.0, 2.0)],
    )
    def test_the_three_anchors_named_in_the_specification(
        self, temperature_c: float, expected_kw: float
    ) -> None:
        """BUILD_SPEC §10.1 fixes these three points of the HVAC envelope exactly."""
        assert DEFAULT_PHYSICS.hvac_power_kw(temperature_c) == pytest.approx(expected_kw)

    def test_hvac_curve_is_clamped_rather_than_extrapolated(self) -> None:
        """At -40 °C a linearly extrapolated envelope becomes physically absurd.

        Clamping is the documented choice: the curve was drawn from observations over a bounded
        range and continuing its last slope would invent values the source never supported.
        """
        assert DEFAULT_PHYSICS.hvac_power_kw(-40.0) == DEFAULT_PHYSICS.hvac_power_kw(-20.0)
        assert DEFAULT_PHYSICS.hvac_power_kw(80.0) == DEFAULT_PHYSICS.hvac_power_kw(45.0)

    def test_air_density_follows_the_ideal_gas_law_at_constant_pressure(self) -> None:
        # rho(T) = 1.225 · 288.15 / (273.15 + T); at 15 °C it returns the reference itself.
        assert DEFAULT_PHYSICS.air_density_kg_m3(15.0) == pytest.approx(1.225, rel=1e-12)
        assert DEFAULT_PHYSICS.air_density_kg_m3(0.0) == pytest.approx(
            1.225 * 288.15 / 273.15, rel=1e-12
        )
        # Colder is denser, monotonically.
        densities = [DEFAULT_PHYSICS.air_density_kg_m3(t) for t in (-20.0, 0.0, 20.0, 40.0)]
        assert densities == sorted(densities, reverse=True)

    def test_absolute_zero_cannot_flip_the_density_sign(self) -> None:
        """Guards a corrupted sentinel: below 0 K the formula changes sign."""
        assert DEFAULT_PHYSICS.air_density_kg_m3(-300.0) > 0.0

    def test_piecewise_linear_interpolates_and_clamps(self) -> None:
        curve = ((0.0, 0.0), (10.0, 10.0), (20.0, 30.0))
        assert piecewise_linear(curve, 5.0) == pytest.approx(5.0)
        assert piecewise_linear(curve, 15.0) == pytest.approx(20.0)
        assert piecewise_linear(curve, -5.0) == 0.0  # clamped, not extrapolated to -5
        assert piecewise_linear(curve, 99.0) == 30.0

    def test_piecewise_linear_resolves_a_duplicate_breakpoint_to_the_upper_value(self) -> None:
        """A repeated x expresses a step; the documented resolution is the upper value."""
        assert piecewise_linear(((0.0, 1.0), (5.0, 1.0), (5.0, 2.0), (10.0, 2.0)), 5.0) == 2.0

    def test_piecewise_linear_rejects_an_empty_curve(self) -> None:
        with pytest.raises(ValueError, match="at least one breakpoint"):
            piecewise_linear((), 0.0)

    def test_a_tuned_constant_set_changes_the_answer(self, sedan: VehicleProfile) -> None:
        """Constants are a constructor argument precisely so sensitivity studies need no
        monkey-patching. A 100 % efficient drivetrain must consume exactly 0.90 of what a
        90 % efficient one does, on the traction terms."""
        conditions = SegmentConditions(speed_kmh=100.0, distance_km=10.0)
        default = PhysicalEnergyModel().segment_energy(sedan, conditions)
        ideal = PhysicalEnergyModel(PhysicsConstants(drivetrain_efficiency=1.0)).segment_energy(
            sedan, conditions
        )
        assert ideal.traction_kwh == pytest.approx(default.traction_kwh * ETA_DRIVE, rel=1e-12)
