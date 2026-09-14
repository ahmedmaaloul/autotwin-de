"""Simulation payloads: creating a run, describing it, and reporting what it is doing.

The vocabulary problem this module solves is worth stating. The web form offers weather as
*live / clear / rain / snow / storm* and traffic as *live / low / moderate / high / severe*,
because that is how an operator thinks about a demo. The engine models something narrower and
more honest: an ambient **temperature** regime (:class:`~autotwin_simulator.environment.
WeatherMode`) and a congestion **baseline** (:class:`~autotwin_simulator.environment.
TrafficIntensity`). It has no precipitation-type model at all.

:data:`WEATHER_MODE_ALIASES` and :data:`TRAFFIC_INTENSITY_ALIASES` map one onto the other, and
the response always reports the engine's own value — so an operator who asks for "storm" is told
in the payload that the run is executing ``synthetic``, rather than being left to believe in a
thunderstorm the physics never simulated (BUILD_SPEC §0.4).
"""

from __future__ import annotations

from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import Field, field_validator

from autotwin_api.schemas.common import ApiModel
from autotwin_contracts import SimulationState
from autotwin_simulator.environment import TrafficIntensity, WeatherMode

__all__ = [
    "TRAFFIC_INTENSITY_ALIASES",
    "WEATHER_MODE_ALIASES",
    "KafkaStatus",
    "SimulationCreate",
    "SimulationRunDetail",
    "SimulationRunSummary",
    "SimulatorStatus",
    "resolve_traffic_intensity",
    "resolve_weather_mode",
]

MAX_SIMULATED_VEHICLES: Final[int] = 500
"""Upper bound on a run started through the API.

The engine itself has no limit; this one exists because the API process runs the simulation
in-process beside the request loop, and a five-thousand-vehicle tick would starve every other
endpoint. A larger fleet belongs in the standalone CLI.
"""

WEATHER_MODE_ALIASES: Final[MappingProxyType[str, WeatherMode]] = MappingProxyType(
    {
        "live": WeatherMode.observed,
        "observed": WeatherMode.observed,
        "synthetic": WeatherMode.synthetic,
        "clear": WeatherMode.mild,
        "mild": WeatherMode.mild,
        "cold": WeatherMode.cold,
        "snow": WeatherMode.cold,
        "hot": WeatherMode.hot,
        # The engine models ambient temperature, not precipitation type. Rain and storm are
        # therefore mapped onto the seeded climatology, which does vary precipitation and wind
        # — the closest thing the physics actually has. The response reports `synthetic`.
        "rain": WeatherMode.synthetic,
        "storm": WeatherMode.synthetic,
    }
)

TRAFFIC_INTENSITY_ALIASES: Final[MappingProxyType[str, TrafficIntensity]] = MappingProxyType(
    {
        "live": TrafficIntensity.observed,
        "observed": TrafficIntensity.observed,
        "none": TrafficIntensity.none,
        "low": TrafficIntensity.light,
        "light": TrafficIntensity.light,
        "moderate": TrafficIntensity.moderate,
        "high": TrafficIntensity.heavy,
        "heavy": TrafficIntensity.heavy,
        "severe": TrafficIntensity.heavy,
    }
)


def resolve_weather_mode(value: str) -> WeatherMode:
    """Map a requested weather vocabulary onto the engine's, case-insensitively.

    Raises:
        ValueError: The value is neither an engine mode nor a known alias. Surfaced by Pydantic
            as ``422 validation_error`` listing what is accepted, because silently substituting
            a default would run a different experiment from the one that was asked for.
    """
    try:
        return WEATHER_MODE_ALIASES[value.strip().lower()]
    except KeyError:
        accepted = ", ".join(sorted(WEATHER_MODE_ALIASES))
        msg = f"unknown weather mode {value!r}; accepted values: {accepted}"
        raise ValueError(msg) from None


