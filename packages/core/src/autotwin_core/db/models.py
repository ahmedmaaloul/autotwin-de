"""The AutoTwin schema: every table of BUILD_SPEC §3.2 as a SQLAlchemy 2.0 mapped class.

This module is the single source of truth for the physical schema — ``alembic/env.py`` reads
``Base.metadata`` from here and nothing else creates tables (BUILD_SPEC §3.3). Three conventions
run through all of it:

* **Enums are stored by value.** Every enum column uses the shared ``sa.Enum`` instances from
  :mod:`autotwin_core.db.types`, which persist the lowercase member *values* of BUILD_SPEC §2 —
  the same strings the API returns and the frontend switches on.
* **Indexes are declared, never implied.** GeoAlchemy2's automatic spatial index is switched off
  and each index is written out in ``__table_args__`` with the name the spec gives it, so an
  Alembic autogenerate diff contains only real changes. Unique constraints are left unnamed on
  purpose: the naming convention in :mod:`autotwin_core.db.base` derives their names.
* **Provenance where the spec says so.** :class:`~autotwin_core.db.mixins.ProvenanceMixin` is
  mixed into the seven externally-sourced tables and deliberately kept off ``telemetry``.

Audit stamps follow one rule: mutable entity tables carry ``created_at``/``updated_at``
(:class:`TimestampMixin`), append-only audit tables carry ``created_at`` only
(:class:`CreatedAtMixin`), and ``telemetry`` carries neither because its ``ingested_at`` already
says when the row arrived and the table is written a thousand rows at a time.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

import sqlalchemy as sa
from geoalchemy2.elements import WKBElement
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from autotwin_contracts.enums import (
    Bundesland,
    ChargingCategory,
    ConnectorType,
    CurrentType,
    DataOrigin,
    EnergyIntensity,
    IngestionOutcome,
    ProviderMode,
    RoadClass,
    SimulationState,
    SourceSystem,
    TrafficEventType,
    TrafficSeverity,
    VehicleClass,
    VehicleState,
    WeatherCondition,
)
from autotwin_core.db.base import (
    Base,
    CreatedAtMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    model_repr,
)
from autotwin_core.db.mixins import ProvenanceMixin
from autotwin_core.db.types import (
    BUNDESLAND_ENUM,
    CHARGING_CATEGORY_ENUM,
    CONNECTOR_TYPE_ENUM,
    CURRENT_TYPE_ENUM,
    DATA_ORIGIN_ENUM,
    ENERGY_INTENSITY_ENUM,
    INGESTION_OUTCOME_ENUM,
    PROVIDER_MODE_ENUM,
    ROAD_CLASS_ENUM,
    SIMULATION_STATE_ENUM,
    SOURCE_SYSTEM_ENUM,
    TRAFFIC_EVENT_TYPE_ENUM,
    TRAFFIC_SEVERITY_ENUM,
    VEHICLE_CLASS_ENUM,
    VEHICLE_STATE_ENUM,
    WEATHER_CONDITION_ENUM,
    JSONBDict,
    LineStringGeometry,
    PointGeography,
    utc_now,
)

__all__ = [
    "ChargingPoint",
    "ChargingStation",
    "DataIngestionRun",
    "MLModel",
    "MLPrediction",
    "Route",
    "RouteSegment",
    "SimulationRun",
    "Telemetry",
    "TrafficEvent",
    "Trip",
    "Vehicle",
    "VehicleModel",
    "WeatherObservation",
    "WeatherStation",
]


# ---------------------------------------------------------------------------------------------
# Ingestion bookkeeping
# ---------------------------------------------------------------------------------------------


class DataIngestionRun(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One execution of one ingestion pipeline.

    Every externally-sourced row points back here, and ``GET /api/v1/data/quality`` reads
    :attr:`quality_report` straight off the latest run per source. Counting received, accepted,
    rejected and duplicate rows separately is what makes "the Ladesäulenregister has 12 broken
    coordinates this week" a visible fact rather than a silent loss.
    """

    __tablename__ = "data_ingestion_runs"
    __table_args__ = (
        # The data-quality page and the dashboard both ask for "the latest run per source".
        sa.Index("ix_data_ingestion_runs_source_started_at", "source", sa.text("started_at DESC")),
    )

    source: Mapped[SourceSystem] = mapped_column(SOURCE_SYSTEM_ENUM, nullable=False)
    pipeline: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        default=utc_now,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=True,
    )
    status: Mapped[IngestionOutcome] = mapped_column(INGESTION_OUTCOME_ENUM, nullable=False)
    provider_mode: Mapped[ProviderMode] = mapped_column(PROVIDER_MODE_ENUM, nullable=False)
    rows_received: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    rows_accepted: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    rows_rejected: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    rows_duplicate: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    bytes_downloaded: Mapped[int | None] = mapped_column(sa.BigInteger(), nullable=True)
    source_url: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    source_file_sha256: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    quality_report: Mapped[JSONBDict | None] = mapped_column(postgresql.JSONB(), nullable=True)

    def __repr__(self) -> str:
        """Identify the run by pipeline and source — what a log line needs."""
        return model_repr(self, "id", "pipeline", "source", "status")


