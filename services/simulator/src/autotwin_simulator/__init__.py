"""AutoTwin DE — the connected-vehicle digital twin (BUILD_SPEC §9, ADR 004).

No public feed of German EV telemetry exists: OEM telematics is proprietary and carries personal
data. AutoTwin therefore **simulates** it, from physics rather than from noise, and labels every
row it produces as simulated at every layer — database, Kafka envelope, API response and UI
badge. ADR 004 sets that out in full; this package is its implementation.

The module layout follows the causal chain of one simulated second:

.. code-block:: text

    environment  where am I?   road class, limit, gradient, weather, traffic, chargers
         |
    driver       how fast do I want to go, and how do I get there?
         |
    vehicle      what does that cost?   PhysicalEnergyModel -> SOC, pack temperature, range
         |
    engine       N vehicles, a clock, a sink, and the SimulationState lifecycle
         |
    runner       one asyncio task, started by FastAPI or by the CLI, stopped cleanly
         |
    training_data   60-second windows -> data/gold/training/energy_windows.parquet

Consumption is **not** computed here. It comes from
:class:`autotwin_ml.baseline.PhysicalEnergyModel` — the same object the API serves route
predictions from — so that a model validated against this data was validated against the thing
that generated it, and the circularity is visible rather than hidden.

Importing this package pulls in :mod:`autotwin_ml`, :mod:`autotwin_streaming`,
:mod:`autotwin_core` and :mod:`polars`, but no training or explainability stack: LightGBM, SHAP
and scikit-learn stay unimported.

Every assumption, every seeded random element and every validity limit is written down in
``docs/data/simulation.md``. **Nothing this package produces may be presented as real fleet
telemetry.**
"""

from __future__ import annotations

from autotwin_simulator.driver import (
    CRUISE_SPEED_KMH,
    Driver,
    DriverProfile,
    sample_driver_profile,
)
from autotwin_simulator.engine import (
    DEFAULT_VEHICLE_MIX,
    DatabaseSimulationStore,
    EngineStats,
    NullSimulationStore,
    SimulationClock,
    SimulationConfig,
    SimulationEngine,
    SimulationStore,
    build_environments,
    load_routes_for,
    resolve_vehicle_mix,
)
from autotwin_simulator.environment import (
    EnvironmentConfig,
    RoadConditions,
    RouteEnvironment,
    RouteSegmentProfile,
    SimulationRoute,
    TrafficIntensity,
    WeatherMode,
    build_route_environment,
    load_simulation_routes,
    seeded_rng,
)
from autotwin_simulator.runner import (
    SimulationRunner,
    get_runner,
    run_standalone,
    set_runner,
    shutdown_runner,
)
from autotwin_simulator.training_data import (
    DEFAULT_TRAINING_DATA_PATH,
    TRAINING_SCHEMA,
    WINDOW_SECONDS,
    TrainingDataResult,
    TrainingWindow,
    WindowAggregator,
    build_generator_engine,
    generate_seasonal_training_data,
    generate_training_data,
)
from autotwin_simulator.vehicle import (
    IntervalStatistics,
    SimulatedVehicle,
    TelemetrySample,
    VehicleStep,
)

__version__ = "0.1.0"

__all__ = [
    "CRUISE_SPEED_KMH",
    "DEFAULT_TRAINING_DATA_PATH",
    "DEFAULT_VEHICLE_MIX",
    "TRAINING_SCHEMA",
    "WINDOW_SECONDS",
    "DatabaseSimulationStore",
    "Driver",
    "DriverProfile",
    "EngineStats",
    "EnvironmentConfig",
    "IntervalStatistics",
    "NullSimulationStore",
    "RoadConditions",
    "RouteEnvironment",
    "RouteSegmentProfile",
    "SimulatedVehicle",
    "SimulationClock",
    "SimulationConfig",
    "SimulationEngine",
    "SimulationRoute",
    "SimulationRunner",
    "SimulationStore",
    "TelemetrySample",
    "TrafficIntensity",
    "TrainingDataResult",
    "TrainingWindow",
    "VehicleStep",
    "WeatherMode",
    "WindowAggregator",
    "__version__",
    "build_environments",
    "build_generator_engine",
    "build_route_environment",
    "generate_seasonal_training_data",
    "generate_training_data",
    "get_runner",
    "load_routes_for",
    "load_simulation_routes",
    "resolve_vehicle_mix",
    "run_standalone",
    "sample_driver_profile",
    "seeded_rng",
    "set_runner",
    "shutdown_runner",
]