def resolve_traffic_intensity(value: str) -> TrafficIntensity:
    """Map a requested traffic vocabulary onto the engine's, case-insensitively.

    Raises:
        ValueError: The value is neither an engine intensity nor a known alias.
    """
    try:
        return TRAFFIC_INTENSITY_ALIASES[value.strip().lower()]
    except KeyError:
        accepted = ", ".join(sorted(TRAFFIC_INTENSITY_ALIASES))
        msg = f"unknown traffic intensity {value!r}; accepted values: {accepted}"
        raise ValueError(msg) from None


class SimulationCreate(ApiModel):
    """What ``POST /api/v1/simulations`` accepts.

    Everything except the fleet size has a default, and every default is deterministic: the same
    request twice produces the same telemetry, which is the property that makes a simulated
    training set defensible at all (BUILD_SPEC §10.2).
    """

    name: str = Field(
        default="AutoTwin Demo",
        min_length=1,
        max_length=120,
        description="Label for the run, shown in the run table.",
    )
    vehicle_count: int = Field(
        default=40,
        ge=1,
        le=MAX_SIMULATED_VEHICLES,
        description=f"Vehicles to simulate (1-{MAX_SIMULATED_VEHICLES}).",
    )
    speed_factor: float = Field(
        default=10.0,
        gt=0.0,
        le=120.0,
        description=(
            "Simulated seconds per wall-clock second. 1 is real time; 60 compresses an hour "
            "into a minute and is what the demo uses."
        ),
    )
    seed: int = Field(
        default=20_260_214,
        ge=0,
        description="Random seed. The same seed and configuration reproduce the run exactly.",
    )
    route_slugs: list[str] = Field(
        default_factory=list,
        description="Corridors to drive, e.g. `['frankfurt-stuttgart']`. Empty means all of them.",
    )
    weather_mode: str = Field(
        default="live",
        description=(
            "live | clear | rain | snow | storm, or an engine mode "
            "(observed | synthetic | cold | mild | hot). The engine models ambient temperature, "
            "not precipitation type; `rain` and `storm` therefore run the seeded climatology and "
            "the response reports `synthetic`."
        ),
    )
    traffic_intensity: str = Field(
        default="live",
        description=(
            "live | low | moderate | high | severe, or an engine intensity "
            "(observed | none | light | moderate | heavy)."
        ),
    )
    vehicle_mix: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Share of each vehicle profile, e.g. `{'compact_ev': 0.6, 'suv_ev': 0.4}`. "
            "Normalised by the engine; empty means the default German fleet mix."
        ),
    )
    duration_s: float | None = Field(
        default=None,
        gt=0.0,
        description="Simulated seconds to run for. Null runs until the operator stops it.",
    )

    @field_validator("vehicle_mix")
    @classmethod
    def _mix_must_be_non_negative(cls, value: dict[str, float]) -> dict[str, float]:
        """Reject a negative share rather than letting it cancel another profile out."""
        negative = sorted(code for code, share in value.items() if share < 0.0)
        if negative:
            msg = f"vehicle_mix shares must be >= 0; negative for: {', '.join(negative)}"
            raise ValueError(msg)
        return value

    @field_validator("weather_mode")
    @classmethod
    def _known_weather_mode(cls, value: str) -> str:
        """Fail fast on an unknown weather vocabulary."""
        resolve_weather_mode(value)
        return value

    @field_validator("traffic_intensity")
    @classmethod
    def _known_traffic_intensity(cls, value: str) -> str:
        """Fail fast on an unknown traffic vocabulary."""
        resolve_traffic_intensity(value)
        return value