# ---------------------------------------------------------------------------------------------
# Charging infrastructure — Bundesnetzagentur Ladesäulenregister
# ---------------------------------------------------------------------------------------------


class ChargingStation(UUIDPrimaryKeyMixin, ProvenanceMixin, TimestampMixin, Base):
    """One physical charging site (Ladestandort) with its connectors.

    :attr:`external_id` is the natural key the adapters synthesise (``bnetza:<hash>``), which is
    what turns re-ingesting the register into an idempotent upsert. Address parts are nullable
    because the published register genuinely omits them for a small share of sites, and dropping
    those rows would understate the network in exactly the regions that matter most.
    """

    __tablename__ = "charging_stations"
    __table_args__ = (
        sa.UniqueConstraint("external_id"),
        sa.Index("ix_charging_stations_location", "location", postgresql_using="gist"),
        sa.Index("ix_charging_stations_bundesland", "bundesland"),
        sa.Index("ix_charging_stations_charging_category", "charging_category"),
        # Descending: every power-ranked listing and the "strongest chargers first" map layer
        # read this index backwards-free.
        sa.Index("ix_charging_stations_max_power_kw", sa.text("max_power_kw DESC")),
        sa.Index("ix_charging_stations_operator", "operator"),
    )

    external_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    operator: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    street: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    house_number: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    city: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    bundesland: Mapped[Bundesland | None] = mapped_column(BUNDESLAND_ENUM, nullable=True)
    country: Mapped[str] = mapped_column(
        sa.Text(),
        nullable=False,
        default="DE",
        server_default="DE",
    )
    location: Mapped[WKBElement] = mapped_column(PointGeography, nullable=False)
    commissioned_on: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    charging_points_count: Mapped[int] = mapped_column(
        nullable=False,
        default=1,
        server_default=sa.text("1"),
    )
    max_power_kw: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    total_power_kw: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    charging_category: Mapped[ChargingCategory] = mapped_column(
        CHARGING_CATEGORY_ENUM,
        nullable=False,
    )
    # Materialised rather than computed on read: the dashboard counts fast chargers on every
    # load, and a stored boolean keeps that a plain indexed count instead of a scan.
    is_fast_charger: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=sa.false(),
    )
    raw: Mapped[JSONBDict | None] = mapped_column(postgresql.JSONB(), nullable=True)

    points: Mapped[list[ChargingPoint]] = relationship(
        back_populates="station",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ChargingPoint.ordinal",
    )

    def __repr__(self) -> str:
        """Identify the site by its natural key and power class."""
        return model_repr(self, "id", "external_id", "city", "charging_category")


