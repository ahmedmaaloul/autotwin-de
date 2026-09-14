"""Response models for ``GET /api/v1/traffic/events`` and ``/events/geojson`` (BUILD_SPEC §7).

A traffic event has two useful geometries and both are carried: ``latitude``/``longitude`` is
the representative point every list view and every marker needs, while ``geometry`` is the
published extent of the disruption — the stretch of carriageway a 14 km roadworks zone actually
occupies. The list endpoint returns both; the GeoJSON endpoint picks the line where there is one
and falls back to the point, because a map that draws a 14 km Baustelle as a single pin
understates it badly.

The GeoJSON payload itself is :class:`~autotwin_contracts.geo.GeoJSONFeatureCollection`, shared
with the other map endpoints, so every layer in the frontend consumes one shape.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from autotwin_api.schemas.common import ApiModel, ProvenanceOut
from autotwin_contracts import GeoJSONLineString, TrafficEventType, TrafficSeverity

__all__ = ["TrafficEventOut"]


class TrafficEventOut(ApiModel):
    """One disruption as published by Autobahn GmbH, normalised onto AutoTwin's enums."""

    id: UUID = Field(..., description="Primary key of the event row.")
    external_id: str | None = Field(
        default=None,
        description="Identifier in the source system; stable across polls, so the frontend "
        "can keep a marker selected while the feed refreshes.",
    )
    event_type: TrafficEventType = Field(
        ...,
        description="roadworks, closure, incident, warning, congestion or other.",
        examples=["roadworks"],
    )
    severity: TrafficSeverity = Field(
        ...,
        description="low, moderate, high or severe. Drives the per-segment delay factor used "
        "by the route analysis (BUILD_SPEC §7.3).",
        examples=["high"],
    )
    road_name: str | None = Field(
        default=None,
        description="Road the event is on, e.g. 'A5'.",
        examples=["A5"],
    )
    direction: str | None = Field(
        default=None,
        description="Carriageway direction as published, e.g. 'Frankfurt -> Kassel'.",
    )
    title: str = Field(..., description="Short German headline of the report.")
    description: str | None = Field(default=None, description="Full German report text.")
    latitude: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Latitude of the representative point in WGS 84 decimal degrees.",
    )
    longitude: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Longitude of the representative point in WGS 84 decimal degrees.",
    )
    geometry: GeoJSONLineString | None = Field(
        default=None,
        description="Extent of the disruption as an RFC 7946 LineString, when the source "
        "publishes one; null for an event reported as a single point.",
    )
    starts_at: datetime | None = Field(
        default=None,
        description="Validity start (UTC); null when the source states none.",
    )
    ends_at: datetime | None = Field(
        default=None,
        description="Validity end (UTC); null for an open-ended report.",
    )
    is_blocked: bool = Field(
        ...,
        description="Whether the carriageway is fully closed (Vollsperrung).",
    )
    delay_minutes: float | None = Field(
        default=None,
        description="Delay in minutes where the source quantifies one.",
    )
    provenance: ProvenanceOut | None = Field(
        default=None,
        description="Where the row came from (BUILD_SPEC §3.1).",
    )
