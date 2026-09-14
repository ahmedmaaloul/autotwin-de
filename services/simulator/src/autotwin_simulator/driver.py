"""The speed model: how fast a simulated driver wants to go, and how they get there.

Two separable questions, kept separate on purpose.

**What speed does this driver want here?** A function of the road class, the posted limit, the
traffic state and the weather, scaled by a seeded per-driver temperament. It is pure: the same
:class:`~autotwin_simulator.environment.RoadConditions` and the same driver always produce the
same target, which is what lets a reviewer check the speed model on paper.

**How does the vehicle get from its current speed to that target?** A first-order approach —
``a = (v_target - v) / tau`` — clipped to comfortable acceleration limits, plus a *correlated*
noise term. The correlation is the point. White noise added to acceleration produces a jagged
trace that no car has ever driven and that would teach the ML model that
``acceleration_abs_mean_ms2`` is uninformative. An Ornstein-Uhlenbeck-style AR(1) term with an
eight-second correlation time produces something that looks like a human holding a pedal.

The driver is also where standstills come from. Severity categories alone cannot produce
stop-and-go: dividing the target speed by a delay factor yields a vehicle that glides along a
jammed Autobahn at a steady 65 km/h, which is not what a jam is and not what it costs in energy.
So a driver in ``high`` or ``severe`` traffic has a per-second probability of coming to a halt
for a sampled number of seconds, and the cyclic accelerate-brake-accelerate losses that follow
are exactly the signal the physical baseline cannot see and the learned model can.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from autotwin_contracts import RoadClass, TrafficSeverity, WeatherCondition
from autotwin_ml.constants import KMH_PER_MS, MS_PER_KMH
from autotwin_simulator.environment import RoadConditions, seeded_rng

__all__ = [
    "ACCELERATION_EVENT_THRESHOLD_MS2",
    "CRUISE_SPEED_KMH",
    "WEATHER_SPEED_FACTORS",
    "Driver",
    "DriverProfile",
    "sample_driver_profile",
]

CRUISE_SPEED_KMH: Final[dict[RoadClass, float]] = {
    RoadClass.motorway: 130.0,
    RoadClass.trunk: 110.0,
    RoadClass.primary: 100.0,
    RoadClass.secondary: 85.0,
    RoadClass.tertiary: 70.0,
    RoadClass.residential: 50.0,
    RoadClass.service: 30.0,
    RoadClass.unknown: 80.0,
}
"""Free-flow speed an average driver chooses on each road class, in km/h.

``motorway`` is 130 because that is the German *Richtgeschwindigkeit* — the advisory speed on
unrestricted Autobahn — and because measured free-flow speeds on unrestricted stretches cluster
just above it. It is a starting point that the posted limit, the traffic state, the weather and
the driver's own temperament all then modify; it is never the speed anybody actually drives."""

WEATHER_SPEED_FACTORS: Final[dict[WeatherCondition, float]] = {
    WeatherCondition.clear: 1.0,
    WeatherCondition.clouds: 1.0,
    WeatherCondition.fog: 0.80,
    WeatherCondition.rain: 0.93,
    WeatherCondition.snow: 0.75,
    WeatherCondition.storm: 0.88,
    WeatherCondition.unknown: 1.0,
}
"""Multiplier on the target speed by weather condition.

Order-of-magnitude figures for how much slower German traffic runs in the wet: a few percent in
rain, a quarter in snow. They are not measured — no public German dataset pairs road weather
with free-flow speed at this granularity — so they are stated here, kept coarse, and documented
in ``docs/data/simulation.md`` rather than buried in an expression."""

ACCELERATION_EVENT_THRESHOLD_MS2: Final[float] = 1.0
"""|a| above which a physics step counts towards ``accel_events_per_km``.

1 m/s² is a firm but unremarkable acceleration (0-100 km/h in 28 s). Counting events above it
separates stop-and-go and overtaking from cruise, which is the distinction the ML feature
exists to encode."""

_NOISE_CORRELATION_TIME_S: Final[float] = 8.0
"""Correlation time of the acceleration noise. Eight seconds is roughly how long a human holds
a pedal position before adjusting it."""

_NOISE_SIGMA_MS2: Final[float] = 0.18
"""Standard deviation of the stationary acceleration noise, in m/s².

Small compared with the comfort limits: the noise is meant to texture a deliberate manoeuvre,
never to drive one."""

_LOW_SPEED_NOISE_KMH: Final[float] = 15.0
"""Below this speed the noise is faded out, so a vehicle at a standstill does not jitter."""

_STANDSTILL_RATE_PER_S: Final[dict[TrafficSeverity, float]] = {
    TrafficSeverity.low: 0.0,
    TrafficSeverity.moderate: 0.0,
    TrafficSeverity.high: 1.0 / 900.0,
    TrafficSeverity.severe: 1.0 / 180.0,
}
"""Probability per simulated second of coming to a halt, by traffic severity.

One halt per fifteen minutes in ``high`` traffic and one per three minutes in ``severe`` — the
difference between slow-moving and genuine stop-and-go. ``low`` and ``moderate`` never halt: a
free-flowing or merely dense Autobahn does not stop."""

