"""Physical constants of the AutoTwin energy model (BUILD_SPEC §10.1).

Every number the longitudinal road-load model uses lives in :class:`PhysicsConstants`,
including the two non-trivial *curves* (HVAC load over ambient temperature, cold-battery
internal-resistance factor). Scattering them as module-level floats was the obvious
alternative and was rejected for three reasons:

1. **Reproducibility.** A trained model artefact is only meaningful together with the
   constants that produced its training set. One frozen object can be serialised next to the
   artefact and compared; fourteen module globals cannot.
2. **Counterfactuals.** :mod:`autotwin_ml.insights` re-runs the physical model under altered
   conditions to attribute energy. Sensitivity studies ("what if the drivetrain were 93 %
   efficient?") need a second constant set, not a monkey-patched module.
3. **One place to read.** A reviewer asking "where does 0.65 come from?" should find the
   number and its justification in the same line of source.

**Provenance of the values.** These are textbook vehicle-dynamics figures and published
class-level EV observations, not manufacturer data. BUILD_SPEC §10.1 fixes them, and
``docs/ml/energy-model.md`` states the validity envelope and the honest disclaimer: this is an
educational engineering approximation, never an OEM battery model.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "DEFAULT_PHYSICS",
    "HVAC_CURVE_C_KW",
    "JOULES_PER_KWH",
    "KMH_PER_MS",
    "MS_PER_KMH",
    "SECONDS_PER_HOUR",
    "WATTS_PER_KW",
    "PhysicsConstants",
    "piecewise_linear",
]


def piecewise_linear(curve: Sequence[tuple[float, float]], x: float) -> float:
    """Interpolate ``curve`` — a sequence of ``(x, y)`` breakpoints — at ``x``.

    Shared by the HVAC envelope here and by the charging-curve deratings in
    :mod:`autotwin_ml.charging`, because both are hand-drawn engineering envelopes with the
    same two requirements: linear between breakpoints, and **clamped** outside them.

    Clamping rather than extrapolating is the deliberate part. Every curve in this project was
    drawn from published observations over a bounded range; continuing its last slope past the
    final breakpoint would invent values the source never supported, and at -40 °C a linearly
    extrapolated HVAC load or charging derate becomes physically absurd.

    ``curve`` must be non-empty and sorted by ``x`` ascending; duplicate breakpoints express a
    step and resolve to the upper value.
    """
    if not curve:
        msg = "piecewise_linear needs at least one breakpoint"
        raise ValueError(msg)
    xs = [point[0] for point in curve]
    if x <= xs[0]:
        return curve[0][1]
    if x >= xs[-1]:
        return curve[-1][1]
    index = bisect_right(xs, x)
    lower_x, lower_y = curve[index - 1]
    upper_x, upper_y = curve[index]
    span = upper_x - lower_x
    if span <= 0.0:
        return upper_y
    return lower_y + (x - lower_x) / span * (upper_y - lower_y)


# --------------------------------------------------------------------------------------
# Unit conversions. Named constants rather than inline literals because a stray 3.6 in the
# wrong direction is the single most common defect in vehicle-energy code, and a reviewer
# can check a named constant at a glance.
# --------------------------------------------------------------------------------------

MS_PER_KMH: Final[float] = 1.0 / 3.6
"""Multiply km/h by this to obtain m/s."""

KMH_PER_MS: Final[float] = 3.6
"""Multiply m/s by this to obtain km/h."""

SECONDS_PER_HOUR: Final[float] = 3600.0
"""Seconds in an hour — the divisor when turning a kW load into kWh over a duration."""

WATTS_PER_KW: Final[float] = 1000.0
"""Watts in a kilowatt."""

JOULES_PER_KWH: Final[float] = 3.6e6
"""Joules in a kilowatt-hour: ``E_kwh = P_watt * t_seconds / 3.6e6`` (BUILD_SPEC §10.1)."""


# --------------------------------------------------------------------------------------
# HVAC envelope
# --------------------------------------------------------------------------------------

HVAC_CURVE_C_KW: Final[tuple[tuple[float, float], ...]] = (
    (-20.0, 4.5),
    (-10.0, 3.5),
    (0.0, 2.2),
    (10.0, 0.9),
    (20.0, 0.0),
    (25.0, 0.5),
    (30.0, 1.2),
    (35.0, 2.0),
    (45.0, 3.2),
)
"""Cabin climate load in kW as a function of ambient temperature in °C.

