"""Builders for a corridor the simulator can drive with no database and no network.

Everything the simulator needs about the world comes from a
:class:`~autotwin_simulator.environment.RouteEnvironment`, and every one of its inputs is
optional: without a session the weather, the traffic and the terrain are the seeded synthetic
models. That is what lets the physics be tested at full fidelity in milliseconds, and it is why
these helpers construct the value objects directly instead of reaching for a fixture database.

Two deliberate simplifications live here, and both are stated rather than hidden:

* **The corridor is a meridian.** A straight line due south, so the heading is constant and a
  headwind term cannot vary along the route. A test about speed or state of charge should not
  have to reason about the vehicle turning.
* **The terrain is switched off** by :class:`FlatEnvironment`. The synthetic gradient is a sum
  of three sinusoids (``GRADIENT_WAVES``) with a mean of zero over a corridor, so over a whole
  trip it cancels — but over any *prefix* of one it does not, and a test asserting that energy
  rises monotonically would then be asserting something about the phase of a sine wave. Tests
  that care about the gradient build a plain ``RouteEnvironment`` instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from autotwin_contracts import (
    ChargingCategory,
    Coordinate,
    RoadClass,
    VehicleProfile,
    VehicleState,
    get_vehicle_profile,
)
from autotwin_core.geo import cumulative_distances_m, densify
from autotwin_ml import ChargingCandidate
from autotwin_simulator.driver import Driver, DriverProfile, sample_driver_profile
from autotwin_simulator.environment import (
    EnvironmentConfig,
    RouteEnvironment,
    RouteSegmentProfile,
    SimulationRoute,
    TrafficIntensity,
    WeatherMode,
)
from autotwin_simulator.vehicle import SimulatedVehicle, VehicleStep

__all__ = [
    "CORRIDOR_ORIGIN",
    "SIMULATION_START",
    "FlatEnvironment",
    "build_driver",
    "build_environment",
    "build_vehicle",
    "charging_candidate",
    "drive",
    "straight_corridor",
]

CORRIDOR_ORIGIN = Coordinate(latitude=50.0, longitude=8.6)
"""North end of every synthetic corridor — just east of the Rhine-Main area, on a round number."""

SIMULATION_START = datetime(2026, 3, 10, 9, 0, tzinfo=UTC)
"""A fixed Tuesday morning in UTC.

