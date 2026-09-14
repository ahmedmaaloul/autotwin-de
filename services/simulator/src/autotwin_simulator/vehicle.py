"""One simulated connected vehicle: position, speed, state of charge, pack temperature.

The single rule this module is built around is that **there is only one energy model in
AutoTwin**. Consumption here comes from :class:`~autotwin_ml.baseline.PhysicalEnergyModel` —
the same object, with the same constants, that ``POST /api/v1/routes/analyze`` serves
predictions from and that ``autotwin_ml.insights`` decomposes into explanation drivers. A second
implementation living in the simulator would make the ML model's headline number meaningless,
because the thing it was validated against would no longer be the thing it was trained on.

What this module adds on top of that model is *state over time*:

* **Position.** An offset in metres along a corridor, integrated from the speed the driver
  model produces. The map position is looked up from the offset only when a sample is emitted.
* **State of charge.** The integral of consumed energy over usable capacity. It is
  **non-increasing while driving**, because the physical baseline caps a segment at zero net
  energy (a downhill stretch cannot refill the pack — see
  :meth:`~autotwin_ml.baseline.PhysicalEnergyModel.segment_energy`), and strictly increasing
  while charging.
* **Battery temperature.** A first-order lag toward ambient plus ohmic self-heating proportional
  to the energy passing through the pack, with an active-cooling branch above 35 °C. It warms
  under load and cools at rest, which is what closes the loop between hard driving and the
  cold-battery penalty in the energy model.
* **Charging.** Below the driver's reserve state of charge the vehicle picks a real ingested
  charging site ahead of it on the corridor, drives there, and recovers charge along
  :func:`~autotwin_ml.charging.charging_power_kw` — the same taper the route optimiser plans
  with.

Every value the vehicle reports is either integrated from the physics above or read from the
environment. Nothing is drawn at random except the driver's behaviour, and that is seeded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from autotwin_contracts import (
    RoadClass,
    TelemetryEvent,
    TripEvent,
    TripEventType,
    VehicleProfile,
    VehicleState,
)
from autotwin_ml import (
    ChargingCandidate,
    PhysicalEnergyModel,
    SegmentConditions,
    charging_power_kw,
)
from autotwin_ml.baseline import headwind_component_ms
from autotwin_ml.constants import DEFAULT_PHYSICS, KMH_PER_MS, MS_PER_KMH, PhysicsConstants
from autotwin_simulator.driver import ACCELERATION_EVENT_THRESHOLD_MS2, Driver
from autotwin_simulator.environment import RoadConditions, RouteEnvironment, seeded_rng

__all__ = [
    "FALLBACK_CHARGER_POWER_KW",
    "PACK_ACTIVE_COOLING_TARGET_C",
    "PACK_AMBIENT_TIME_CONSTANT_S",
    "PACK_HEATING_C_PER_KWH",
    "ROLLING_CONSUMPTION_HALF_LIFE_KM",
    "IntervalStatistics",
    "SimulatedVehicle",
    "TelemetrySample",
    "VehicleStep",
]


# --------------------------------------------------------------------------------------
# Thermal model of the high-voltage pack
# --------------------------------------------------------------------------------------

PACK_THERMAL_CAPACITY_KWH_PER_K: Final[float] = 0.111
"""Heat capacity of a typical EV traction battery, in kWh per Kelvin.

Roughly 400 kg of cells, module hardware and coolant at an effective 1 000 J/(kg·K) — a pack
that heavy stores 4·10⁵ J/K, which is 0.111 kWh/K. It is why a battery's temperature moves in
tens of minutes rather than seconds, and why a single hard overtake changes nothing."""

PACK_OHMIC_LOSS_FRACTION: Final[float] = 0.07
"""Share of the energy passing through the pack that is dissipated inside it as heat.

Internal resistance losses at the cell and interconnect level, lumped into one number. 7 % is
the order of magnitude for a warm pack at motorway load; it rises steeply when cold, which this
model does not attempt to capture (the cold penalty is in the energy model instead)."""

PACK_HEATING_C_PER_KWH: Final[float] = PACK_OHMIC_LOSS_FRACTION / PACK_THERMAL_CAPACITY_KWH_PER_K
"""Temperature rise in Kelvin per kWh through the pack — derived, never tuned separately.

0.63 K/kWh. At a 25 kW motorway load that is 4.4 K per hour of heating, which the ambient lag
below balances at roughly 8 K above ambient. Deriving it from the two physical constants above
rather than fitting it is what keeps the number explicable."""

PACK_AMBIENT_TIME_CONSTANT_S: Final[float] = 1_800.0
"""Time constant of passive heat exchange with the surrounding air, in seconds.

Half an hour: a pack parked in the cold is still warm after twenty minutes and cold after two
hours, which matches how badly a car that sat out overnight charges in the morning."""

PACK_ACTIVE_COOLING_TARGET_C: Final[float] = 35.0
"""Temperature above which the thermal-management system starts working in earnest."""

PACK_ACTIVE_COOLING_TIME_CONSTANT_S: Final[float] = 600.0
"""Time constant of the active cooling branch, in seconds — three times stronger than passive.

It is what keeps a 150 kW charging session from running the pack to an implausible temperature.
The model has no chiller capacity limit and no pre-conditioning, so it is an envelope, not a
thermal simulation of a specific cooling circuit."""


# --------------------------------------------------------------------------------------
# Range, charging and trip behaviour
# --------------------------------------------------------------------------------------

ROLLING_CONSUMPTION_HALF_LIFE_KM: Final[float] = 5.0
"""Half-life, in kilometres, of the exponential average behind ``estimated_range_km``.

