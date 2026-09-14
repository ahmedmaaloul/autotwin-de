"""Physical baseline energy model — the longitudinal road-load model of BUILD_SPEC §10.1.

This module is the honest reference the whole project is measured against. The ML regressor
of ``autotwin_ml.training`` has to beat *this* on the same held-out test set, and if it does
not, the report says so. That only works if the baseline is transparent enough that a reader
can recompute any number in it on paper::

    F_roll  = c_rr · m · g · cos(theta)
    F_aero  = 0.5 · rho(T) · c_d · A · v_air²          rho = 1.225 · 288.15 / (273.15 + T)
    F_grade = m · g · sin(theta)
    F_inert = m · a · 1.05
    P_wheel = (F_roll + F_aero + F_grade + F_inert) · v                        [W]
    P_batt  = P_wheel / 0.90 · k_cold          if P_wheel >= 0
            = max(P_wheel · 0.65 / k_cold, -50 kW)  otherwise
    P_aux   = 0.35 kW + HVAC(T)
    E_kwh   = (P_batt + P_aux) · t / 3.6e6

**Deliberately kept free of the ML stack.** Nothing here imports LightGBM, SHAP, NumPy or
polars, so the simulator can call it on every tick and the API can import it at start-up
without pulling a 200 MB dependency tree into the request path. The heavier modules
(``features``, ``training``, ``inference``) import *this*, never the other way round.

See ``docs/ml/energy-model.md`` for the validity envelope and the explicit statement that this
is an educational engineering approximation, not an OEM battery model.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from autotwin_contracts.vehicles import VehicleProfile
from autotwin_core.errors import ValidationError
from autotwin_ml.constants import (
    DEFAULT_PHYSICS,
    JOULES_PER_KWH,
    SECONDS_PER_HOUR,
    WATTS_PER_KW,
    PhysicsConstants,
)
from autotwin_ml.types import EnergyResult, SegmentConditions

__all__ = [
    "PhysicalEnergyModel",
    "headwind_component_ms",
]

_PERCENT: Final[float] = 100.0
"""Gradients arrive in percent; ``theta = atan(gradient_percent / 100)``."""


def headwind_component_ms(
    wind_speed_ms: float,
    wind_direction_deg: float,
    heading_deg: float,
) -> float:
    """Component of the wind along the direction of travel, in m/s; positive is a headwind.

    ``wind_direction_deg`` follows the meteorological convention used by DWD: the compass
    bearing the wind blows **from**. ``heading_deg`` is the compass bearing the vehicle travels
    **towards**, as :func:`autotwin_core.geo.distance.bearing_deg` returns it. When the two are
    equal the vehicle drives straight into the wind, and the full wind speed is a headwind.

    Provided as a free function rather than folded into :class:`SegmentConditions` because DWD's
    10-minute observation feed that AutoTwin ingests carries the scalar speed ``FF_10``; the
    direction ``DD_10`` is only available for part of the network. A caller that has both can
    resolve a real headwind here; a caller that has only the speed must leave
    :attr:`SegmentConditions.headwind_ms` at zero rather than guess a direction.
    """
    return wind_speed_ms * math.cos(math.radians(wind_direction_deg - heading_deg))


@dataclass(frozen=True, slots=True)
class _WheelPower:
    """The four road-load terms as powers at the wheel, in watts.

    Powers rather than forces: the four terms all share the factor ``v``, and carrying them as
    powers means the decomposition never has to re-multiply and risk a mismatch with the total.
    """

    rolling_w: float
    aero_w: float
    gradient_w: float
    inertia_w: float

    @property
    def total_w(self) -> float:
        """``P_wheel`` — the sum the drivetrain efficiency is applied to."""
        return self.rolling_w + self.aero_w + self.gradient_w + self.inertia_w


class PhysicalEnergyModel:
    """Steady-state longitudinal energy model for a battery-electric vehicle.

    Stateless and therefore safe to share: one instance can serve every request and every
    simulated vehicle. The only state is the frozen :class:`~autotwin_ml.constants.
    PhysicsConstants` set, which exists as a constructor argument so that sensitivity studies
    and the counterfactual attribution in :mod:`autotwin_ml.insights` can vary a constant
    without monkey-patching a module.
    """

    __slots__ = ("_constants",)

    def __init__(self, constants: PhysicsConstants = DEFAULT_PHYSICS) -> None:
        """Bind the constant set this model evaluates with."""
        self._constants = constants

    @property
    def constants(self) -> PhysicsConstants:
        """The constant set in use — needed by callers that report their own assumptions."""
        return self._constants

    # -- force balance ------------------------------------------------------------------

    def _wheel_power(self, profile: VehicleProfile, conditions: SegmentConditions) -> _WheelPower:
        """Evaluate the four road-load terms at the wheel, in watts.

        The gradient enters as ``theta = atan(gradient_percent / 100)`` rather than through the
        small-angle shortcut ``sin(theta) ~= gradient/100``: the shortcut is accurate to better
        than 0.2 % on any public road, but writing the exact trigonometry costs nothing and
        keeps ``cos(theta)`` in the rolling term consistent with ``sin(theta)`` in the gradient
        term, which is where the shortcut actually starts to bite.
        """
        constants = self._constants
        speed_ms = conditions.speed_ms
        mass_kg = profile.mass_kg
        theta_rad = math.atan(conditions.gradient_percent / _PERCENT)

        # F_roll = c_rr · m · g · cos(theta). The cosine is the component of the weight the
        # tyres actually carry; it only matters on steep ground (0.5 % at 6 %, 4 % at 16 %).
        rolling_n = (
            profile.rolling_resistance * mass_kg * constants.gravity_ms2 * math.cos(theta_rad)
        )

        # F_aero = 0.5 · rho(T) · c_d · A · v_air · |v_air|. The signed square keeps a tailwind
        # stronger than the vehicle speed (v_air < 0) pushing rather than braking, instead of
        # flipping into a spurious drag force. With headwind_ms = 0 this is exactly the
        # specification's 0.5 · rho · c_d · A · v².
        air_density = constants.air_density_kg_m3(conditions.outside_temperature_c)
        air_speed_ms = speed_ms + conditions.headwind_ms
        aero_n = 0.5 * air_density * profile.drag_area * air_speed_ms * abs(air_speed_ms)

        # F_grade = m · g · sin(theta); negative downhill, which is what makes regen possible.
        gradient_n = mass_kg * constants.gravity_ms2 * math.sin(theta_rad)

        # F_inert = m · a · 1.05; the 1.05 accounts for the rotating driveline mass.
        inertia_n = mass_kg * conditions.acceleration_ms2 * constants.rotating_mass_factor

        return _WheelPower(
            rolling_w=rolling_n * speed_ms,
            aero_w=aero_n * speed_ms,
            gradient_w=gradient_n * speed_ms,
            inertia_w=inertia_n * speed_ms,
        )

    def battery_power_w(self, wheel_power_w: float, battery_temperature_c: float) -> float:
        """Convert power at the wheel into power at the battery terminals, in watts.

        Traction (``P_wheel >= 0``) pays the drivetrain efficiency and the cold-battery
        internal-resistance surcharge. Recuperation keeps only ``eta_regen`` of the energy and
        is floored at :attr:`~autotwin_ml.constants.PhysicsConstants.regen_power_floor_w`.

        **The cold-battery factor is applied asymmetrically, and that is deliberate.**
        BUILD_SPEC §10.1 says the factor is "applied to ``P_batt``", which it writes with a
        traction case in mind. Taken literally on the recuperation branch it would *multiply* a
        negative number by 1.20 at -10 °C and make a cold pack recover **more** energy than a
        warm one — the opposite of the physics, where a cold cell's higher internal resistance
        and its charge-acceptance limit both cut recuperation hard. The factor is therefore
        applied as a penalty in both directions: multiplied into traction, divided out of
        recuperation. Documented in ``docs/ml/energy-model.md``.
        """
        constants = self._constants
        cold_factor = constants.cold_battery_factor(battery_temperature_c)
        if wheel_power_w >= 0.0:
            return wheel_power_w / constants.drivetrain_efficiency * cold_factor
        recovered_w = wheel_power_w * constants.regen_efficiency / cold_factor
        return max(recovered_w, constants.regen_power_floor_w)

    # -- energy -------------------------------------------------------------------------

    def segment_energy(
        self,
        profile: VehicleProfile,
        conditions: SegmentConditions,
    ) -> EnergyResult:
        """Energy consumed on one segment, with its term-by-term decomposition.

        **How the decomposition is built.** The specification applies the drivetrain efficiency
        to the *sum* of the four forces, not to each one, so the per-term split is an
        attribution on top of the physics and needs a stated rule. The rule is proportional
        allocation: every road-load term is converted to the battery side with the *same*
        multiplier ``k = P_batt / P_wheel`` that the specification applied to their sum. That
        makes the four buckets add up to the traction energy exactly, by construction, and it
        automatically absorbs the -50 kW recuperation floor into ``k``.

        **Guards.**

        * Zero or negative duration → an all-zero result. No time, no energy.
        * ``speed <= 0`` → auxiliary and HVAC only. A vehicle standing in a jam or at a charger
          still runs its control units and its heater, but has no road load and no distance.
        * ``P_wheel == 0`` → the multiplier is undefined and would be a division by zero; all
          four traction buckets are zero, which is the right answer.
        * A segment is never allowed to be a net energy *source*. If the recuperation credit
          exceeds what the segment's dissipative and auxiliary terms consumed, the credit is
          scaled back so the total lands exactly at zero. Physically a long steep descent can of
          course net-charge a real pack; the cap exists because this model has no brake
          blending and no state-of-charge-dependent charge-acceptance limit, and an uncapped
          credit would let a sustained 6 % descent refill the battery faster than any real
          vehicle. The clipping is applied to the negative buckets, so the additive identity of
          :class:`~autotwin_ml.types.EnergyResult` survives it.
        """
        constants = self._constants
        duration_s = conditions.effective_duration_s
        distance_km = conditions.distance_km
        if duration_s <= 0.0:
            return EnergyResult.zero(distance_km=distance_km, duration_s=0.0)

        hours = duration_s / SECONDS_PER_HOUR
        auxiliary_kwh = constants.auxiliary_base_kw * hours
        hvac_kwh = constants.hvac_power_kw(conditions.outside_temperature_c) * hours

        if conditions.speed_ms <= 0.0:
            # Stationary: no road load, no distance travelled, only the standing loads.
            return EnergyResult(
                kwh=auxiliary_kwh + hvac_kwh,
                distance_km=distance_km,
                duration_s=duration_s,
                rolling_kwh=0.0,
                aero_kwh=0.0,
                gradient_kwh=0.0,
                inertia_kwh=0.0,
                auxiliary_kwh=auxiliary_kwh,
                hvac_kwh=hvac_kwh,
                regen_kwh=0.0,
            )

        wheel = self._wheel_power(profile, conditions)
        wheel_total_w = wheel.total_w
        battery_w = self.battery_power_w(
            wheel_total_w,
            conditions.effective_battery_temperature_c,
        )
        # Proportional allocation of the drivetrain conversion across the four terms.
        multiplier = battery_w / wheel_total_w if wheel_total_w != 0.0 else 0.0
        to_kwh = multiplier * duration_s / JOULES_PER_KWH

        rolling_kwh = wheel.rolling_w * to_kwh
        aero_kwh = wheel.aero_w * to_kwh
        gradient_kwh = wheel.gradient_w * to_kwh
        inertia_kwh = wheel.inertia_w * to_kwh

        total_kwh = rolling_kwh + aero_kwh + gradient_kwh + inertia_kwh + auxiliary_kwh + hvac_kwh
        if total_kwh < 0.0:
            rolling_kwh, aero_kwh, gradient_kwh, inertia_kwh = _cap_regen_credit(
                (rolling_kwh, aero_kwh, gradient_kwh, inertia_kwh),
                total_kwh=total_kwh,
            )
            total_kwh = 0.0

        traction_kwh = rolling_kwh + aero_kwh + gradient_kwh + inertia_kwh
        return EnergyResult(
            kwh=total_kwh,
            distance_km=distance_km,
            duration_s=duration_s,
            rolling_kwh=rolling_kwh,
            aero_kwh=aero_kwh,
            gradient_kwh=gradient_kwh,
            inertia_kwh=inertia_kwh,
            auxiliary_kwh=auxiliary_kwh,
            hvac_kwh=hvac_kwh,
            regen_kwh=min(0.0, traction_kwh),
        )

    def consumption_kwh_per_100km(
        self,
        profile: VehicleProfile,
        conditions: SegmentConditions,
    ) -> float:
        """Consumption on one segment in kWh/100 km — the unit every EV driver reads.

        Raises :class:`~autotwin_core.errors.ValidationError` for a zero-length segment: asking
        for a *rate per distance* over no distance is a caller error, unlike asking for the raw
        energy, which is perfectly well defined and available from :meth:`segment_energy`.
        """
        if conditions.distance_km <= 0.0:
            msg = (
                "consumption per 100 km is undefined for a zero-length segment; "
                "use segment_energy() for the raw kWh of a stationary window"
            )
            raise ValidationError(msg, details={"field": "distance_km"})
        return self.segment_energy(profile, conditions).kwh_per_100km

    def trip_energy(
        self,
        profile: VehicleProfile,
        segments: Sequence[SegmentConditions],
    ) -> EnergyResult:
        """Total energy over a sequence of segments, decomposition included.

        The buckets are summed alongside the totals, so a whole corridor decomposes exactly the
        way a single segment does — which is what makes the trip-level explanation of
        BUILD_SPEC §11 possible. An empty sequence totals to zero rather than raising: a route
        with no segments is an empty route, not an error.
        """
        return EnergyResult.sum(self.segment_energy(profile, segment) for segment in segments)

    def instantaneous_power_kw(
        self,
        profile: VehicleProfile,
        conditions: SegmentConditions,
    ) -> float:
        """Battery power in kW at this instant — ``telemetry.instantaneous_power_kw``.

        Positive while drawing from the pack, negative while recuperating. Unlike
        :meth:`segment_energy` this is *not* floored at zero: an instantaneous negative power
        is exactly what a live recuperation readout should show. The non-negativity rule is a
        property of an integrated segment, not of a sample.
        """
        auxiliary_kw = self._constants.auxiliary_power_kw(conditions.outside_temperature_c)
        if conditions.speed_ms <= 0.0:
            return auxiliary_kw
        wheel_total_w = self._wheel_power(profile, conditions).total_w
        battery_w = self.battery_power_w(
            wheel_total_w,
            conditions.effective_battery_temperature_c,
        )
        return battery_w / WATTS_PER_KW + auxiliary_kw


def _cap_regen_credit(
    buckets: tuple[float, float, float, float],
    *,
    total_kwh: float,
) -> tuple[float, float, float, float]:
    """Shrink the negative buckets so the segment total lands exactly at zero.

    Called only when the uncapped total is negative. Solving ``positive + s · negative = 0``
    for the scale ``s`` gives ``s = (negative - total) / negative``, which is guaranteed to lie
    in ``(0, 1)`` because ``total >= negative`` whenever the positive buckets are non-negative.
    Scaling rather than truncating keeps the additive identity of
    :class:`~autotwin_ml.types.EnergyResult` intact and keeps the relative weight of the
    gradient and inertia credits, which the insight generator reads.
    """
    negative_sum = sum(value for value in buckets if value < 0.0)
    if negative_sum >= 0.0:
        # Unreachable while the auxiliary buckets are non-negative; guarded so a future change
        # to the auxiliary model cannot turn this into a division by zero.
        return buckets
    scale = (negative_sum - total_kwh) / negative_sum
    scaled = tuple(value * scale if value < 0.0 else value for value in buckets)
    return (scaled[0], scaled[1], scaled[2], scaled[3])