_STANDSTILL_SECONDS: Final[tuple[float, float]] = (8.0, 50.0)
"""Uniform range of a halt's duration in seconds."""

_MIN_TARGET_SPEED_KMH: Final[float] = 5.0
"""Floor under a moving target speed, so a pathological combination of factors cannot reduce a
driving vehicle to a crawl that never finishes its route. A vehicle that should be stopped is
stopped explicitly, through the standstill model or a target override."""


@dataclass(frozen=True, slots=True)
class DriverProfile:
    """The seeded temperament of one simulated driver.

    Frozen: a driver's style is a property of the driver, not of the moment. Everything that
    varies within a trip lives in :class:`Driver`'s mutable state instead.
    """

    aggressiveness: float
    """Multiplier on the free-flow cruising speed. Roughly 0.85-1.15 across a fleet."""

    limit_compliance: float
    """Multiplier on a posted limit. Above 1.0 for the majority of German drivers, who travel a
    few percent over the sign; below it for the cautious."""

    reaction_tau_s: float
    """Time constant of the approach to the target speed, in seconds. Larger is gentler."""

    comfort_acceleration_ms2: float
    """Largest acceleration this driver asks for in normal driving."""

    comfort_deceleration_ms2: float
    """Largest deceleration in normal driving, as a positive number."""

    reserve_soc_percent: float
    """State of charge at which this driver starts looking for a charging stop.

    A range-anxious driver plugs in at 25 %, a confident one runs to 10 %. This single number
    is responsible for most of the variation in how often simulated vehicles charge, and it is
    the most human parameter in the model."""

    departure_soc_percent: float
    """State of charge this driver charges to before leaving.

    Clustered around 80 % because that is where the taper knee sits (``SOC_TAPER_CURVE``) and
    because drivers learn it; the patient tail charges to 90 %."""


def sample_driver_profile(seed: int, vehicle_id: str) -> DriverProfile:
    """Draw one driver's temperament from the fleet distribution, reproducibly.

    Every parameter is drawn from a bounded distribution and then clamped, so that no seed can
    produce a driver who accelerates at 6 m/s² or refuses to charge above 4 %. The bounds are
    the model's statement about what a *plausible* driver is.
    """
    rng = seeded_rng(seed, "driver", vehicle_id)
    aggressiveness = _clamp(rng.gauss(1.0, 0.09), 0.80, 1.20)
    return DriverProfile(
        aggressiveness=aggressiveness,
        limit_compliance=_clamp(rng.gauss(1.05, 0.05), 0.95, 1.18),
        reaction_tau_s=_clamp(rng.gauss(6.0, 1.5), 3.0, 12.0),
        comfort_acceleration_ms2=_clamp(rng.gauss(1.2, 0.25), 0.6, 2.2),
        comfort_deceleration_ms2=_clamp(rng.gauss(1.6, 0.35), 0.9, 3.0),
        # The bold driver is also the one who runs the battery down: correlating the reserve
        # with the aggressiveness keeps the fleet's characters coherent instead of producing a
        # timid speeder who charges at 25 %.
        reserve_soc_percent=_clamp(28.0 - 14.0 * aggressiveness + rng.gauss(0.0, 2.0), 8.0, 30.0),
        departure_soc_percent=_clamp(rng.gauss(80.0, 5.0), 65.0, 92.0),
    )