Distance-weighted rather than time-weighted, because the quantity being averaged is kWh per
100 km: an hour spent at a standstill must not dominate the estimate the way it would if the
weighting were by time. Five kilometres is what a dashboard range estimate behaves like — it
reacts to a change of road but not to a single overtake."""

MAX_ROLLING_CONSUMPTION_FACTOR: Final[float] = 5.0
"""Cap on one sample's contribution, as a multiple of the vehicle's nominal consumption.

A 0.2 s step during a hard acceleration has a genuine instantaneous consumption of several
hundred kWh/100 km. Feeding that into the range estimate would make the number jump around in a
way no real instrument does, so a single sample can never push the average past five times
nominal."""

FALLBACK_CHARGER_POWER_KW: Final[float] = 50.0
"""Power of the **synthetic** charger used when a corridor has no ingested charging sites.

The Ladesäulenregister has not been ingested, or the corridor genuinely has no DC site within
:data:`~autotwin_simulator.environment.CHARGING_CORRIDOR_BUFFER_KM`. Rather than strand the
fleet — which would make ``make demo`` look broken — the vehicle charges where it stands at
50 kW, the most common German DC rating, with ``station_id = None`` on the trip event and a
``reason`` that names the fallback. The engine logs it at ``warning`` level once per corridor.
Nothing downstream may count this as infrastructure: it corresponds to no real site."""

CRITICAL_SOC_PERCENT: Final[float] = 3.0
"""Below this the vehicle stops where it is and charges from the synthetic fallback.

A real driver at 3 % is calling the ADAC. The model's version of that is a rescue charge, which
keeps a long-running demo populated instead of accumulating bricked vehicles at the roadside."""

STOP_APPROACH_DECELERATION_MS2: Final[float] = 1.0
"""Deceleration used to plan the approach to a charging stop or the end of a route, in m/s².

Gentle on purpose: it sets how far ahead the vehicle starts slowing (``v²/2a`` — 626 m from
130 km/h), and a vehicle that brakes harder than its driver's comfort limit would produce an
acceleration trace with a discontinuity at every stop."""

ARRIVAL_TOLERANCE_M: Final[float] = 25.0
"""How close to a charging site or a corridor end counts as having arrived."""

IDLE_DWELL_SECONDS: Final[tuple[float, float]] = (120.0, 900.0)
"""Uniform range of the pause between finishing one trip and starting the next, in seconds."""

CHARGE_SEARCH_RETRY_KM: Final[float] = 5.0
"""How far a vehicle drives between charging searches.

Without it every vehicle would rescan the candidate list on every physics step — several hundred
vehicles times ten steps a tick, for an answer that cannot change in half a second of simulated
time. Five kilometres is about two minutes of motorway driving, far finer than the spacing of
German fast-charging sites."""

_NEVER_SEARCHED_OFFSET_M: Final[float] = -math.inf
"""Sentinel for "no charging search has run on this trip yet", so the first one is not
throttled. A finite sentinel would delay the first search by :data:`CHARGE_SEARCH_RETRY_KM`,
which matters for a vehicle that starts a trip already below its reserve."""

_CANDIDATE_SEPARATION_KM: Final[float] = 0.5
"""Minimum gap between two sites for the "is there another one after this?" test.

Guards against a cluster of pillars at one service area answering "yes, there is a later site"
about itself and talking the vehicle out of the last real stop before a gap."""

_MINIMUM_RESERVE_RANGE_FACTOR: Final[float] = 1.15
"""Safety factor on the distance a vehicle believes it can still cover when choosing a stop.

