"""Value objects exchanged with the physical energy model.

Three frozen dataclasses, each with one job:

* :class:`SegmentConditions` — everything the road-load model needs to know about one stretch
  of road. It is the *input* the route analyser, the simulator and the counterfactual
  attribution in :mod:`autotwin_ml.insights` all build.
* :class:`EnergyResult` — the energy that stretch costs, **plus the term-by-term
  decomposition**. The decomposition is not a debugging nicety: the deterministic insight
  generator of BUILD_SPEC §11 is only possible because the model can say how many kWh went
  into heating and how many into pushing air out of the way.
* :class:`RouteEnergySegment` — the narrow contract between a finished route analysis and the
  charging optimiser, which needs distance, time and energy per segment and nothing else.

Plain dataclasses rather than Pydantic models: these are hot-path physics objects constructed
once per simulation tick, they never cross an HTTP boundary (the API maps them onto its own
response schemas), and ``slots=True`` keeps a 600 km route's worth of them cheap.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Final

from autotwin_contracts.enums import RoadClass, TrafficSeverity
from autotwin_core.errors import ValidationError
from autotwin_ml.constants import MS_PER_KMH

__all__ = [
    "EnergyResult",
    "RouteEnergySegment",
    "SegmentConditions",
]

_MAX_PLAUSIBLE_SPEED_KMH: Final[float] = 400.0
"""Above this a "speed" is a unit mix-up (m/s read as km/h) or corrupt telemetry."""

_MAX_PLAUSIBLE_GRADIENT_PERCENT: Final[float] = 30.0
"""Steeper than any public road in Germany; the Autobahn network stays under 6 %."""

_MAX_PLAUSIBLE_TEMPERATURE_C: Final[float] = 60.0
"""Bounds the DWD ``-999`` missing-value sentinel out of the physics."""


@dataclass(frozen=True, slots=True)
class SegmentConditions:
    """Driving conditions on one homogeneous stretch of road.

    "Homogeneous" is the modelling assumption: within a segment the speed, gradient, weather
    and traffic state are treated as constant, and the energy is the steady-state power times
    the duration. Route segmentation (5 km chunks, ``autotwin_core.geo.segmentation``) and the
    simulator's 60 s telemetry windows are both fine enough for that to hold at the accuracy
    this model claims.
    """

    speed_kmh: float
    """Mean speed over the segment. ``<= 0`` means the vehicle did not move — a traffic
    standstill or a charging stop — and then only the auxiliary load is consumed."""

    distance_km: float
    """Length of the segment in kilometres."""

    gradient_percent: float = 0.0
    """Mean longitudinal gradient in percent (rise over run · 100); negative is downhill.

    Percent rather than radians because that is how road engineering, OSM ``incline`` tags and
    elevation profiles all express it, and it is what a reader can sanity-check."""

    outside_temperature_c: float = 20.0
    """Ambient air temperature in °C. Drives the HVAC load and the air density."""

    battery_temperature_c: float | None = None
    """High-voltage pack temperature in °C; falls back to the ambient temperature when unknown.

    The fallback is the cold-soak assumption — a car that has been parked overnight. The
    simulator tracks a real pack temperature and passes it, so the fallback only applies to a
    route analysis for a vehicle that is not currently driving."""

    acceleration_ms2: float = 0.0
    """Mean **signed** acceleration over the segment, in m/s².

    Zero for a steady-state segment, which is the normal case for route analysis: a 5 km chunk
    driven at a constant 120 km/h has no net acceleration. It is non-zero for telemetry windows
    where the vehicle genuinely changed speed.

    The model deliberately does **not** see the *cyclic* accelerate-and-brake losses of
    stop-and-go traffic; a window whose speed starts and ends equal has a net acceleration of
    zero however violent the driving was in between. Those losses are what the ML features
    ``acceleration_abs_mean_ms2`` and ``accel_events_per_km`` exist to capture, and the gap
    between the two is a large part of what the learned model adds over this baseline."""

    traffic_severity: TrafficSeverity = TrafficSeverity.low
    """Traffic state on the segment. The physical model does not read it — traffic acts on
    energy through the *speed* the caller derived with
    :attr:`~autotwin_contracts.enums.TrafficSeverity.delay_factor` — but it is carried here so
    the insight generator can reconstruct the free-flow counterfactual and so the ML feature
    builder has the ordinal."""

    precipitation_mm: float = 0.0
    """Precipitation in mm over the observation interval.

    Carried as an ML feature and reported in explanations. **The physical baseline ignores it**:
    wet-road rolling resistance and spray drag are real (a few percent) but the project has no
    data to calibrate a coefficient against, and inventing one would violate the no-invented-
    numbers rule of BUILD_SPEC §0."""

    wind_speed_ms: float = 0.0
    """Scalar mean wind speed in m/s, as DWD reports it in ``FF_10``.

    A *scalar* speed cannot be turned into a force without a direction. Set
    :attr:`headwind_ms` when the wind direction and the vehicle heading are both known;
    otherwise the aerodynamic term uses the vehicle speed alone, exactly as BUILD_SPEC §10.1
    specifies, and no wind effect is claimed."""

    headwind_ms: float = 0.0
    """Component of the wind along the direction of travel, in m/s; positive is a headwind.

    Compute it with :func:`~autotwin_ml.baseline.headwind_component_ms`. The default of 0.0
    makes the aerodynamic term reduce to the specification's ``0.5 · rho · c_d · A · v²``."""

    road_class: RoadClass = RoadClass.unknown
    """Functional road class; an ML feature (``road_class_ordinal``) and a label in
    explanations. Not read by the physics — a motorway costs more only through its speed."""

    speed_limit_kmh: float | None = None
    """Posted limit in km/h, ``None`` on an unrestricted Autobahn stretch or where OSM is
    silent. An ML feature and the reference for "driving well above the limit" explanations."""

    duration_s: float | None = None
    """Explicit duration in seconds; derived from distance and speed when ``None``.

    Needed for the stationary case: a 0 km / 0 km/h window still draws the auxiliary load for
    as long as it lasts, and distance ÷ speed cannot express that."""

    def __post_init__(self) -> None:
        """Reject inputs that are physically impossible or an obvious unit mix-up.

        Raised as :class:`~autotwin_core.errors.ValidationError` (HTTP 422) because the most
        likely caller is a route-analysis request carrying a user-supplied vehicle or a DWD
        row whose ``-999`` sentinel was not converted to ``None``. Silently modelling a
        -999 °C segment would produce a plausible-looking number from nonsense input, which is
        exactly the failure mode BUILD_SPEC §0.4 forbids.
        """
        if self.distance_km < 0.0:
            msg = f"distance_km must not be negative, got {self.distance_km!r}"
            raise ValidationError(msg, details={"field": "distance_km"})
        if self.duration_s is not None and self.duration_s < 0.0:
            msg = f"duration_s must not be negative, got {self.duration_s!r}"
            raise ValidationError(msg, details={"field": "duration_s"})
        if self.speed_kmh > _MAX_PLAUSIBLE_SPEED_KMH:
            msg = (
                f"speed_kmh {self.speed_kmh!r} exceeds {_MAX_PLAUSIBLE_SPEED_KMH} km/h; "
                "check for an m/s value passed as km/h"
            )
            raise ValidationError(msg, details={"field": "speed_kmh"})
        if abs(self.gradient_percent) > _MAX_PLAUSIBLE_GRADIENT_PERCENT:
            msg = (
                f"gradient_percent {self.gradient_percent!r} exceeds "
                f"±{_MAX_PLAUSIBLE_GRADIENT_PERCENT} %; check for a value given as a ratio"
            )
            raise ValidationError(msg, details={"field": "gradient_percent"})
        if abs(self.outside_temperature_c) > _MAX_PLAUSIBLE_TEMPERATURE_C:
            msg = (
                f"outside_temperature_c {self.outside_temperature_c!r} is outside "
                f"±{_MAX_PLAUSIBLE_TEMPERATURE_C} °C; check for an unconverted DWD -999 sentinel"
            )
            raise ValidationError(msg, details={"field": "outside_temperature_c"})

    @property
    def speed_ms(self) -> float:
        """Speed in m/s, floored at zero — the SI value the force balance needs."""
        return max(0.0, self.speed_kmh) * MS_PER_KMH

    @property
    def distance_m(self) -> float:
        """Segment length in metres."""
        return self.distance_km * 1000.0

    @property
    def effective_battery_temperature_c(self) -> float:
        """Pack temperature, falling back to ambient when the caller does not track one."""
        if self.battery_temperature_c is None:
            return self.outside_temperature_c
        return self.battery_temperature_c

    @property
    def effective_duration_s(self) -> float:
        """Time spent on the segment in seconds.

        The explicit :attr:`duration_s` wins when given; otherwise distance ÷ speed. A
        stationary segment without an explicit duration takes zero time and therefore costs
        zero energy, which is the only answer that does not invent a number.
        """
        if self.duration_s is not None:
            return self.duration_s
        speed_ms = self.speed_ms
        if speed_ms <= 0.0:
            return 0.0
        return self.distance_m / speed_ms

    @property
    def free_flow_speed_kmh(self) -> float:
        """Speed the segment would be driven at without traffic, in km/h.

        Traffic acts on travel *time*: a severity multiplies free-flow time by
        :attr:`~autotwin_contracts.enums.TrafficSeverity.delay_factor`, and since time is
        distance ÷ speed, the free-flow speed is the observed speed times that same factor.
        This inverts the reduction the route analyser applied when it derived
        ``assumed_speed_kmh`` (BUILD_SPEC §7.3), and it is how
        :func:`~autotwin_ml.insights.explain_route_energy` builds its no-traffic counterfactual.

        Self-consistent when the caller applied no reduction: ``low`` has a delay factor of
        1.0, so the counterfactual equals the actual and the traffic driver is zero.
        """
        return self.speed_kmh * self.traffic_severity.delay_factor

    def at_reference_conditions(
        self, *, reference_temperature_c: float = 20.0
    ) -> SegmentConditions:
        """This segment with every *environmental* driver neutralised.

        Flat, free-flowing, still air, at the temperature where the HVAC envelope is zero and
        the cold-battery factor is one. What remains is the cost of driving this distance at
        this road class and this speed — the reference point the insight ladder starts from.
        """
        return replace(
            self,
            speed_kmh=self.free_flow_speed_kmh,
            traffic_severity=TrafficSeverity.low,
            gradient_percent=0.0,
            outside_temperature_c=reference_temperature_c,
            battery_temperature_c=reference_temperature_c,
            headwind_ms=0.0,
            duration_s=None,
        )


