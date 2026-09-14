"""The landing page's payload (BUILD_SPEC §7.1).

One response, because the dashboard is one screen: eight tiles that must agree with each other.
Fetching them separately would let the vehicle count come from a moment the energy trend does
not cover, and a dashboard whose figures disagree is worse than a slow one.

:class:`DashboardTrafficEvent` restates the traffic-event shape rather than importing the
traffic router's model. That is deliberate coupling avoidance: the dashboard must render even
while another router module is being rewritten, and the app factory's import isolation only
holds if this module imports nothing from its siblings. The field names are the frontend's
``TrafficEvent`` exactly, so the two are interchangeable on the wire.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from autotwin_api.schemas.common import ApiModel
from autotwin_contracts import (
    ChargingCategory,
    DataOrigin,
    GeoJSONLineString,
    IngestionStatus,
    ProviderMode,
    SourceSystem,
    TrafficEventType,
    TrafficSeverity,
    WeatherCondition,
)

__all__ = [
    "ChargingCategoryCount",
    "DashboardSummary",
    "DashboardTrafficEvent",
    "DataFreshnessEntry",
    "EnergyTrendPoint",
    "WeatherSnapshot",
]


class DataFreshnessEntry(ApiModel):
    """How current one source is — the row behind the dashboard's freshness strip."""

    source: SourceSystem = Field(..., description="The source system.")
    last_run_at: datetime | None = Field(
        default=None,
        description="Start of its most recent ingestion run (UTC).",
    )
    status: IngestionStatus = Field(
        ...,
        description="healthy | delayed | degraded | failed | simulation.",
    )
    age_minutes: float | None = Field(
        default=None,
        description="Minutes since that run started; null when the source has never run.",
    )
    data_origin: DataOrigin = Field(
        ...,
        description="official for ingested sources, simulated for the simulator.",
    )
    mode: ProviderMode | None = Field(
        default=None,
        description="How the last run obtained its data: live, cache or fixture.",
    )


class EnergyTrendPoint(ApiModel):
    """Mean fleet consumption in one time bucket."""

    bucket: datetime = Field(..., description="Start of the bucket (UTC), hourly.")
    kwh_100km: float = Field(
        ...,
        description="Mean consumption of moving vehicles in that hour, kWh/100 km.",
    )


class DashboardTrafficEvent(ApiModel):
    """A traffic disruption, in the frontend's ``TrafficEvent`` shape."""

    id: UUID = Field(..., description="Primary key in `traffic_events`.")
    external_id: str | None = Field(
        default=None,
        description="Identifier in the Autobahn GmbH API.",
    )
    event_type: TrafficEventType = Field(
        ...,
        description="roadworks, closure, incident, warning, congestion or other.",
    )
    severity: TrafficSeverity = Field(..., description="low, moderate, high or severe.")
    road_name: str | None = Field(default=None, description="Road identifier, e.g. `A5`.")
    direction: str | None = Field(
        default=None,
        description="Direction of travel affected, as the publisher words it.",
    )
    title: str = Field(..., description="Short description from the source.")
    description: str | None = Field(default=None, description="Full text from the source.")
    latitude: float = Field(..., description="Latitude of the event's reference point (WGS 84).")
    longitude: float = Field(..., description="Longitude of the event's reference point (WGS 84).")
    geometry: GeoJSONLineString | None = Field(
        default=None,
        description="Affected stretch as a GeoJSON LineString, when the source supplies one.",
    )
    starts_at: datetime | None = Field(default=None, description="Start of validity (UTC).")
    ends_at: datetime | None = Field(default=None, description="End of validity (UTC).")
    is_blocked: bool = Field(..., description="Whether the road is fully closed.")
    delay_minutes: float | None = Field(
        default=None,
        description="Expected delay in minutes, when the source estimates one.",
    )


class WeatherSnapshot(ApiModel):
    """The most recent DWD observation held, as one headline figure."""

    temperature_c: float | None = Field(default=None, description="Air temperature in °C.")
    condition: WeatherCondition = Field(
        ...,
        description="clear, clouds, rain, snow, fog, storm or unknown.",
    )
    observed_at: datetime | None = Field(
        default=None,
        description="When the station recorded it (UTC).",
    )
    station: str | None = Field(default=None, description="DWD station name, when known.")


class ChargingCategoryCount(ApiModel):
    """Charging sites in one power class (BUILD_SPEC §2, ``ChargingCategory``)."""

    category: ChargingCategory = Field(
        ...,
        description="normal (<22 kW), fast (22-149 kW) or ultra_fast (>=150 kW).",
    )
    count: int = Field(..., ge=0, description="Number of sites in the class.")


class DashboardSummary(ApiModel):
    """Everything the landing page renders, from one consistent read of the database."""

    vehicles_active: int = Field(
        ...,
        ge=0,
        description=(
            "Simulated vehicles that reported telemetry in the last 15 minutes. Measured from "
            "the telemetry itself rather than from `vehicles.state`, so a run that died without "
            "closing its rows cannot leave the dashboard claiming a live fleet."
        ),
    )
    charging_stations_total: int = Field(
        ...,
        ge=0,
        description="Charging sites held from the Bundesnetzagentur register.",
    )
    fast_charging_points_total: int = Field(
        ...,
        ge=0,
        description="Individual charging points rated at 50 kW or more.",
    )
    traffic_events_active: int = Field(
        ...,
        ge=0,
        description="Traffic events whose validity window contains the current moment.",
    )
    avg_consumption_kwh_100km: float | None = Field(
        default=None,
        description=(
            "Mean consumption of moving simulated vehicles over the last 24 hours, kWh/100 km. "
            "Null when no vehicle has driven in that window."
        ),
    )
    data_freshness: list[DataFreshnessEntry] = Field(
        default_factory=list,
        description="One entry per source that has ever been ingested, newest run first.",
    )
    energy_trend: list[EnergyTrendPoint] = Field(
        default_factory=list,
        description="Hourly mean consumption over the last 24 hours, oldest first.",
    )
    recent_traffic: list[DashboardTrafficEvent] = Field(
        default_factory=list,
        description="The five most recently starting traffic events.",
    )
    weather_snapshot: WeatherSnapshot | None = Field(
        default=None,
        description="Latest DWD observation held; null when none has been ingested.",
    )
    charging_by_category: list[ChargingCategoryCount] = Field(
        default_factory=list,
        description="Site counts per power class, largest first.",
    )
    generated_at: datetime = Field(
        ...,
        description="When this snapshot was taken (UTC). Every figure above is as of this time.",
    )