The vehicle looks for sites within ``remaining_range / factor`` rather than ``remaining_range``,
so its own optimism cannot be the reason it strands itself between two chargers."""


# --------------------------------------------------------------------------------------
# What one telemetry interval carries
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntervalStatistics:
    """Physics-step statistics accumulated between two telemetry samples.

    The telemetry event is a *snapshot* — the speed at one instant — but the ML feature vector of
    BUILD_SPEC §10.2 needs distributional quantities: the standard deviation of speed, the mean
    absolute acceleration, the count of acceleration events. Recovering those from 10-second
    snapshots would throw away exactly the stop-and-go structure that distinguishes a jam from a
    cruise, so they are accumulated at the physics step (1 s by default) and carried here.

    Every field is a **sum**, not a mean, so that windows of different lengths combine by simple
    addition in :mod:`autotwin_simulator.training_data`.
    """

    steps: int
    duration_s: float
    distance_km: float
    energy_kwh: float
    driving_steps: int
    """Steps spent in :attr:`~autotwin_contracts.enums.VehicleState.driving`; the training-set
    builder keeps only windows that are entirely driving."""

    speed_sum_kmh: float
    speed_square_sum_kmh2: float
    absolute_acceleration_sum_ms2: float
    acceleration_events: int
    temperature_sum_c: float
    battery_temperature_sum_c: float
    soc_sum_percent: float
    gradient_sum_percent: float
    precipitation_sum_mm: float
    wind_speed_sum_ms: float
    speed_limit_sum_kmh: float
    """Sum of the posted limit, with unrestricted stretches contributing
    :data:`~autotwin_simulator.training_data.UNRESTRICTED_SPEED_LIMIT_KMH`."""

    motorway_steps: int
    road_class_steps: tuple[tuple[RoadClass, int], ...]
    """Step count per road class, so a window can report the class it mostly ran on."""

    traffic_severity_ordinal_sum: int


@dataclass(slots=True)
class _IntervalAccumulator:
    """Mutable counterpart of :class:`IntervalStatistics`, reset after every sample."""

    steps: int = 0
    duration_s: float = 0.0
    distance_km: float = 0.0
    energy_kwh: float = 0.0
    driving_steps: int = 0
    speed_sum_kmh: float = 0.0
    speed_square_sum_kmh2: float = 0.0
    absolute_acceleration_sum_ms2: float = 0.0
    acceleration_events: int = 0
    temperature_sum_c: float = 0.0
    battery_temperature_sum_c: float = 0.0
    soc_sum_percent: float = 0.0
    gradient_sum_percent: float = 0.0
    precipitation_sum_mm: float = 0.0
    wind_speed_sum_ms: float = 0.0
    speed_limit_sum_kmh: float = 0.0
    motorway_steps: int = 0
    road_class_steps: dict[RoadClass, int] = field(default_factory=dict)
    traffic_severity_ordinal_sum: int = 0

    def add(
        self,
        *,
        conditions: RoadConditions,
        state: VehicleState,
        dt_s: float,
        speed_kmh: float,
        acceleration_ms2: float,
        distance_km: float,
        energy_kwh: float,
        battery_temperature_c: float,
        soc_percent: float,
        unrestricted_limit_kmh: float,
    ) -> None:
        """Fold one physics step into the accumulator."""
        self.steps += 1
        self.duration_s += dt_s
        self.distance_km += distance_km
        self.energy_kwh += energy_kwh
        if state is VehicleState.driving:
            self.driving_steps += 1
        self.speed_sum_kmh += speed_kmh
        self.speed_square_sum_kmh2 += speed_kmh * speed_kmh
        self.absolute_acceleration_sum_ms2 += abs(acceleration_ms2)
        if abs(acceleration_ms2) >= ACCELERATION_EVENT_THRESHOLD_MS2:
            self.acceleration_events += 1
        self.temperature_sum_c += conditions.temperature_c
        self.battery_temperature_sum_c += battery_temperature_c
        self.soc_sum_percent += soc_percent
        self.gradient_sum_percent += conditions.gradient_percent
        self.precipitation_sum_mm += conditions.precipitation_mm
        self.wind_speed_sum_ms += conditions.wind_speed_ms
        limit = conditions.speed_limit_kmh
        self.speed_limit_sum_kmh += limit if limit is not None else unrestricted_limit_kmh
        if conditions.road_class is RoadClass.motorway:
            self.motorway_steps += 1
        self.road_class_steps[conditions.road_class] = (
            self.road_class_steps.get(conditions.road_class, 0) + 1
        )
        self.traffic_severity_ordinal_sum += conditions.traffic_severity.ordinal

    def snapshot(self) -> IntervalStatistics:
        """Freeze the accumulator into the value object the sample carries."""
        return IntervalStatistics(
            steps=self.steps,
            duration_s=self.duration_s,
            distance_km=self.distance_km,
            energy_kwh=self.energy_kwh,
            driving_steps=self.driving_steps,
            speed_sum_kmh=self.speed_sum_kmh,
            speed_square_sum_kmh2=self.speed_square_sum_kmh2,
            absolute_acceleration_sum_ms2=self.absolute_acceleration_sum_ms2,
            acceleration_events=self.acceleration_events,
            temperature_sum_c=self.temperature_sum_c,
            battery_temperature_sum_c=self.battery_temperature_sum_c,
            soc_sum_percent=self.soc_sum_percent,
            gradient_sum_percent=self.gradient_sum_percent,
            precipitation_sum_mm=self.precipitation_sum_mm,
            wind_speed_sum_ms=self.wind_speed_sum_ms,
            speed_limit_sum_kmh=self.speed_limit_sum_kmh,
            motorway_steps=self.motorway_steps,
            road_class_steps=tuple(sorted(self.road_class_steps.items())),
            traffic_severity_ordinal_sum=self.traffic_severity_ordinal_sum,
        )

    def reset(self) -> None:
        """Start a new interval."""
        self.steps = 0
        self.duration_s = 0.0
        self.distance_km = 0.0
        self.energy_kwh = 0.0
        self.driving_steps = 0
        self.speed_sum_kmh = 0.0
        self.speed_square_sum_kmh2 = 0.0
        self.absolute_acceleration_sum_ms2 = 0.0
        self.acceleration_events = 0
        self.temperature_sum_c = 0.0
        self.battery_temperature_sum_c = 0.0
        self.soc_sum_percent = 0.0
        self.gradient_sum_percent = 0.0
        self.precipitation_sum_mm = 0.0
        self.wind_speed_sum_ms = 0.0
        self.speed_limit_sum_kmh = 0.0
        self.motorway_steps = 0
        self.road_class_steps = {}
        self.traffic_severity_ordinal_sum = 0


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One telemetry event plus everything the training-set builder needs and it does not carry.

    ``TelemetryEvent`` is the wire contract of BUILD_SPEC §3.2 and deliberately holds only what
    the ``telemetry`` table holds. The gradient, the wind, the precipitation and the vehicle's
    physical parameters are not in that table — they belong to the route and the model — so they
    travel alongside rather than being bolted onto the event and leaking into the topic.
    """

    event: TelemetryEvent
    profile: VehicleProfile
    conditions: RoadConditions
    interval: IntervalStatistics