@dataclass(frozen=True, slots=True)
class EnergyResult:
    """Energy consumed over a segment or a whole trip, with its term-by-term decomposition.

    **Additive identity** — the six consumption buckets sum to :attr:`kwh` exactly::

        kwh == rolling_kwh + aero_kwh + gradient_kwh + inertia_kwh + auxiliary_kwh + hvac_kwh

    :attr:`regen_kwh` is a **memo, not a seventh addend**: it reports how much energy the
    battery actually got back over the segment and is already contained in the (negative)
    gradient and inertia buckets. Adding it again would double-count the recuperation. The
    distinction is called out here because a decomposition that silently fails to sum is a trap
    for every downstream chart.

    Signs: :attr:`rolling_kwh`, :attr:`aero_kwh`, :attr:`auxiliary_kwh` and :attr:`hvac_kwh` are
    never negative — those forces always oppose motion. :attr:`gradient_kwh` and
    :attr:`inertia_kwh` are signed, negative on a descent or a deceleration.
    """

    kwh: float
    """Net battery energy in kWh. Never negative for a single segment — see
    :meth:`~autotwin_ml.baseline.PhysicalEnergyModel.segment_energy` for the capping rule."""

    distance_km: float
    """Distance the energy was spent over, in kilometres."""

    duration_s: float
    """Time the energy was spent over, in seconds."""

    rolling_kwh: float
    """Work against rolling resistance (tyre deformation)."""

    aero_kwh: float
    """Work against aerodynamic drag — the term that grows with the cube of speed."""

    gradient_kwh: float
    """Potential-energy term; negative on a net descent."""

    inertia_kwh: float
    """Kinetic-energy term including the rotating-mass surcharge; negative when decelerating."""

    auxiliary_kwh: float
    """Constant 12 V base load (control units, lighting, pumps, DC/DC losses)."""

    hvac_kwh: float
    """Cabin heating and air conditioning — the dominant winter penalty."""

    regen_kwh: float
    """Energy recuperated into the battery, as a **non-positive** number (memo, see above)."""

    @property
    def kwh_per_100km(self) -> float:
        """Consumption in kWh/100 km — the target variable of the ML model.

        Returns ``0.0`` for a zero-distance result. A stationary window genuinely has no
        distance to normalise against, and the alternatives (infinity, NaN) poison every
        aggregate and every JSON payload downstream. The raw :attr:`kwh` stays exact, and the
        training-set builder is expected to drop zero-distance windows rather than feed a
        meaningless target to the regressor.
        """
        if self.distance_km <= 0.0:
            return 0.0
        return self.kwh / self.distance_km * 100.0

    @property
    def traction_kwh(self) -> float:
        """The four road-load buckets combined: energy that went through the drivetrain."""
        return self.rolling_kwh + self.aero_kwh + self.gradient_kwh + self.inertia_kwh

    @property
    def components_kwh(self) -> float:
        """Sum of the six consumption buckets; equals :attr:`kwh` up to floating-point noise.

        Exposed so tests and the insight generator can assert the additive identity instead of
        trusting it.
        """
        return self.traction_kwh + self.auxiliary_kwh + self.hvac_kwh

    @property
    def average_power_kw(self) -> float:
        """Mean battery power over the segment in kW; ``0.0`` for a zero-duration result."""
        if self.duration_s <= 0.0:
            return 0.0
        return self.kwh * 3600.0 / self.duration_s

    @classmethod
    def zero(cls, *, distance_km: float = 0.0, duration_s: float = 0.0) -> EnergyResult:
        """An all-zero result — a segment that costs nothing because no time passed on it."""
        return cls(
            kwh=0.0,
            distance_km=distance_km,
            duration_s=duration_s,
            rolling_kwh=0.0,
            aero_kwh=0.0,
            gradient_kwh=0.0,
            inertia_kwh=0.0,
            auxiliary_kwh=0.0,
            hvac_kwh=0.0,
            regen_kwh=0.0,
        )

    def __add__(self, other: EnergyResult) -> EnergyResult:
        """Combine two results bucket by bucket, so a trip decomposes like its segments."""
        return EnergyResult(
            kwh=self.kwh + other.kwh,
            distance_km=self.distance_km + other.distance_km,
            duration_s=self.duration_s + other.duration_s,
            rolling_kwh=self.rolling_kwh + other.rolling_kwh,
            aero_kwh=self.aero_kwh + other.aero_kwh,
            gradient_kwh=self.gradient_kwh + other.gradient_kwh,
            inertia_kwh=self.inertia_kwh + other.inertia_kwh,
            auxiliary_kwh=self.auxiliary_kwh + other.auxiliary_kwh,
            hvac_kwh=self.hvac_kwh + other.hvac_kwh,
            regen_kwh=self.regen_kwh + other.regen_kwh,
        )

    @classmethod
    def sum(cls, results: Iterable[EnergyResult]) -> EnergyResult:
        """Total of an iterable of results; the empty iterable totals to zero."""
        total = cls.zero()
        for result in results:
            total = total + result
        return total


@dataclass(frozen=True, slots=True)
class RouteEnergySegment:
    """One analysed segment as the charging optimiser sees it.

    The optimiser needs three numbers per segment — how far, how long, how much energy — and
    must not depend on how they were produced. Keeping this contract narrow is what lets the
    same beam search run on physical-baseline energies, on ML predictions, or on a blend,
    without a single change.
    """

    start_offset_km: float
    """Distance from the route origin to the start of this segment, in kilometres."""

    distance_km: float
    """Length of this segment in kilometres."""

    duration_s: float
    """Expected travel time on this segment in seconds, traffic delays already applied."""

    energy_kwh: float
    """Expected battery energy for this segment in kWh."""

    @property
    def end_offset_km(self) -> float:
        """Distance from the route origin to the end of this segment, in kilometres."""
        return self.start_offset_km + self.distance_km

    @classmethod
    def from_energy_result(cls, start_offset_km: float, result: EnergyResult) -> RouteEnergySegment:
        """Adapt a physical-model result into the optimiser's view of a segment."""
        return cls(
            start_offset_km=start_offset_km,
            distance_km=result.distance_km,
            duration_s=result.duration_s,
            energy_kwh=result.kwh,
        )