class Driver:
    """The mutable driving state of one vehicle: noise memory and standstill timer.

    Separate from :class:`~autotwin_simulator.vehicle.SimulatedVehicle` because the same
    physical car driven by a different person consumes measurably different energy, and keeping
    the two apart is what lets the simulation say so.
    """

    __slots__ = ("_noise_ms2", "_rng", "_standstill_remaining_s", "profile")

    def __init__(self, profile: DriverProfile, *, seed: int, vehicle_id: str) -> None:
        """Bind a temperament and its own noise/standstill random stream.

        The stream is derived from ``(seed, "behaviour", vehicle_id)`` rather than shared with
        :func:`sample_driver_profile`, so that changing how a profile is drawn cannot silently
        shift every subsequent noise sample of every vehicle.
        """
        self.profile = profile
        self._rng = seeded_rng(seed, "behaviour", vehicle_id)
        self._noise_ms2 = 0.0
        self._standstill_remaining_s = 0.0

    @property
    def is_halted(self) -> bool:
        """True while the driver is sitting in a traffic standstill."""
        return self._standstill_remaining_s > 0.0

    def target_speed_kmh(self, conditions: RoadConditions) -> float:
        """The speed this driver wants at this point on this road, in km/h.

        Pure — it reads no mutable state and draws no random numbers — so it can be tabulated
        and checked. The order of operations is: pick a free-flow cruising speed for the road
        class, scale it by temperament, cap it at the posted limit as this driver treats limits,
        then apply the traffic delay factor and the weather factor.

        Traffic enters as a *divisor*, mirroring
        :attr:`~autotwin_contracts.enums.TrafficSeverity.delay_factor`: a severity multiplies
        travel time, and travel time is distance over speed, so the same factor divides speed.
        That is the identity :attr:`~autotwin_ml.types.SegmentConditions.free_flow_speed_kmh`
        inverts when the insight generator builds its no-traffic counterfactual, and keeping the
        two consistent is what makes the traffic penalty in an explanation trustworthy.
        """
        desired = CRUISE_SPEED_KMH[conditions.road_class] * self.profile.aggressiveness
        if conditions.speed_limit_kmh is not None:
            desired = min(desired, conditions.speed_limit_kmh * self.profile.limit_compliance)
        desired /= conditions.traffic_severity.delay_factor
        desired *= WEATHER_SPEED_FACTORS[conditions.condition]
        return max(_MIN_TARGET_SPEED_KMH, desired)

    def update(
        self,
        speed_kmh: float,
        conditions: RoadConditions,
        dt_s: float,
        *,
        target_override_kmh: float | None = None,
    ) -> tuple[float, float]:
        """Advance the speed by one physics step; return ``(speed_kmh, acceleration_ms2)``.

        ``target_override_kmh`` lets the vehicle impose its own target — braking into a charging
        stop or to the end of a route — without the driver model having to know about either.
        An override of 0.0 also suppresses the standstill draw, because a vehicle that is
        stopping anyway cannot additionally be caught in a jam.

        The returned acceleration is **measured from the speed change**, not the acceleration
        that was requested. They differ whenever the speed floors at zero, and a telemetry trace
        whose acceleration does not integrate to its speed is a trace nobody can validate.
        """
        target_kmh = self._resolve_target_kmh(conditions, dt_s, target_override_kmh)
        requested_ms2 = self._requested_acceleration_ms2(speed_kmh, target_kmh, dt_s)
        new_speed_kmh = max(0.0, speed_kmh + requested_ms2 * KMH_PER_MS * dt_s)
        acceleration_ms2 = (new_speed_kmh - speed_kmh) * MS_PER_KMH / dt_s
        return new_speed_kmh, acceleration_ms2

    def _resolve_target_kmh(
        self,
        conditions: RoadConditions,
        dt_s: float,
        override_kmh: float | None,
    ) -> float:
        """Pick the target for this step, running the standstill timer as a side effect."""
        if override_kmh is not None:
            if override_kmh <= 0.0:
                self._standstill_remaining_s = 0.0
            return override_kmh
        if self._standstill_remaining_s > 0.0:
            self._standstill_remaining_s -= dt_s
            return 0.0
        if self._draws_standstill(conditions.traffic_severity, dt_s):
            self._standstill_remaining_s = self._rng.uniform(*_STANDSTILL_SECONDS)
            return 0.0
        return self.target_speed_kmh(conditions)

    def _draws_standstill(self, severity: TrafficSeverity, dt_s: float) -> bool:
        """Bernoulli draw for coming to a halt during this step.

        The per-step probability is ``1 - exp(-rate · dt)`` rather than ``rate · dt`` so that the
        halt frequency is independent of the physics step length. Without that, doubling the
        speed factor — which doubles the step — would double the number of jams, and a run at
        20x would look like a different country from the same run at 10x.
        """
        rate = _STANDSTILL_RATE_PER_S[severity]
        if rate <= 0.0:
            return False
        return self._rng.random() < 1.0 - math.exp(-rate * dt_s)

    def _requested_acceleration_ms2(
        self,
        speed_kmh: float,
        target_kmh: float,
        dt_s: float,
    ) -> float:
        """First-order approach to the target plus correlated noise, clipped to comfort limits.

        The noise is an AR(1) process whose retention factor is ``exp(-dt / tau)``, which keeps
        its correlation time and its stationary variance the same at any physics step length —
        the same step-independence argument as the standstill draw.
        """
        error_ms = (target_kmh - speed_kmh) * MS_PER_KMH
        requested = error_ms / self.profile.reaction_tau_s
        retention = math.exp(-dt_s / _NOISE_CORRELATION_TIME_S)
        self._noise_ms2 = retention * self._noise_ms2 + math.sqrt(
            max(0.0, 1.0 - retention * retention)
        ) * self._rng.gauss(0.0, _NOISE_SIGMA_MS2)
        fade = min(1.0, speed_kmh / _LOW_SPEED_NOISE_KMH)
        requested += self._noise_ms2 * fade
        return _clamp(
            requested,
            -self.profile.comfort_deceleration_ms2,
            self.profile.comfort_acceleration_ms2,
        )


def _clamp(value: float, lower: float, upper: float) -> float:
    """Constrain ``value`` to ``[lower, upper]``."""
    return max(lower, min(upper, value))
