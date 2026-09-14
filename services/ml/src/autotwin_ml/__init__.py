"""AutoTwin DE — physical energy baseline, charging optimisation and deterministic insights.

Importing this package pulls in **only** the pure-Python core: the road-load energy model
(:mod:`autotwin_ml.baseline`), the charging optimiser (:mod:`autotwin_ml.charging`) and the
explanation generator (:mod:`autotwin_ml.insights`). Nothing here imports LightGBM, SHAP,
NumPy, polars or joblib.

That is deliberate and load-bearing. The simulator calls the energy model on every vehicle tick
and the API imports it during start-up; if ``import autotwin_ml`` dragged in the gradient-
boosting stack, both would pay hundreds of milliseconds and ~200 MB of resident memory for code
they never execute. The heavier modules — ``features``, ``training``, ``inference``,
``registry``, ``explain``, ``cli`` — are imported from their own module paths by the code that
actually needs them, and they import *this* core, never the other way round.
"""

from __future__ import annotations

from autotwin_ml.baseline import PhysicalEnergyModel, headwind_component_ms
from autotwin_ml.charging import (
    CHARGING_OBJECTIVE,
    DEFAULT_BEAM_WIDTH,
    DEFAULT_MAX_STOPS,
    DEFAULT_MIN_SOC_PERCENT,
    ChargingCandidate,
    ChargingPlan,
    ChargingStop,
    charge_time_minutes,
    charging_power_kw,
    optimise_charging,
)
from autotwin_ml.constants import DEFAULT_PHYSICS, PhysicsConstants
from autotwin_ml.insights import (
    DriverDirection,
    EnergyDriver,
    EnergyExplanation,
    TrafficContext,
    WeatherContext,
    explain_route_energy,
)
from autotwin_ml.types import EnergyResult, RouteEnergySegment, SegmentConditions

__version__ = "0.1.0"

__all__ = [
    "CHARGING_OBJECTIVE",
    "DEFAULT_BEAM_WIDTH",
    "DEFAULT_MAX_STOPS",
    "DEFAULT_MIN_SOC_PERCENT",
    "DEFAULT_PHYSICS",
    "ChargingCandidate",
    "ChargingPlan",
    "ChargingStop",
    "DriverDirection",
    "EnergyDriver",
    "EnergyExplanation",
    "EnergyResult",
    "PhysicalEnergyModel",
    "PhysicsConstants",
    "RouteEnergySegment",
    "SegmentConditions",
    "TrafficContext",
    "WeatherContext",
    "__version__",
    "charge_time_minutes",
    "charging_power_kw",
    "explain_route_energy",
    "headwind_component_ms",
    "optimise_charging",
]