class ChargingPoint(UUIDPrimaryKeyMixin, ProvenanceMixin, TimestampMixin, Base):
    """A single connector (Ladepunkt) belonging to a station."""

    __tablename__ = "charging_points"
    __table_args__ = (sa.Index("ix_charging_points_station_id", "station_id"),)

    station_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("charging_stations.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    connector_type: Mapped[ConnectorType] = mapped_column(
        CONNECTOR_TYPE_ENUM,
        nullable=False,
        default=ConnectorType.unknown,
        server_default=ConnectorType.unknown.value,
    )
    current_type: Mapped[CurrentType] = mapped_column(
        CURRENT_TYPE_ENUM,
        nullable=False,
        default=CurrentType.unknown,
        server_default=CurrentType.unknown.value,
    )
    power_kw: Mapped[float | None] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=True)
    public_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    station: Mapped[ChargingStation] = relationship(back_populates="points")

    def __repr__(self) -> str:
        """Identify the connector by its station and position."""
        return model_repr(self, "id", "station_id", "ordinal", "connector_type")


# ---------------------------------------------------------------------------------------------
# Weather — Deutscher Wetterdienst
# ---------------------------------------------------------------------------------------------


class WeatherStation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A DWD measuring station.

    No provenance block: the station list is metadata about the source itself rather than
    observed data, and BUILD_SPEC §3.1 scopes the block to the seven tables that carry
    measurements or derived facts.
    """

    __tablename__ = "weather_stations"
    __table_args__ = (
        sa.UniqueConstraint("dwd_station_id"),
        sa.Index("ix_weather_stations_location", "location", postgresql_using="gist"),
    )

    dwd_station_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    location: Mapped[WKBElement] = mapped_column(PointGeography, nullable=False)
    elevation_m: Mapped[float | None] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=True)
    bundesland: Mapped[Bundesland | None] = mapped_column(BUNDESLAND_ENUM, nullable=True)
    valid_from: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)
    valid_to: Mapped[date | None] = mapped_column(sa.Date(), nullable=True)

    observations: Mapped[list[WeatherObservation]] = relationship(back_populates="station")

    def __repr__(self) -> str:
        """Identify the station by its DWD id and name."""
        return model_repr(self, "id", "dwd_station_id", "name")


class WeatherObservation(UUIDPrimaryKeyMixin, ProvenanceMixin, TimestampMixin, Base):
    """One weather observation at a point in time and space.

    Almost every measurement is nullable because DWD station coverage is uneven — a station
    reporting temperature but no humidity is normal, and rejecting the row over it would thin
    the network for no analytical gain. The unique constraint on
    ``(weather_station_id, observed_at)`` is the upsert key for re-runs of the hourly pipeline.
    """

    __tablename__ = "weather_observations"
    __table_args__ = (
        sa.UniqueConstraint("weather_station_id", "observed_at"),
        sa.Index("ix_weather_observations_location", "location", postgresql_using="gist"),
        sa.Index("ix_weather_observations_observed_at", sa.text("observed_at DESC")),
    )

    weather_station_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("weather_stations.id", ondelete="SET NULL"),
        nullable=True,
    )
    observed_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    location: Mapped[WKBElement] = mapped_column(PointGeography, nullable=False)
    temperature_c: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    precipitation_mm: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    wind_speed_ms: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    wind_gust_ms: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    humidity_percent: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    pressure_hpa: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    condition: Mapped[WeatherCondition] = mapped_column(
        WEATHER_CONDITION_ENUM,
        nullable=False,
        default=WeatherCondition.unknown,
        server_default=WeatherCondition.unknown.value,
    )

    station: Mapped[WeatherStation | None] = relationship(back_populates="observations")

    def __repr__(self) -> str:
        """Identify the observation by station and timestamp."""
        return model_repr(self, "id", "weather_station_id", "observed_at", "temperature_c")


# ---------------------------------------------------------------------------------------------
# Traffic — Autobahn GmbH / Mobilithek
# ---------------------------------------------------------------------------------------------


class TrafficEvent(UUIDPrimaryKeyMixin, ProvenanceMixin, TimestampMixin, Base):
    """A roadworks, closure, incident or warning report.

    :attr:`location` is the representative point used for spatial filtering, while
    :attr:`geometry` holds the affected stretch when the source publishes one — matching events
    onto route segments needs the line, drawing the map marker needs the point.
    """

    __tablename__ = "traffic_events"
    __table_args__ = (
        sa.UniqueConstraint("external_id"),
        sa.Index("ix_traffic_events_location", "location", postgresql_using="gist"),
        sa.Index("ix_traffic_events_event_type", "event_type"),
        sa.Index("ix_traffic_events_starts_at", sa.text("starts_at DESC")),
        sa.Index("ix_traffic_events_road_name", "road_name"),
    )

    external_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    event_type: Mapped[TrafficEventType] = mapped_column(TRAFFIC_EVENT_TYPE_ENUM, nullable=False)
    severity: Mapped[TrafficSeverity] = mapped_column(
        TRAFFIC_SEVERITY_ENUM,
        nullable=False,
        default=TrafficSeverity.moderate,
        server_default=TrafficSeverity.moderate.value,
    )
    road_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    direction: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    title: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    location: Mapped[WKBElement] = mapped_column(PointGeography, nullable=False)
    geometry: Mapped[WKBElement | None] = mapped_column(LineStringGeometry, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    is_blocked: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=sa.false(),
    )
    delay_minutes: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    raw: Mapped[JSONBDict | None] = mapped_column(postgresql.JSONB(), nullable=True)

    def __repr__(self) -> str:
        """Identify the event by road, type and severity."""
        return model_repr(self, "id", "road_name", "event_type", "severity")


# ---------------------------------------------------------------------------------------------
# Routes and their analysis segments
# ---------------------------------------------------------------------------------------------


class Route(UUIDPrimaryKeyMixin, ProvenanceMixin, TimestampMixin, Base):
    """A planned or known corridor, e.g. the Frankfurt→Stuttgart demo route.

    Stored as ``geometry(LineString,4326)`` rather than geography: the corridor is rendered and
    simplified far more often than it is measured, and the few places that need metres cast to
    ``::geography`` explicitly (BUILD_SPEC §3).
    """

    __tablename__ = "routes"
    __table_args__ = (
        sa.UniqueConstraint("slug"),
        sa.Index("ix_routes_geometry", "geometry", postgresql_using="gist"),
    )

    slug: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    origin_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    destination_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    origin: Mapped[WKBElement] = mapped_column(PointGeography, nullable=False)
    destination: Mapped[WKBElement] = mapped_column(PointGeography, nullable=False)
    geometry: Mapped[WKBElement] = mapped_column(LineStringGeometry, nullable=False)
    distance_m: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    duration_s: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    routing_profile: Mapped[str] = mapped_column(
        sa.Text(),
        nullable=False,
        default="driving",
        server_default="driving",
    )
    is_demo: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=sa.false(),
    )

    segments: Mapped[list[RouteSegment]] = relationship(
        back_populates="route",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RouteSegment.ordinal",
    )
    trips: Mapped[list[Trip]] = relationship(back_populates="route")

    def __repr__(self) -> str:
        """Identify the corridor by slug and endpoints."""
        return model_repr(self, "id", "slug", "origin_name", "destination_name")


class RouteSegment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One analysis chunk of a route, carrying both its conditions and its prediction.

    Environment (temperature, traffic severity) and result (kWh, intensity) live on the same row
    on purpose: ``RouteAnalysis`` (BUILD_SPEC §7.3) has to explain *why* a segment costs what it
    costs, and that explanation is only reproducible if the inputs are stored next to the output.
    No provenance block — a segment is derived from a route that already carries one.
    """

    __tablename__ = "route_segments"
    __table_args__ = (
        sa.UniqueConstraint("route_id", "ordinal"),
        sa.Index("ix_route_segments_geometry", "geometry", postgresql_using="gist"),
    )

    route_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("routes.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False)
    geometry: Mapped[WKBElement] = mapped_column(LineStringGeometry, nullable=False)
    start_offset_m: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    distance_m: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    road_class: Mapped[RoadClass] = mapped_column(
        ROAD_CLASS_ENUM,
        nullable=False,
        default=RoadClass.unknown,
        server_default=RoadClass.unknown.value,
    )
    speed_limit_kmh: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    assumed_speed_kmh: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    elevation_gain_m: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    temperature_c: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    traffic_severity: Mapped[TrafficSeverity | None] = mapped_column(
        TRAFFIC_SEVERITY_ENUM,
        nullable=True,
    )
    predicted_kwh_per_100km: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    predicted_kwh: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    energy_intensity: Mapped[EnergyIntensity | None] = mapped_column(
        ENERGY_INTENSITY_ENUM,
        nullable=True,
    )

    route: Mapped[Route] = relationship(back_populates="segments")

    def __repr__(self) -> str:
        """Identify the segment by route and position."""
        return model_repr(self, "id", "route_id", "ordinal", "road_class")


# ---------------------------------------------------------------------------------------------
# Vehicles, trips and telemetry
# ---------------------------------------------------------------------------------------------


class VehicleModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A generic EV profile (BUILD_SPEC §9), seeded by migration ``0002``.

    Deliberately class-based rather than OEM-specific: the numbers are public-domain,
    order-of-magnitude figures for a vehicle *segment*, which keeps the physical model
    defensible without reverse-engineering anyone's battery.
    """

    __tablename__ = "vehicle_models"
    __table_args__ = (sa.UniqueConstraint("code"),)

    code: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    display_name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    vehicle_class: Mapped[VehicleClass] = mapped_column(VEHICLE_CLASS_ENUM, nullable=False)
    battery_capacity_kwh: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    usable_capacity_kwh: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    nominal_consumption_kwh_100km: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    max_dc_power_kw: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    max_ac_power_kw: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    mass_kg: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    drag_coefficient: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    frontal_area_m2: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    rolling_resistance: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    is_generic: Mapped[bool] = mapped_column(
        nullable=False,
        default=True,
        server_default=sa.true(),
    )

    vehicles: Mapped[list[Vehicle]] = relationship(back_populates="vehicle_model")

    def __repr__(self) -> str:
        """Identify the profile by its stable code."""
        return model_repr(self, "id", "code", "vehicle_class")


class Vehicle(UUIDPrimaryKeyMixin, ProvenanceMixin, TimestampMixin, Base):
    """A simulated vehicle instance.

    Carries the provenance block with ``data_origin = simulated`` — always, without exception.
    That single column is what the UI reads to stamp every one of these rows ``SIMULIERT``
    (BUILD_SPEC §0.2), and it is the reason the block is on this table at all even though no
    external authority ever published it.
    """

    __tablename__ = "vehicles"
    __table_args__ = (sa.UniqueConstraint("vehicle_id"),)

    vehicle_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    vehicle_model_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("vehicle_models.id", ondelete="RESTRICT"),
        nullable=False,
    )
    simulation_run_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("simulation_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    state: Mapped[VehicleState] = mapped_column(
        VEHICLE_STATE_ENUM,
        nullable=False,
        default=VehicleState.idle,
        server_default=VehicleState.idle.value,
    )

    vehicle_model: Mapped[VehicleModel] = relationship(back_populates="vehicles")
    simulation_run: Mapped[SimulationRun | None] = relationship(back_populates="vehicles")
    trips: Mapped[list[Trip]] = relationship(
        back_populates="vehicle",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        """Identify the vehicle by its human id and state."""
        return model_repr(self, "id", "vehicle_id", "state")


class Trip(UUIDPrimaryKeyMixin, ProvenanceMixin, TimestampMixin, Base):
    """One journey of one vehicle, aggregated as it runs.

    :attr:`distance_m` and :attr:`energy_kwh` are maintained incrementally by the streaming
    consumer so that ``/api/v1/trips`` never has to aggregate the telemetry table on read.
    """

    __tablename__ = "trips"
    __table_args__ = (
        sa.UniqueConstraint("trip_id"),
        sa.Index("ix_trips_vehicle_id_started_at", "vehicle_id", sa.text("started_at DESC")),
    )

    trip_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    vehicle_id: Mapped[UUID] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("vehicles.id", ondelete="CASCADE"),
        nullable=False,
    )
    route_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    start_soc_percent: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    end_soc_percent: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    distance_m: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
        default=0.0,
        server_default=sa.text("0"),
    )
    energy_kwh: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
        default=0.0,
        server_default=sa.text("0"),
    )
    avg_consumption_kwh_100km: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    state: Mapped[VehicleState] = mapped_column(
        VEHICLE_STATE_ENUM,
        nullable=False,
        default=VehicleState.driving,
        server_default=VehicleState.driving.value,
    )

    vehicle: Mapped[Vehicle] = relationship(back_populates="trips")
    route: Mapped[Route | None] = relationship(back_populates="trips")

    def __repr__(self) -> str:
        """Identify the trip by its human id and state."""
        return model_repr(self, "id", "trip_id", "vehicle_id", "state")


class Telemetry(Base):
    """High-volume vehicle telemetry — one row per simulator tick per vehicle.

    The only table with a ``BIGINT`` identity key instead of a UUID: at forty vehicles ticking
    every simulated second this grows by millions of rows, and a monotonic 8-byte key keeps the
    index dense and the append cheap. It also carries no provenance block — every row is
    simulated by construction, so a single ``data_origin`` column says everything the honesty
    rule requires.

    ``vehicle_id`` and ``trip_id`` are the *human* identifiers rather than foreign keys: they are
    the Kafka message keys (BUILD_SPEC §8), and the consumer must be able to insert a batch
    without first resolving UUIDs. Duplicates are tolerated — delivery is at-least-once and the
    API always reads the latest row per vehicle.
    """

    __tablename__ = "telemetry"
    __table_args__ = (
        # The live map's "latest position per vehicle" query and every per-vehicle history read.
        sa.Index("ix_telemetry_vehicle_id_recorded_at", "vehicle_id", sa.text("recorded_at DESC")),
        sa.Index("ix_telemetry_recorded_at", sa.text("recorded_at DESC")),
        sa.Index("ix_telemetry_trip_id", "trip_id"),
        sa.Index("ix_telemetry_location", "location", postgresql_using="gist"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger(), primary_key=True, autoincrement=True)
    vehicle_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    trip_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    route_id: Mapped[UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    location: Mapped[WKBElement] = mapped_column(PointGeography, nullable=False)
    speed_kmh: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    acceleration_ms2: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    heading_deg: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    battery_soc_percent: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    battery_temperature_c: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    outside_temperature_c: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    instantaneous_power_kw: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    energy_consumption_kwh_100km: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    cumulative_energy_kwh: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    estimated_range_km: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    road_class: Mapped[RoadClass] = mapped_column(
        ROAD_CLASS_ENUM,
        nullable=False,
        default=RoadClass.unknown,
        server_default=RoadClass.unknown.value,
    )
    speed_limit_kmh: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    odometer_m: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    state: Mapped[VehicleState] = mapped_column(VEHICLE_STATE_ENUM, nullable=False)
    data_origin: Mapped[DataOrigin] = mapped_column(
        DATA_ORIGIN_ENUM,
        nullable=False,
        default=DataOrigin.simulated,
        server_default=DataOrigin.simulated.value,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )

    def __repr__(self) -> str:
        """Identify the sample by vehicle and timestamp."""
        return model_repr(self, "id", "vehicle_id", "recorded_at", "battery_soc_percent")


# ---------------------------------------------------------------------------------------------
# Simulation control plane
# ---------------------------------------------------------------------------------------------


class SimulationRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Configuration and live counters of one simulation run.

    :attr:`seed` is stored rather than derived so a run is reproducible after the fact: the
    ``/simulation`` page can state the exact seed that produced a recorded scenario, which is
    what makes the simulated training data auditable.
    """

    __tablename__ = "simulation_runs"

    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    state: Mapped[SimulationState] = mapped_column(
        SIMULATION_STATE_ENUM,
        nullable=False,
        default=SimulationState.pending,
        server_default=SimulationState.pending.value,
    )
    vehicle_count: Mapped[int] = mapped_column(nullable=False)
    speed_factor: Mapped[float] = mapped_column(postgresql.DOUBLE_PRECISION(), nullable=False)
    weather_mode: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    traffic_intensity: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    vehicle_mix: Mapped[JSONBDict] = mapped_column(
        postgresql.JSONB(),
        nullable=False,
        default=dict,
    )
    route_slugs: Mapped[list[str]] = mapped_column(
        postgresql.JSONB(),
        nullable=False,
        default=list,
    )
    seed: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    events_emitted: Mapped[int] = mapped_column(
        sa.BigInteger(),
        nullable=False,
        default=0,
        server_default=sa.text("0"),
    )
    errors: Mapped[int] = mapped_column(nullable=False, default=0, server_default=sa.text("0"))
    config: Mapped[JSONBDict] = mapped_column(postgresql.JSONB(), nullable=False, default=dict)

    vehicles: Mapped[list[Vehicle]] = relationship(back_populates="simulation_run")

    def __repr__(self) -> str:
        """Identify the run by name and state."""
        return model_repr(self, "id", "name", "state", "vehicle_count")


# ---------------------------------------------------------------------------------------------
# ML registry and prediction audit
# ---------------------------------------------------------------------------------------------


class MLModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Registry entry for a trained energy-consumption model.

    :attr:`metrics` holds the model's *and* the physical baseline's MAE/RMSE/R² on the same test
    split (BUILD_SPEC §10.2). Storing them together is the point: a model without its baseline
    is a number nobody can judge. :attr:`training_data_origin` records that the training set was
    simulated, so the honesty rule survives into the ML surface.
    """

    __tablename__ = "ml_models"
    __table_args__ = (sa.UniqueConstraint("name", "version"),)

    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    version: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    algorithm: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    trained_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False)
    training_rows: Mapped[int] = mapped_column(nullable=False)
    feature_names: Mapped[list[str]] = mapped_column(postgresql.JSONB(), nullable=False)
    metrics: Mapped[JSONBDict] = mapped_column(postgresql.JSONB(), nullable=False)
    artifact_path: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=sa.false(),
    )
    training_data_origin: Mapped[DataOrigin] = mapped_column(
        DATA_ORIGIN_ENUM,
        nullable=False,
        default=DataOrigin.simulated,
        server_default=DataOrigin.simulated.value,
    )
    notes: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    predictions: Mapped[list[MLPrediction]] = relationship(back_populates="ml_model")

    def __repr__(self) -> str:
        """Identify the artefact by name, version and activation state."""
        return model_repr(self, "id", "name", "version", "is_active")


class MLPrediction(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Audit record of one served prediction, with its features and SHAP contributions.

    Append-only. ``route_id`` and ``trip_id`` are plain columns rather than foreign keys on
    purpose: the audit trail must outlive the route or trip it described, and a prediction is
    still evidence of what the model answered even after the corridor has been re-planned.
    """

    __tablename__ = "ml_predictions"

    ml_model_id: Mapped[UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("ml_models.id", ondelete="SET NULL"),
        nullable=True,
    )
    route_id: Mapped[UUID | None] = mapped_column(postgresql.UUID(as_uuid=True), nullable=True)
    trip_id: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    features: Mapped[JSONBDict] = mapped_column(postgresql.JSONB(), nullable=False)
    prediction_kwh_100km: Mapped[float] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=False,
    )
    baseline_kwh_100km: Mapped[float | None] = mapped_column(
        postgresql.DOUBLE_PRECISION(),
        nullable=True,
    )
    shap_values: Mapped[JSONBDict | None] = mapped_column(postgresql.JSONB(), nullable=True)
    context: Mapped[JSONBDict | None] = mapped_column(postgresql.JSONB(), nullable=True)

    ml_model: Mapped[MLModel | None] = relationship(back_populates="predictions")

    def __repr__(self) -> str:
        """Identify the prediction by model and value."""
        return model_repr(self, "id", "ml_model_id", "prediction_kwh_100km")