Pinned because the synthetic climatology and the rush-hour model are both functions of the
instant: a test run at wall-clock time would silently change its own ambient temperature.
"""

_DEGREE_KM = 111.2
"""Kilometres per degree of latitude — used only to document the corridor lengths below."""


def straight_corridor(
    *,
    length_km: float = 100.0,
    road_class: RoadClass = RoadClass.motorway,
    speed_limit_kmh: float | None = 130.0,
    slug: str = "probe",
) -> SimulationRoute:
    """A corridor running due south from :data:`CORRIDOR_ORIGIN`, one segment throughout.

    ``duration_s`` is set from an assumed 110 km/h so that
    :attr:`~autotwin_simulator.environment.SimulationRoute.free_flow_speed_kmh` is a motorway
    figure; nothing in the vehicle model reads it, but the fallback road-class table does, and a
    corridor whose stated average speed disagreed with its segment would be a confusing fixture.
    """
    end = Coordinate(
        latitude=CORRIDOR_ORIGIN.latitude - length_km / _DEGREE_KM,
        longitude=CORRIDOR_ORIGIN.longitude,
    )
    coordinates = tuple(densify([CORRIDOR_ORIGIN, end], max_spacing_m=1_000.0))
    cumulative = tuple(cumulative_distances_m(list(coordinates)))
    length_m = cumulative[-1]
    return SimulationRoute(
        slug=slug,
        base_slug=slug,
        name=f"Probe {slug}",
        coordinates=coordinates,
        cumulative_m=cumulative,
        distance_m=length_m,
        duration_s=length_m / 1000.0 / 110.0 * 3600.0,
        segments=(
            RouteSegmentProfile(
                start_offset_m=0.0,
                distance_m=length_m,
                road_class=road_class,
                speed_limit_kmh=speed_limit_kmh,
            ),
        ),
    )


class FlatEnvironment(RouteEnvironment):
    """A corridor with the synthetic terrain switched off.

    The gradient is the only field of the environment that is *always* synthetic (there is no
    elevation source — see the module docstring of ``autotwin_simulator.environment``), so a
    test that wants a controlled experiment has to neutralise it explicitly rather than hope a
    seed produced a flat stretch.
    """

    __slots__ = ()

    def gradient_percent(self, offset_m: float) -> float:
        """Perfectly level road, everywhere."""
        return 0.0


def build_environment(
    *,
    length_km: float = 100.0,
    road_class: RoadClass = RoadClass.motorway,
    speed_limit_kmh: float | None = 130.0,
    weather_mode: WeatherMode = WeatherMode.mild,
    traffic_intensity: TrafficIntensity = TrafficIntensity.none,
    seed: int = 7,
    charging_candidates: Sequence[ChargingCandidate] = (),
    flat: bool = True,
) -> RouteEnvironment:
    """Resolve a synthetic corridor into an environment, with no session and no I/O.

    The defaults describe the reference case every physics test wants: level road, +20 °C dry
    still air (``WeatherMode.mild`` is the temperature at which the HVAC envelope is zero) and
    free-flowing traffic. Each one can be turned back on by a test that is about it.
    """
    route = straight_corridor(
        length_km=length_km,
        road_class=road_class,
        speed_limit_kmh=speed_limit_kmh,
    )
    config = EnvironmentConfig(
        seed=seed,
        weather_mode=weather_mode,
        traffic_intensity=traffic_intensity,
    )
    factory = FlatEnvironment if flat else RouteEnvironment
    return factory(route, config, charging_candidates=charging_candidates)


def build_driver(*, seed: int = 7, vehicle_id: str = "ATW-0001") -> Driver:
    """A driver with the temperament the fleet distribution draws for ``vehicle_id``."""
    return Driver(sample_driver_profile(seed, vehicle_id), seed=seed, vehicle_id=vehicle_id)


def build_vehicle(
    environment: RouteEnvironment,
    *,
    soc_percent: float = 90.0,
    seed: int = 7,
    vehicle_id: str = "ATW-0001",
    code: str = "sedan_ev",
    ambient_temperature_c: float = 20.0,
    driver_profile: DriverProfile | None = None,
) -> SimulatedVehicle:
    """One vehicle placed at the start of ``environment``, cold-soaked at the ambient."""
    driver = (
        Driver(driver_profile, seed=seed, vehicle_id=vehicle_id)
        if driver_profile is not None
        else build_driver(seed=seed, vehicle_id=vehicle_id)
    )
    profile: VehicleProfile = get_vehicle_profile(code)
    return SimulatedVehicle(
        vehicle_id=vehicle_id,
        profile=profile,
        environment=environment,
        driver=driver,
        seed=seed,
        trip_token="test",
        soc_percent=soc_percent,
        ambient_temperature_c=ambient_temperature_c,
    )


def charging_candidate(
    *,
    offset_km: float,
    max_power_kw: float = 150.0,
    station_id: str = "bnetza:test-1",
) -> ChargingCandidate:
    """A charging site on the corridor, as ``load_charging_candidates`` would return it."""
    return ChargingCandidate(
        station_id=station_id,
        name="Test Site",
        coordinate=CORRIDOR_ORIGIN,
        offset_km=offset_km,
        detour_km=0.4,
        max_power_kw=max_power_kw,
        operator="Testbetreiber",
        charging_category=ChargingCategory.ultra_fast,
    )


def drive(
    vehicle: SimulatedVehicle,
    environment: RouteEnvironment,
    *,
    ticks: int,
    tick_seconds: float = 10.0,
    physics_step_s: float = 1.0,
    start_time: datetime = SIMULATION_START,
    begin_trip: bool = True,
    stop_when_idle: bool = True,
) -> list[VehicleStep]:
    """Open a trip and advance the vehicle, returning every step it produced.

    Stops early once the vehicle has finished its corridor and gone idle, so a test that asks
    for more ticks than the corridor needs gets the trip and nothing after it.
    """
    if begin_trip:
        vehicle.begin_trip(environment, start_time)
    steps: list[VehicleStep] = []
    for index in range(ticks):
        steps.append(
            vehicle.advance(
                start_time=start_time + timedelta(seconds=tick_seconds * index),
                duration_s=tick_seconds,
                physics_step_s=physics_step_s,
            )
        )
        if stop_when_idle and vehicle.state is VehicleState.idle:
            break
    return steps