class SimulationRunSummary(ApiModel):
    """A row of ``simulation_runs`` — one execution of the simulator."""

    id: UUID = Field(..., description="Run identifier.")
    name: str = Field(..., description="Operator's label for the run.")
    state: SimulationState = Field(
        ...,
        description="pending, running, paused, stopping, stopped, completed or failed.",
    )
    vehicle_count: int = Field(..., ge=0, description="Vehicles the run was configured with.")
    speed_factor: float = Field(..., description="Simulated seconds per wall-clock second.")
    seed: int = Field(..., description="Seed that makes the run reproducible.")
    started_at: datetime | None = Field(
        default=None,
        description="When it started (UTC); null while pending.",
    )
    stopped_at: datetime | None = Field(
        default=None,
        description="When it reached a terminal state (UTC).",
    )
    events_emitted: int = Field(..., ge=0, description="Telemetry samples produced so far.")
    errors: int = Field(..., ge=0, description="Vehicle ticks that raised and were skipped.")


class SimulationRunDetail(SimulationRunSummary):
    """A run with the full configuration it was created from."""

    weather_mode: str = Field(..., description="The engine's weather mode for this run.")
    traffic_intensity: str = Field(..., description="The engine's traffic intensity.")
    vehicle_mix: dict[str, float] = Field(
        default_factory=dict,
        description="Share of each vehicle profile in the fleet.",
    )
    route_slugs: list[str] = Field(
        default_factory=list,
        description="Corridors driven; empty means every corridor in the database.",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="The complete engine configuration, as persisted — enough to rerun it.",
    )
    created_at: datetime = Field(..., description="When the run was created (UTC).")
    is_current: bool = Field(
        default=False,
        description="Whether this run is the one this API process is currently driving.",
    )


class KafkaStatus(ApiModel):
    """Broker state, when Kafka is the configured transport."""

    connected: bool = Field(
        ...,
        description="Whether the broker answered a metadata request within the probe budget.",
    )
    bootstrap_servers: str = Field(..., description="Configured bootstrap servers.")
    topics: list[str] = Field(
        default_factory=list,
        description="Topics AutoTwin publishes to (BUILD_SPEC §8).",
    )


class SimulatorStatus(ApiModel):
    """What the simulator in *this* process is doing right now.

    Counters come from the engine and from the sink, not from the database: they are the
    transport's own view of what it has accepted, and reporting the database's view instead
    would hide precisely the case this endpoint exists to reveal — a simulator producing events
    faster than they are being persisted.
    """

    state: SimulationState = Field(
        ...,
        description="Lifecycle state; `pending` when no run has been started in this process.",
    )
    run_id: UUID | None = Field(
        default=None,
        description="The `simulation_runs` row being driven, null when idle.",
    )
    vehicles_active: int = Field(..., ge=0, description="Vehicles currently on the road.")
    events_per_second: float = Field(
        ...,
        ge=0.0,
        description="Telemetry throughput measured against the **wall** clock.",
    )
    messages_processed: int = Field(
        ...,
        ge=0,
        description="Telemetry samples the engine has handed to the transport.",
    )
    db_writes: int = Field(
        ...,
        ge=0,
        description=(
            "Samples that reached PostgreSQL. With the database sink this is the sink's own "
            "count; with Kafka it is counted from the `telemetry` table, because the rows are "
            "written by the consumer process and not by this one."
        ),
    )
    errors: int = Field(..., ge=0, description="Errors the engine recorded.")
    transport: Literal["kafka", "database"] = Field(
        ...,
        description="Which transport is actually carrying telemetry, not which was configured.",
    )
    kafka: KafkaStatus | None = Field(
        default=None,
        description="Broker detail; null when telemetry is written straight to PostgreSQL.",
    )
    degraded_reason: str | None = Field(
        default=None,
        description=(
            "Why Kafka was configured but not used, e.g. 'broker at localhost:19092 did not "
            "answer'. Null on the happy path (BUILD_SPEC §0.3)."
        ),
    )
    simulated_time: datetime | None = Field(
        default=None,
        description="Where the run's simulated clock has reached (UTC).",
    )
    last_error: str | None = Field(
        default=None,
        description="The most recent error the engine recorded, if any.",
    )
