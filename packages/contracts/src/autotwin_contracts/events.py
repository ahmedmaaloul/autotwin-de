"""Kafka/Redpanda topics, the message envelope and the four payload types (BUILD_SPEC §8).

Messages are JSON (UTF-8). The ADR for that choice is short: a schema registry would add an
operational component to a laptop-scale deployment without a consumer that needs it, so the
compatibility contract lives in :attr:`EventEnvelope.schema_version` instead.

Producers and consumers import the topic names and payload models from here rather than
spelling either out, so a topic rename is a one-line change with a compiler behind it.

The envelope and its payloads are frozen: an event is a statement about something that
already happened, and a consumer that mutates one in flight would silently diverge from the
bytes it committed an offset for.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from autotwin_contracts.enums import (
    ConnectorType,
    DataOrigin,
    RoadClass,
    TrafficEventType,
    TrafficSeverity,
    VehicleState,
)
from autotwin_contracts.geo import Coordinate
from autotwin_contracts.temporal import UtcDatetime, utc_now

__all__ = [
    "ALL_TOPICS",
    "EVENT_TYPE_CHARGING",
    "EVENT_TYPE_TELEMETRY",
    "EVENT_TYPE_TRAFFIC",
    "EVENT_TYPE_TRIP",
    "SCHEMA_VERSION",
    "TOPIC_CHARGING_EVENTS",
    "TOPIC_TELEMETRY",
    "TOPIC_TRAFFIC_EVENTS",
    "TOPIC_TRIP_EVENTS",
    "ChargingEvent",
    "ChargingEventEnvelope",
    "ChargingEventType",
    "EventEnvelope",
    "TelemetryEnvelope",
    "TelemetryEvent",
    "TrafficEventEnvelope",
    "TrafficEventMessage",
    "TripEnvelope",
    "TripEvent",
    "TripEventType",
]

TOPIC_TELEMETRY: Final[str] = "vehicle.telemetry.v1"
"""High-volume vehicle telemetry, keyed by ``vehicle_id``, retained 6 h."""

TOPIC_TRIP_EVENTS: Final[str] = "vehicle.trip-events.v1"
"""Trip lifecycle transitions, keyed by ``trip_id``, retained 24 h."""

TOPIC_TRAFFIC_EVENTS: Final[str] = "traffic.events.v1"
"""Traffic disruptions fanned out to consumers, keyed by ``external_id``, retained 24 h."""

TOPIC_CHARGING_EVENTS: Final[str] = "charging.events.v1"
"""Charging sessions at a site, keyed by ``station_id``, retained 24 h."""

ALL_TOPICS: Final[tuple[str, ...]] = (
    TOPIC_TELEMETRY,
    TOPIC_TRIP_EVENTS,
    TOPIC_TRAFFIC_EVENTS,
    TOPIC_CHARGING_EVENTS,
)
"""Every topic AutoTwin owns — the input of ``infra/scripts/create-topics.sh``."""

EVENT_TYPE_TELEMETRY: Final[str] = "vehicle.telemetry"
EVENT_TYPE_TRIP: Final[str] = "vehicle.trip"
EVENT_TYPE_TRAFFIC: Final[str] = "traffic.event"
EVENT_TYPE_CHARGING: Final[str] = "charging.event"

SCHEMA_VERSION: Final[int] = 1
"""Current envelope schema version; bump only on a breaking payload change."""


class EventEnvelope[PayloadT](BaseModel):
    """The envelope wrapped around every message on every topic.

    Generic over its payload so that a consumer of ``vehicle.telemetry.v1`` can declare
    ``EventEnvelope[TelemetryEvent]`` and have Pydantic validate the payload in the same pass
    as the envelope — the alternative, validating ``dict`` payloads afterwards, is where
    schema drift hides.
    """

    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(
        default_factory=uuid4,
        description="Unique id of this message; the deduplication key for consumers.",
    )
    event_type: str = Field(
        ...,
        description="Dotted event name, e.g. 'vehicle.telemetry'. Independent of the topic name.",
    )
    schema_version: int = Field(
        default=SCHEMA_VERSION,
        ge=1,
        description="Envelope/payload schema version, bumped on breaking changes.",
    )
    occurred_at: UtcDatetime = Field(
        default_factory=utc_now,
        description="When the event happened in the producing system (UTC), not when it was sent.",
    )
    producer: str = Field(
        ...,
        min_length=1,
        description="Producing component, e.g. 'autotwin-simulator' or 'autotwin-ingestion'.",
    )
    data_origin: DataOrigin = Field(
        ...,
        description="Honesty marker travelling with the payload: official, simulated or derived.",
    )
    payload: PayloadT = Field(..., description="The typed event body.")

    @classmethod
    def wrap(
        cls,
        payload: PayloadT,
        *,
        event_type: str,
        producer: str,
        data_origin: DataOrigin = DataOrigin.simulated,
        occurred_at: UtcDatetime | None = None,
    ) -> Self:
        """Wrap a payload, defaulting ``occurred_at`` to now and the origin to ``simulated``.

        The default origin is deliberate: everything AutoTwin *publishes* on a topic today is
        produced by the simulator, and a producer that emits official data has to say so.
        """
        return cls(
            event_type=event_type,
            producer=producer,
            data_origin=data_origin,
            occurred_at=occurred_at if occurred_at is not None else utc_now(),
            payload=payload,
        )


class TelemetryEvent(BaseModel):
    """One telemetry sample of one simulated vehicle.

    Field-for-field the ``telemetry`` table of BUILD_SPEC §3.2, so the streaming consumer can
    bulk-insert a validated batch without a translation layer. Latitude/longitude are flat
    floats rather than a nested :class:`Coordinate` because that is the table's shape and
    because it keeps the hottest message on the bus small; :attr:`coordinate` reconstructs the
    value object where code wants one.
    """

    model_config = ConfigDict(frozen=True)

    vehicle_id: str = Field(
        ...,
        min_length=1,
        description="Human vehicle identifier, e.g. 'ATW-0042'. Partition key of the topic.",
    )
    trip_id: str | None = Field(
        default=None,
        description="Identifier of the trip in progress; null while the vehicle is idle.",
    )
    route_id: UUID | None = Field(
        default=None,
        description="Corridor the vehicle is driving, when it follows a persisted route.",
    )
    recorded_at: UtcDatetime = Field(..., description="Simulation timestamp of the sample (UTC).")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="WGS 84 latitude in degrees.")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="WGS 84 longitude in degrees.")
    speed_kmh: float = Field(..., ge=0.0, description="Ground speed in km/h.")
    acceleration_ms2: float = Field(
        ...,
        description="Longitudinal acceleration in m/s²; negative while braking or recuperating.",
    )
    heading_deg: float | None = Field(
        default=None,
        ge=0.0,
        lt=360.0,
        description="Course over ground in degrees clockwise from north.",
    )
    battery_soc_percent: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="State of charge in percent of usable capacity.",
    )
    battery_temperature_c: float = Field(
        ...,
        description="Battery temperature in °C; drives the cold-battery efficiency penalty.",
    )
    outside_temperature_c: float = Field(
        ...,
        description="Ambient temperature in °C at the vehicle's position.",
    )
    instantaneous_power_kw: float = Field(
        ...,
        description="Battery power in kW; negative during recuperation.",
    )
    energy_consumption_kwh_100km: float = Field(
        ...,
        description="Momentary consumption in kWh/100 km — the ML target variable.",
    )
    cumulative_energy_kwh: float = Field(
        ...,
        ge=0.0,
        description="Net energy drawn from the battery since the start of the trip, in kWh.",
    )
    estimated_range_km: float = Field(
        ...,
        ge=0.0,
        description="Remaining range in km at the current consumption rate.",
    )
    road_class: RoadClass = Field(
        default=RoadClass.unknown,
        description="Functional class of the road under the vehicle.",
    )
    speed_limit_kmh: float | None = Field(
        default=None,
        gt=0.0,
        description="Posted limit in km/h; null on unrestricted Autobahn stretches.",
    )
    odometer_m: float = Field(
        ...,
        ge=0.0,
        description="Distance travelled on this trip, in metres.",
    )
    state: VehicleState = Field(..., description="Vehicle state at the moment of the sample.")

    @property
    def coordinate(self) -> Coordinate:
        """The sample position as the validated value object."""
        return Coordinate(latitude=self.latitude, longitude=self.longitude)


class TripEventType(StrEnum):
    """Lifecycle transitions published on ``vehicle.trip-events.v1``."""

    started = "started"
    finished = "finished"
    charging_started = "charging_started"
    charging_finished = "charging_finished"
    paused = "paused"


class TripEvent(BaseModel):
    """A trip lifecycle transition — the low-volume counterpart of the telemetry stream.

    Consumers use it to close out ``trips`` aggregates without having to detect edges in the
    telemetry stream, which is exactly the kind of derived state that goes wrong at scale.
    """

    model_config = ConfigDict(frozen=True)

    trip_id: str = Field(..., min_length=1, description="Trip identifier. Partition key.")
    vehicle_id: str = Field(..., min_length=1, description="Vehicle the trip belongs to.")
    event_type: TripEventType = Field(..., description="Which transition occurred.")
    occurred_at: UtcDatetime = Field(..., description="Simulation time of the transition (UTC).")
    route_id: UUID | None = Field(default=None, description="Corridor being driven, if any.")
    latitude: float | None = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description="Where the transition happened (latitude).",
    )
    longitude: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="Where the transition happened (longitude).",
    )
    soc_percent: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="State of charge at the transition, in percent.",
    )
    distance_m: float | None = Field(
        default=None,
        ge=0.0,
        description="Trip distance so far in metres.",
    )
    energy_kwh: float | None = Field(
        default=None,
        description="Net energy used so far on the trip, in kWh.",
    )
    station_id: str | None = Field(
        default=None,
        description="Charging site involved, for the charging_* transitions.",
    )
    reason: str | None = Field(
        default=None,
        description="Why the transition happened, e.g. 'soc_below_reserve'.",
    )


class TrafficEventMessage(BaseModel):
    """A traffic disruption published on ``traffic.events.v1``.

    A flattened projection of :class:`~autotwin_contracts.records.TrafficEventRecord`: the bus
    carries what a consumer needs to re-route or annotate a segment, not the raw source blob.
    """

    model_config = ConfigDict(frozen=True)

    external_id: str = Field(
        ...,
        min_length=1,
        description="Identifier in the source system. Partition key of the topic.",
    )
    event_type: TrafficEventType = Field(..., description="Kind of disruption.")
    severity: TrafficSeverity = Field(..., description="Expected impact on travel time.")
    road_name: str | None = Field(default=None, description="Road designation, e.g. 'A5'.")
    direction: str | None = Field(default=None, description="Fahrtrichtung as published.")
    title: str = Field(..., min_length=1, description="Short German headline.")
    description: str | None = Field(default=None, description="Full German report text.")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="Representative point (latitude).")
    longitude: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Representative point (longitude).",
    )
    starts_at: UtcDatetime | None = Field(default=None, description="Validity start (UTC).")
    ends_at: UtcDatetime | None = Field(default=None, description="Validity end (UTC).")
    is_blocked: bool = Field(default=False, description="Whether the road is fully blocked.")
    delay_minutes: float | None = Field(
        default=None,
        ge=0.0,
        description="Reported delay in minutes, if quantified by the source.",
    )
    observed_at: UtcDatetime = Field(
        default_factory=utc_now,
        description="When AutoTwin observed this version of the report (UTC).",
    )

    @property
    def coordinate(self) -> Coordinate:
        """The event position as the validated value object."""
        return Coordinate(latitude=self.latitude, longitude=self.longitude)


class ChargingEventType(StrEnum):
    """Phases of a charging session on ``charging.events.v1``."""

    session_started = "session_started"
    session_updated = "session_updated"
    session_finished = "session_finished"


class ChargingEvent(BaseModel):
    """A charging session phase at one site.

    Keyed by station rather than by vehicle so that site occupancy and throughput can be
    aggregated straight off the topic, without joining the high-volume telemetry stream.
    """

    model_config = ConfigDict(frozen=True)

    station_id: str = Field(
        ...,
        min_length=1,
        description="external_id of the charging site. Partition key of the topic.",
    )
    event_type: ChargingEventType = Field(..., description="Which phase of the session this is.")
    occurred_at: UtcDatetime = Field(..., description="Simulation time of the phase (UTC).")
    vehicle_id: str = Field(..., min_length=1, description="Vehicle occupying the connector.")
    trip_id: str | None = Field(default=None, description="Trip the charging stop belongs to.")
    connector_type: ConnectorType = Field(
        default=ConnectorType.unknown,
        description="Connector the vehicle is plugged into.",
    )
    power_kw: float | None = Field(
        default=None,
        ge=0.0,
        description="Momentary charging power in kW.",
    )
    energy_added_kwh: float | None = Field(
        default=None,
        ge=0.0,
        description="Energy delivered so far in this session, in kWh.",
    )
    soc_percent: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="State of charge at this phase, in percent.",
    )
    duration_s: float | None = Field(
        default=None,
        ge=0.0,
        description="Elapsed session time in seconds.",
    )
    latitude: float | None = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description="Site location (latitude), for map consumers that do not join the station.",
    )
    longitude: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="Site location (longitude).",
    )


TelemetryEnvelope = EventEnvelope[TelemetryEvent]
"""Message type of ``vehicle.telemetry.v1``."""

TripEnvelope = EventEnvelope[TripEvent]
"""Message type of ``vehicle.trip-events.v1``."""

TrafficEventEnvelope = EventEnvelope[TrafficEventMessage]
"""Message type of ``traffic.events.v1``."""

ChargingEventEnvelope = EventEnvelope[ChargingEvent]
"""Message type of ``charging.events.v1``."""