The three anchors named in BUILD_SPEC §10.1 are honoured exactly — ``0 kW at +20 °C``,
``3.5 kW at -10 °C``, ``2.0 kW at +35 °C`` — and the remaining breakpoints interpolate between
them along the shape reported for mid-size European EVs: heating is the dominant winter load
(a resistive heater alone draws 4-6 kW at -20 °C, a heat pump roughly half that, so 4.5 kW is
a fleet-average compromise), while air conditioning peaks far lower because the compressor
only has to reject heat across a 10-15 K gradient.

This is **a rough envelope, not a measurement.** It has no humidity term, no cabin
pre-conditioning, no solar-load term, no occupancy, no heat-pump/resistive distinction and no
recirculation state. It exists so that the winter consumption penalty every German EV driver
experiences appears in the model at the right order of magnitude, and it is the single largest
source of modelling error at low temperatures. See ``docs/ml/energy-model.md``.

Values between breakpoints are linearly interpolated; outside the range the curve is clamped
to its end values, because extrapolating a hand-drawn envelope past -20 °C or +45 °C would be
false precision.
"""


@dataclass(frozen=True, slots=True)
class PhysicsConstants:
    """The complete constant set of the physical energy model (BUILD_SPEC §10.1).

    Frozen and slotted: the model reads these on every simulation tick, and a constant set
    mutated mid-run would make a training set unreproducible — the same reasoning that makes
    :class:`~autotwin_contracts.vehicles.VehicleProfile` frozen.
    """

    gravity_ms2: float = 9.80665
    """Standard gravity ``g`` (CGPM 1901 conventional value), used by the rolling-resistance
    and gradient terms."""

    air_density_reference_kg_m3: float = 1.225
    """Density of dry air at the ISA sea-level reference state (15 °C, 1013.25 hPa)."""

    air_density_reference_temperature_c: float = 15.0
    """Reference temperature of :attr:`air_density_reference_kg_m3`, in °C.

    BUILD_SPEC §10.1 writes the correction as ``rho = 1.225 * 288.15 / (273.15 + T)``, i.e. the
    ideal-gas law at constant pressure. 288.15 K is exactly this 15 °C."""

    kelvin_offset_c: float = 273.15
    """Offset between the Celsius and Kelvin scales."""

    drivetrain_efficiency: float = 0.90
    """``eta_drive`` — inverter, motor and single-speed reduction gear combined.

    0.90 is a deliberately flat, load-independent figure. Real drivetrain efficiency is a map
    over torque and speed that peaks near 0.95 and collapses below 0.80 at very low load; the
    ML model is what learns that shape from the simulated windows, while the baseline stays a
    transparent first-order reference anybody can recompute on paper."""

    regen_efficiency: float = 0.65
    """``eta_regen`` — the share of braking energy at the wheel that reaches the battery.

    Lower than :attr:`drivetrain_efficiency` because recuperation pays the drivetrain losses a
    second time (wheel → motor → inverter → cells) and because part of every real deceleration
    is taken by the friction brakes through brake blending."""

    regen_power_floor_w: float = -50_000.0
    """Most negative battery power the model allows, in watts (-50 kW, BUILD_SPEC §10.1).

    Stands in for everything that limits recuperation in a real car: motor peak generator
    torque, inverter current limit, and above all the battery's charge-acceptance power, which
    a management system holds far below the discharge limit. Without this floor a long steep
    descent would "refill" the pack at a physically impossible rate."""

    rotating_mass_factor: float = 1.05
    """Mass factor ``lambda`` for the inertia term.

    Accelerating a car also spins up wheels, half-shafts, the reduction gear and the rotor.
    Their rotational inertia is conventionally folded into the translational equation as an
    equivalent 4-6 % mass surcharge for a single-speed EV driveline; BUILD_SPEC fixes 1.05."""

    auxiliary_base_kw: float = 0.35
    """Temperature-independent auxiliary load in kW.

    The always-on 12 V side: control units, lighting, pumps, the DC/DC converter's own loss,
    the infotainment head unit. Separate from :attr:`hvac_curve_c_kw` because it does not
    vanish at 20 °C — it is the floor a stationary vehicle still draws."""

    cold_battery_reference_temperature_c: float = 15.0
    """Pack temperature at and above which no internal-resistance penalty is applied."""

    cold_battery_resistance_per_kelvin: float = 0.008
    """Extra battery-side loss per Kelvin below :attr:`cold_battery_reference_temperature_c`.

    BUILD_SPEC §10.1: ``factor = 1 + max(0, 15 - T_batt) * 0.008``. Lithium-ion internal
    resistance roughly doubles between +25 °C and -10 °C; 0.8 %/K over a 35 K span reproduces
    that order of magnitude with a single linear term instead of an Arrhenius fit the project
    has no data to calibrate."""

    hvac_curve_c_kw: tuple[tuple[float, float], ...] = field(default=HVAC_CURVE_C_KW)
    """Breakpoints of the cabin-climate envelope; see :data:`HVAC_CURVE_C_KW`."""

    def air_density_kg_m3(self, temperature_c: float) -> float:
        """Air density in kg/m³ at ``temperature_c``, at constant sea-level pressure.

        ``rho = 1.225 * 288.15 / (273.15 + T)`` exactly as BUILD_SPEC §10.1 writes it. Cold air
        is denser, so the same speed costs more aerodynamic work in January than in July — a
        real but second-order part of the winter penalty (about +8 % drag at -10 °C versus
        +20 °C), well below the HVAC contribution.

        Pressure is held at the ISA reference: German weather station pressure varies by only
        a few percent, and the DWD observation feed does not reliably carry ``PP_10`` for every
        station, so making density depend on it would add a missing-data path for a sub-percent
        correction.
        """
        absolute_k = self.kelvin_offset_c + temperature_c
        if absolute_k <= 0.0:
            # Below absolute zero the formula changes sign. Unreachable with real observations;
            # guarded so a corrupted -999 sentinel from DWD cannot produce a negative density.
            return self.air_density_reference_kg_m3
        reference_k = self.kelvin_offset_c + self.air_density_reference_temperature_c
        return self.air_density_reference_kg_m3 * reference_k / absolute_k

    def hvac_power_kw(self, temperature_c: float) -> float:
        """Cabin climate load in kW at ``temperature_c``, interpolated on the documented curve.

        Clamped to the first and last breakpoint outside the tabulated range — see
        :data:`HVAC_CURVE_C_KW` for why extrapolation is refused.
        """
        return piecewise_linear(self.hvac_curve_c_kw, temperature_c)

    def auxiliary_power_kw(self, temperature_c: float) -> float:
        """Total auxiliary draw in kW: the constant base load plus the HVAC envelope."""
        return self.auxiliary_base_kw + self.hvac_power_kw(temperature_c)

    def cold_battery_factor(self, battery_temperature_c: float) -> float:
        """Internal-resistance multiplier ``1 + max(0, 15 - T_batt) * 0.008``.

        Always ``>= 1.0``. The model *multiplies* traction power by it and *divides* the
        recuperation credit by it, so a cold pack costs more to discharge and accepts less on
        the way back — see :meth:`~autotwin_ml.baseline.PhysicalEnergyModel.battery_power_w`
        for why that asymmetry is a deliberate reading of the specification.
        """
        deficit_k = max(0.0, self.cold_battery_reference_temperature_c - battery_temperature_c)
        return 1.0 + deficit_k * self.cold_battery_resistance_per_kelvin


DEFAULT_PHYSICS: Final[PhysicsConstants] = PhysicsConstants()
"""The constant set of BUILD_SPEC §10.1, shared by the model, the simulator and the insights.

A module-level singleton rather than a per-call default instance: the object is frozen, so
sharing it is safe, and identity comparison makes it obvious in a debugger whether a caller
passed a tuned set."""
