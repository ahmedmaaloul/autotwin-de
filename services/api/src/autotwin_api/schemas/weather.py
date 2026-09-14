"""Response models for ``GET /api/v1/weather`` and ``/weather/latest`` (BUILD_SPEC §7).

One model, deliberately. A weather observation is the same fact whether it is read from a
bounding box, from a radius around a point or as "the nearest one to here", so splitting it
into a list DTO and a detail DTO would only create two places for the units to drift apart.

Field names and nullability mirror ``apps/web/types/domain.ts`` → ``WeatherObservation``.
Every measurement is nullable because the DWD publishes partial rows: a station that reports a
temperature but no wind is normal, and coercing the missing wind to ``0.0`` would turn a sensor
outage into a confident statement about a calm day.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from autotwin_api.schemas.common import ApiModel, ProvenanceOut
from autotwin_contracts import WeatherCondition

__all__ = ["WeatherObservationOut"]


class WeatherObservationOut(ApiModel):
    """One DWD observation, positioned at the station that reported it."""

    id: UUID = Field(..., description="Primary key of the observation row.")
    observed_at: datetime = Field(
        ...,
        description="When the observation is valid (UTC), as published by the DWD.",
        examples=["2026-09-14T15:20:00Z"],
    )
    latitude: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Latitude of the reporting station in WGS 84 decimal degrees.",
    )
    longitude: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Longitude of the reporting station in WGS 84 decimal degrees.",
    )
    station_name: str | None = Field(
        default=None,
        description="DWD station name, e.g. 'Stuttgart (Schnarrenberg)'; null for an "
        "observation that is not linked to a station row.",
        examples=["Stuttgart (Schnarrenberg)"],
    )
    temperature_c: float | None = Field(
        default=None,
        description="Air temperature 2 m above ground in °C — the strongest weather driver of "
        "EV consumption (BUILD_SPEC §10.1).",
        examples=[23.9],
    )
    precipitation_mm: float | None = Field(
        default=None,
        description="Precipitation in the reporting interval, in mm.",
    )
    wind_speed_ms: float | None = Field(
        default=None,
        description="Mean wind speed in m/s.",
    )
    wind_gust_ms: float | None = Field(
        default=None,
        description="Peak gust in the reporting interval, in m/s.",
    )
    humidity_percent: float | None = Field(
        default=None,
        description="Relative humidity in percent.",
    )
    pressure_hpa: float | None = Field(
        default=None,
        description="Air pressure in hPa.",
    )
    condition: WeatherCondition = Field(
        ...,
        description="Coarse condition derived from the observed parameters; `unknown` when "
        "the reported parameters do not allow a classification.",
        examples=["clear"],
    )
    distance_km: float | None = Field(
        default=None,
        description="Great-circle distance from the queried point to the reporting station, "
        "in kilometres. Present only when the query named a point (`lat`/`lon` or "
        "`/weather/latest`); null for a bounding-box query, which has no single reference.",
        examples=[3.4],
    )
    provenance: ProvenanceOut | None = Field(
        default=None,
        description="Where the row came from (BUILD_SPEC §3.1).",
    )