@dataclass(frozen=True, slots=True)
class VehicleStep:
    """What one tick of one vehicle produced."""

    sample: TelemetrySample
    trip_events: tuple[TripEvent, ...] = ()


# --------------------------------------------------------------------------------------
# The vehicle
# --------------------------------------------------------------------------------------


class SimulatedVehicle:
    """A single connected vehicle driving AutoTwin's demo corridors.

    Mutable by nature — it is a state machine over simulated time — but every transition is
    driven by the physics above plus the seeded driver, so two runs with the same seed produce
    the same trace. See ``docs/data/simulation.md`` for the full list of assumptions.
    """

    __slots__ = (
        "_charge_search_block_offset_m",
        "_charging_candidate",
        "_charging_station_power_kw",
        "_charging_target_soc_percent",
        "_dwell_remaining_s",
        "_energy_model",
        "_interval",
        "_pending_trip_events",
        "_physics",
        "_rng",
        "_rolling_kwh_100km",
        "_trip_index",
        "_unrestricted_limit_kmh",
        "acceleration_ms2",
        "battery_temperature_c",
        "cumulative_energy_kwh",
        "driver",
        "environment",
        "odometer_m",
        "offset_m",
        "profile",
        "soc_percent",
        "speed_kmh",
        "state",
        "trip_id",
        "trip_token",
        "vehicle_id",
    )

    def __init__(
        self,
        *,
        vehicle_id: str,
        profile: VehicleProfile,
        environment: RouteEnvironment,
        driver: Driver,
        seed: int,
        trip_token: str,
        soc_percent: float,
        ambient_temperature_c: float,
        energy_model: PhysicalEnergyModel | None = None,
        physics: PhysicsConstants = DEFAULT_PHYSICS,
        unrestricted_limit_kmh: float = 130.0,
    ) -> None:
        """Place a vehicle at the start of a corridor, cold-soaked at the ambient temperature.

        ``trip_token`` disambiguates trip identifiers between simulation runs: the fleet's
        ``vehicle_id`` values are stable (``ATW-0042`` is the same car in every run, which is
        what makes the vehicle history meaningful), so the trip key has to carry the run.
        """
        self.vehicle_id = vehicle_id
        self.profile = profile
        self.environment = environment
        self.driver = driver
        self.trip_token = trip_token
        self._energy_model = energy_model if energy_model is not None else PhysicalEnergyModel()
        self._physics = physics
        self._rng = seeded_rng(seed, "vehicle", vehicle_id)
        self._unrestricted_limit_kmh = unrestricted_limit_kmh

        self.state = VehicleState.idle
        self.offset_m = 0.0
        self.speed_kmh = 0.0
        self.acceleration_ms2 = 0.0
        self.soc_percent = soc_percent
        self.battery_temperature_c = ambient_temperature_c
        self.cumulative_energy_kwh = 0.0
        self.odometer_m = 0.0
        self.trip_id: str | None = None

        self._trip_index = 0
        self._dwell_remaining_s = 0.0
        self._rolling_kwh_100km = profile.nominal_consumption_kwh_100km
        self._charging_candidate: ChargingCandidate | None = None
        self._charging_station_power_kw = 0.0
        self._charging_target_soc_percent = 0.0
        self._charge_search_block_offset_m = _NEVER_SEARCHED_OFFSET_M
        self._interval = _IntervalAccumulator()
        self._pending_trip_events: list[TripEvent] = []

    # -- introspection ------------------------------------------------------------------

    @property
    def route_id(self) -> UUID | None:
        """``routes.id`` of the corridor being driven, when it came from the database."""
        return self.environment.route.route_id

    @property
    def trip_count(self) -> int:
        """How many trips this vehicle has opened. Part of the route-choice seed key."""
        return self._trip_index

    @property
    def remaining_energy_kwh(self) -> float:
        """Usable energy still in the pack, in kWh."""
        return self.profile.usable_capacity_kwh * self.soc_percent / 100.0

    @property
    def rolling_consumption_kwh_100km(self) -> float:
        """Distance-weighted exponential average of recent consumption, in kWh/100 km."""
        return self._rolling_kwh_100km

    @property
    def estimated_range_km(self) -> float:
        """Remaining range at the recent consumption rate, in km.

        The quantity a dashboard shows. Uses the rolling average rather than the nominal figure
        because that is what makes the number responsive to weather and driving style — which is
        the whole reason range estimation is hard in a real EV.
        """
        consumption = max(1.0, self._rolling_kwh_100km)
        return max(0.0, self.remaining_energy_kwh / consumption * 100.0)

    @property
    def is_charging(self) -> bool:
        """True while plugged in."""
        return self.state is VehicleState.charging

    # -- lifecycle ----------------------------------------------------------------------

    def begin_trip(self, environment: RouteEnvironment, when: datetime) -> TripEvent:
        """Put the vehicle at the start of a corridor and open a trip.

        Resets the trip-scoped counters (odometer, cumulative energy) but **not** the pack
        temperature or the state of charge: those are properties of the car, and a fleet whose
        batteries reset to full between trips would never charge and would never show the
        winter penalty accumulating over a day.
        """
        self._trip_index += 1
        self.environment = environment
        self.offset_m = 0.0
        self.speed_kmh = 0.0
        self.acceleration_ms2 = 0.0
        self.odometer_m = 0.0
        self.cumulative_energy_kwh = 0.0
        self.state = VehicleState.driving
        self.trip_id = f"{self.vehicle_id}-{self.trip_token}-{self._trip_index:04d}"
        self._charging_candidate = None
        self._charging_station_power_kw = 0.0
        self._charging_target_soc_percent = 0.0
        self._charge_search_block_offset_m = _NEVER_SEARCHED_OFFSET_M
        return self._trip_event(TripEventType.started, when)

    def pause_trip(self, when: datetime, *, reason: str) -> TripEvent:
        """Mark the in-flight trip as interrupted and bring the vehicle to a halt.

        Called by the engine when a run stops with trips still open. The trip is *not* finished
        — the vehicle never reached the end of its corridor — so ``ended_at`` stays null and the
        state becomes ``stopped``, which is what BUILD_SPEC §2 defines ``stopped`` to mean.
        """
        self.speed_kmh = 0.0
        self.acceleration_ms2 = 0.0
        self.state = VehicleState.stopped
        return self._trip_event(TripEventType.paused, when, reason=reason)

    def needs_route(self) -> bool:
        """True when the engine should hand this vehicle a corridor and start a trip.

        The engine asks rather than the vehicle pushing, because route assignment is a fleet
        decision — which corridors this run covers, and in which direction — that a single
        vehicle has no business making.
        """
        return self.state is VehicleState.idle and self._dwell_remaining_s <= 0.0

    # -- stepping -----------------------------------------------------------------------

    def advance(
        self,
        *,
        start_time: datetime,
        duration_s: float,
        physics_step_s: float,
    ) -> VehicleStep:
        """Integrate ``duration_s`` of simulated time and emit one telemetry sample.

        The interval is sub-stepped at ``physics_step_s`` so that the speed factor changes how
        fast the demo runs in wall time, not how accurately the vehicle is integrated. Without
        it, a run at 20x would integrate a 20-second Euler step and a vehicle would overshoot
        every charging stop it planned.
        """
        steps = max(1, round(duration_s / physics_step_s))
        step_s = duration_s / steps
        for index in range(steps):
            at_time = start_time + timedelta(seconds=step_s * (index + 1))
            self._step(step_s, at_time)

        recorded_at = start_time + timedelta(seconds=duration_s)
        conditions = self.environment.conditions_at(self.offset_m, recorded_at)
        sample = TelemetrySample(
            event=self._telemetry_event(recorded_at, conditions),
            profile=self.profile,
            conditions=conditions,
            interval=self._interval.snapshot(),
        )
        self._interval.reset()
        trip_events = tuple(self._pending_trip_events)
        self._pending_trip_events.clear()
        if self.state is VehicleState.completed:
            # The completed sample has been emitted; the car itself is now free for the next
            # trip, which is exactly the split `apply_trip_event` encodes for `finished`.
            self.state = VehicleState.idle
        return VehicleStep(sample=sample, trip_events=trip_events)

    def _step(self, dt_s: float, at_time: datetime) -> None:
        """Advance one physics step and fold it into the interval accumulator."""
        conditions = self.environment.conditions_at(self.offset_m, at_time)
        if self.state is VehicleState.charging:
            energy_kwh, distance_km = self._charge_step(dt_s, at_time, conditions)
        elif self.state in (VehicleState.driving, VehicleState.stopped):
            energy_kwh, distance_km = self._drive_step(dt_s, at_time, conditions)
        else:
            energy_kwh, distance_km = self._idle_step(dt_s, conditions)

        self._interval.add(
            conditions=conditions,
            state=self.state,
            dt_s=dt_s,
            speed_kmh=self.speed_kmh,
            acceleration_ms2=self.acceleration_ms2,
            distance_km=distance_km,
            energy_kwh=energy_kwh,
            battery_temperature_c=self.battery_temperature_c,
            soc_percent=self.soc_percent,
            unrestricted_limit_kmh=self._unrestricted_limit_kmh,
        )

    # -- the three step kinds -----------------------------------------------------------

    def _drive_step(
        self,
        dt_s: float,
        at_time: datetime,
        conditions: RoadConditions,
    ) -> tuple[float, float]:
        """Drive for ``dt_s`` seconds; return ``(energy_kwh, distance_km)``."""
        self._maybe_plan_charging_stop()
        override_kmh = self._approach_override_kmh()
        previous_speed_kmh = self.speed_kmh
        self.speed_kmh, self.acceleration_ms2 = self.driver.update(
            previous_speed_kmh,
            conditions,
            dt_s,
            target_override_kmh=override_kmh,
        )
        mean_speed_kmh = 0.5 * (previous_speed_kmh + self.speed_kmh)
        distance_m = mean_speed_kmh * MS_PER_KMH * dt_s
        distance_km = distance_m / 1000.0

        segment = self._segment_conditions(conditions, mean_speed_kmh, distance_km, dt_s)
        result = self._energy_model.segment_energy(self.profile, segment)
        energy_kwh = result.kwh

        self.offset_m = min(self.environment.route.length_m, self.offset_m + distance_m)
        self.odometer_m += distance_m
        self.cumulative_energy_kwh += energy_kwh
        self._consume(energy_kwh)
        self._update_battery_temperature(dt_s, conditions.temperature_c, energy_kwh)
        self._update_rolling_consumption(distance_km, energy_kwh)

        self.state = (
            VehicleState.stopped
            if self.speed_kmh <= 0.0 and self.driver.is_halted
            else VehicleState.driving
        )
        self._resolve_driving_transitions(at_time)
        return energy_kwh, distance_km

    def _charge_step(
        self,
        dt_s: float,
        at_time: datetime,
        conditions: RoadConditions,
    ) -> tuple[float, float]:
        """Charge for ``dt_s`` seconds; return ``(energy_kwh, distance_km)``.

        The energy reported is **zero**: while plugged in the vehicle is supplied by the grid,
        not by the pack, so counting the session against ``trips.energy_kwh`` would corrupt every
        consumption figure derived from it. The auxiliary load is likewise carried by the charger
        and is simply subtracted from the power reaching the battery.
        """
        vehicle_kw = charging_power_kw(self.profile, self.soc_percent, self.battery_temperature_c)
        available_kw = min(self._charging_station_power_kw, vehicle_kw)
        auxiliary_kw = self._physics.auxiliary_power_kw(conditions.temperature_c)
        net_kw = max(0.0, available_kw - auxiliary_kw)
        energy_added_kwh = net_kw * dt_s / 3600.0
        self.soc_percent = min(100.0, self.soc_percent + self._soc_delta(energy_added_kwh))
        self.speed_kmh = 0.0
        self.acceleration_ms2 = 0.0
        self._update_battery_temperature(dt_s, conditions.temperature_c, energy_added_kwh)

        if self.soc_percent >= self._charging_target_soc_percent - 1e-9:
            self.state = VehicleState.driving
            self._pending_trip_events.append(
                self._trip_event(
                    TripEventType.charging_finished,
                    at_time,
                    station_id=self._booked_station_id,
                    reason="target_soc_reached",
                )
            )
            self._charging_candidate = None
            self._charging_station_power_kw = 0.0
            self._charging_target_soc_percent = 0.0
            self._charge_search_block_offset_m = self.offset_m
        return 0.0, 0.0

    def _idle_step(self, dt_s: float, conditions: RoadConditions) -> tuple[float, float]:
        """Sit parked for ``dt_s`` seconds; return ``(energy_kwh, distance_km)``.

        A parked car is not switched off in this model — it keeps its 12 V base load — but the
        cabin climate is not maintained, so only :attr:`PhysicsConstants.auxiliary_base_kw`
        applies. Over a fifteen-minute dwell that is 0.09 kWh: small, real, and the reason the
        pack cools toward ambient rather than holding its temperature.
        """
        self._dwell_remaining_s = max(0.0, self._dwell_remaining_s - dt_s)
        self.speed_kmh = 0.0
        self.acceleration_ms2 = 0.0
        energy_kwh = self._physics.auxiliary_base_kw * dt_s / 3600.0
        self._consume(energy_kwh)
        self._update_battery_temperature(dt_s, conditions.temperature_c, energy_kwh)
        return energy_kwh, 0.0

    # -- transitions --------------------------------------------------------------------

    def _resolve_driving_transitions(self, at_time: datetime) -> None:
        """Arrive at a charging stop, run out of charge, or reach the end of the corridor."""
        if self._at_charging_stop():
            self._begin_charging(at_time)
            return
        if self.soc_percent <= CRITICAL_SOC_PERCENT:
            self._begin_fallback_charging(at_time, reason="soc_depleted")
            return
        if self.offset_m >= self.environment.route.length_m - ARRIVAL_TOLERANCE_M:
            self._finish_trip(at_time)

    def _maybe_plan_charging_stop(self) -> None:
        """Decide whether to book a charging site, and which one.

        Two rules, in this order.

        1. **Below the driver's reserve**, book the strongest site within the distance the
           vehicle still believes it can cover (discounted by
           :data:`_MINIMUM_RESERVE_RANGE_FACTOR`, so its own optimism cannot strand it).
           Strongest rather than nearest is the trade-off the route optimiser also makes:
           charging time dominates a stop's cost, so twenty extra kilometres to a 300 kW site
           beats stopping at a 50 kW one.

        2. **Last chance before a gap.** Above the reserve the vehicle would rather keep
           driving — but only while another site stays reachable. If the best site in range is
           also the *last* site in range, driving past it means the next stop is the synthetic
           fallback charger at 3 % SOC. So it stops here. This is the rule that makes the fleet
           behave like drivers rather than like optimists, and without it a corridor whose
           chargers are unevenly spaced strands cars between them.

        The search is throttled to once per :data:`CHARGE_SEARCH_RETRY_KM` of driving. A charger
        five kilometres earlier or later is immaterial, and scanning the candidate list on every
        physics step of every vehicle is not.
        """
        if self._charging_candidate is not None:
            return
        if self.offset_m < self._charge_search_block_offset_m + CHARGE_SEARCH_RETRY_KM * 1000.0:
            return
        self._charge_search_block_offset_m = self.offset_m

        offset_km = self.offset_m / 1000.0
        reach_km = offset_km + self.estimated_range_km / _MINIMUM_RESERVE_RANGE_FACTOR
        best = self.environment.next_charging_candidate(offset_km, max_offset_km=reach_km)
        if best is None:
            return
        if self.soc_percent > self.driver.profile.reserve_soc_percent:
            successor = self.environment.next_charging_candidate(
                best.offset_km + _CANDIDATE_SEPARATION_KM,
                max_offset_km=reach_km,
            )
            if successor is not None:
                return
        self._charging_candidate = best
        self._charging_station_power_kw = best.max_power_kw
        self._charging_target_soc_percent = self.driver.profile.departure_soc_percent

    @property
    def _booked_station_id(self) -> str | None:
        """``external_id`` of the booked site, or ``None`` for the synthetic fallback charger."""
        return self._charging_candidate.station_id if self._charging_candidate else None

    def _at_charging_stop(self) -> bool:
        """True once the vehicle has reached the site it booked."""
        candidate = self._charging_candidate
        if candidate is None:
            return False
        return self.offset_m >= candidate.offset_km * 1000.0 - ARRIVAL_TOLERANCE_M

    def _approach_override_kmh(self) -> float | None:
        """Target speed while braking into a stop, or ``None`` to let the driver decide.

        ``v = sqrt(2·a·s)`` is the speed from which the planned deceleration brings the vehicle
        to rest in the remaining distance. Returning it as a *target* rather than commanding a
        deceleration keeps the approach inside the driver model, so the noise and the comfort
        limits still apply and the trace does not change character at a stop.
        """
        stop_offset_m = self._next_stop_offset_m()
        if stop_offset_m is None:
            return None
        remaining_m = max(0.0, stop_offset_m - self.offset_m)
        return math.sqrt(2.0 * STOP_APPROACH_DECELERATION_MS2 * remaining_m) * KMH_PER_MS

    def _next_stop_offset_m(self) -> float | None:
        """Offset the vehicle must be stopped at, if it is already close enough to plan for."""
        stop_offset_m = self.environment.route.length_m
        candidate = self._charging_candidate
        if candidate is not None:
            stop_offset_m = min(stop_offset_m, candidate.offset_km * 1000.0)
        speed_ms = self.speed_kmh * MS_PER_KMH
        braking_distance_m = speed_ms * speed_ms / (2.0 * STOP_APPROACH_DECELERATION_MS2)
        if stop_offset_m - self.offset_m > braking_distance_m:
            return None
        return stop_offset_m

    def _begin_charging(self, at_time: datetime) -> None:
        """Plug in at the booked site."""
        self.state = VehicleState.charging
        self.speed_kmh = 0.0
        self.acceleration_ms2 = 0.0
        self._pending_trip_events.append(
            self._trip_event(
                TripEventType.charging_started,
                at_time,
                station_id=self._booked_station_id,
                reason="soc_below_reserve",
            )
        )

    def _begin_fallback_charging(self, at_time: datetime, *, reason: str) -> None:
        """Charge where the vehicle stands, from the synthetic fallback charger.

        Draws :data:`FALLBACK_CHARGER_POWER_KW`. Reached when the corridor has no ingested
        charging sites in range, or when the vehicle ran the pack down before it found one.
        ``station_id`` is ``None`` on the trip event, so nothing downstream can mistake this for
        a session at a real site.
        """
        self.state = VehicleState.charging
        self.speed_kmh = 0.0
        self.acceleration_ms2 = 0.0
        self._charging_candidate = None
        self._charging_station_power_kw = FALLBACK_CHARGER_POWER_KW
        self._charging_target_soc_percent = self.driver.profile.departure_soc_percent
        self._pending_trip_events.append(
            self._trip_event(TripEventType.charging_started, at_time, reason=reason)
        )

    def _finish_trip(self, at_time: datetime) -> None:
        """Close the trip at the end of the corridor and start the idle dwell."""
        self.offset_m = self.environment.route.length_m
        self.speed_kmh = 0.0
        self.acceleration_ms2 = 0.0
        self.state = VehicleState.completed
        self._dwell_remaining_s = self._rng.uniform(*IDLE_DWELL_SECONDS)
        self._pending_trip_events.append(self._trip_event(TripEventType.finished, at_time))

    # -- physics helpers ----------------------------------------------------------------

    def _segment_conditions(
        self,
        conditions: RoadConditions,
        speed_kmh: float,
        distance_km: float,
        dt_s: float,
    ) -> SegmentConditions:
        """Translate the environment into the energy model's input for one step.

        The headwind is resolved here rather than left at zero: the simulator knows both the
        wind direction and the vehicle's heading, which is exactly the case
        :func:`~autotwin_ml.baseline.headwind_component_ms` exists for, and it is why a
        south-bound and a north-bound vehicle on the same corridor consume differently on a
        windy day.
        """
        return SegmentConditions(
            speed_kmh=speed_kmh,
            distance_km=distance_km,
            gradient_percent=conditions.gradient_percent,
            outside_temperature_c=conditions.temperature_c,
            battery_temperature_c=self.battery_temperature_c,
            acceleration_ms2=self.acceleration_ms2,
            traffic_severity=conditions.traffic_severity,
            precipitation_mm=conditions.precipitation_mm,
            wind_speed_ms=conditions.wind_speed_ms,
            headwind_ms=headwind_component_ms(
                conditions.wind_speed_ms,
                conditions.wind_direction_deg,
                conditions.heading_deg,
            ),
            road_class=conditions.road_class,
            speed_limit_kmh=conditions.speed_limit_kmh,
            duration_s=dt_s,
        )

    def _soc_delta(self, energy_kwh: float) -> float:
        """Percentage points of state of charge corresponding to ``energy_kwh``."""
        return energy_kwh / self.profile.usable_capacity_kwh * 100.0

    def _consume(self, energy_kwh: float) -> None:
        """Draw ``energy_kwh`` from the pack, floored at an empty battery."""
        self.soc_percent = max(0.0, self.soc_percent - self._soc_delta(energy_kwh))

    def _update_battery_temperature(
        self,
        dt_s: float,
        ambient_c: float,
        energy_kwh: float,
    ) -> None:
        """Advance the pack temperature by one step.

        Three terms, all documented as module constants: exponential relaxation toward ambient,
        ohmic self-heating proportional to the energy that passed through the pack, and an
        active-cooling branch that only engages above :data:`PACK_ACTIVE_COOLING_TARGET_C`.
        """
        temperature = self.battery_temperature_c
        temperature += (ambient_c - temperature) / PACK_AMBIENT_TIME_CONSTANT_S * dt_s
        temperature += PACK_HEATING_C_PER_KWH * abs(energy_kwh)
        if temperature > PACK_ACTIVE_COOLING_TARGET_C:
            temperature -= (
                (temperature - PACK_ACTIVE_COOLING_TARGET_C)
                / PACK_ACTIVE_COOLING_TIME_CONSTANT_S
                * dt_s
            )
        self.battery_temperature_c = temperature

    def _update_rolling_consumption(self, distance_km: float, energy_kwh: float) -> None:
        """Fold one step into the distance-weighted consumption average behind the range."""
        if distance_km <= 0.0:
            return
        cap = self.profile.nominal_consumption_kwh_100km * MAX_ROLLING_CONSUMPTION_FACTOR
        sample = min(cap, energy_kwh / distance_km * 100.0)
        weight = 1.0 - 0.5 ** (distance_km / ROLLING_CONSUMPTION_HALF_LIFE_KM)
        self._rolling_kwh_100km = (1.0 - weight) * self._rolling_kwh_100km + weight * sample

    # -- event construction -------------------------------------------------------------

    def _telemetry_event(self, recorded_at: datetime, conditions: RoadConditions) -> TelemetryEvent:
        """Build the telemetry sample for this instant.

        ``instantaneous_power_kw`` comes from
        :meth:`~autotwin_ml.baseline.PhysicalEnergyModel.instantaneous_power_kw`, which is
        *signed*, so a recuperating vehicle reports negative power even though the integrated
        segment energy it is charged for is floored at zero. Charging reports the negative of the
        power actually flowing into the pack.
        """
        coordinate = self.environment.route.coordinate_at(self.offset_m)
        interval_distance_km = self._interval.distance_km
        interval_energy_kwh = self._interval.energy_kwh
        consumption_kwh_100km = (
            interval_energy_kwh / interval_distance_km * 100.0
            if interval_distance_km > 0.0
            else 0.0
        )
        if self.state is VehicleState.charging:
            power_kw = -self._charging_power_kw(conditions)
        else:
            power_kw = self._energy_model.instantaneous_power_kw(
                self.profile,
                self._segment_conditions(conditions, self.speed_kmh, 0.0, 1.0),
            )
        return TelemetryEvent(
            vehicle_id=self.vehicle_id,
            trip_id=self.trip_id,
            route_id=self.route_id,
            recorded_at=recorded_at,
            latitude=coordinate.latitude,
            longitude=coordinate.longitude,
            speed_kmh=max(0.0, self.speed_kmh),
            acceleration_ms2=self.acceleration_ms2,
            heading_deg=conditions.heading_deg % 360.0,
            battery_soc_percent=min(100.0, max(0.0, self.soc_percent)),
            battery_temperature_c=self.battery_temperature_c,
            outside_temperature_c=conditions.temperature_c,
            instantaneous_power_kw=power_kw,
            energy_consumption_kwh_100km=consumption_kwh_100km,
            cumulative_energy_kwh=max(0.0, self.cumulative_energy_kwh),
            estimated_range_km=self.estimated_range_km,
            road_class=conditions.road_class,
            speed_limit_kmh=conditions.speed_limit_kmh,
            odometer_m=max(0.0, self.odometer_m),
            state=self.state,
        )

    def _charging_power_kw(self, conditions: RoadConditions) -> float:
        """Net power currently flowing into the pack, in kW; zero when not charging."""
        vehicle_kw = charging_power_kw(self.profile, self.soc_percent, self.battery_temperature_c)
        available_kw = min(self._charging_station_power_kw, vehicle_kw)
        return max(0.0, available_kw - self._physics.auxiliary_power_kw(conditions.temperature_c))

    def _trip_event(
        self,
        event_type: TripEventType,
        when: datetime,
        *,
        station_id: str | None = None,
        reason: str | None = None,
    ) -> TripEvent:
        """Build a lifecycle event carrying the vehicle's position and trip totals."""
        coordinate = self.environment.route.coordinate_at(self.offset_m)
        return TripEvent(
            trip_id=self.trip_id or f"{self.vehicle_id}-{self.trip_token}-0000",
            vehicle_id=self.vehicle_id,
            event_type=event_type,
            occurred_at=when,
            route_id=self.route_id,
            latitude=coordinate.latitude,
            longitude=coordinate.longitude,
            soc_percent=min(100.0, max(0.0, self.soc_percent)),
            distance_m=max(0.0, self.odometer_m),
            energy_kwh=max(0.0, self.cumulative_energy_kwh),
            station_id=station_id,
            reason=reason,
        )
