"""Provider output records — the shape data has *before* it reaches the database.

Adapters in ``autotwin_ingestion.providers`` parse German open data into these models; the
ingestion pipelines then validate, deduplicate and map them onto the SQLAlchemy models of
BUILD_SPEC §3. Keeping a separate record layer buys two things that matter here:

* provider code and its fixture tests need no database at all, and
* a schema change upstream shows up as a validation error in one adapter instead of a broken
  ``INSERT`` somewhere in a pipeline.

Every record is frozen. A parsed source row is a fact about the world at a point in time; if a
pipeline wants a different value it constructs a new record with :meth:`model_copy`, which
leaves the original — and any log line that referenced it — intact.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from autotwin_contracts.enums import (
    Bundesland,
    ChargingCategory,
    ConnectorType,
    CurrentType,
    DataOrigin,
    RoadClass,
    SourceSystem,
    TrafficEventType,
    TrafficSeverity,
    WeatherCondition,
)
from autotwin_contracts.geo import BoundingBox, Coordinate
from autotwin_contracts.temporal import UtcDatetime, utc_now

__all__ = [
    "ChargingPointRecord",
    "ChargingStationRecord",
    "Place",
    "ProvenanceInfo",
    "RouteResult",
    "RouteStepRecord",
    "TrafficEventRecord",
    "WeatherRecord",
    "WeatherStationRecord",
]

FAST_CHARGER_THRESHOLD_KW: float = 50.0
"""Power from which a site counts as a fast charger (BUILD_SPEC §3.2, ``is_fast_charger``)."""


class ProvenanceInfo(BaseModel):
    """The provenance block of BUILD_SPEC §3.1, as carried by records in flight.

    Every externally sourced row keeps this so that the API can answer *where did this number
    come from, and when was it true?* without joining back through the ingestion run.
    """

    model_config = ConfigDict(frozen=True)

    source: SourceSystem = Field(..., description="System that produced the row.")
    source_identifier: str | None = Field(
        default=None,
        description="Identifier of the record in the source system, if it has one.",
    )
    source_url: str | None = Field(
        default=None,
        description="Document or endpoint the row was read from.",
    )
    source_timestamp: UtcDatetime | None = Field(
        default=None,
        description="When the source itself says the value was valid (UTC).",
    )
    data_origin: DataOrigin = Field(
        ...,
        description="official, simulated or derived — never inferred, always stated.",
    )
    ingestion_run_id: UUID | None = Field(
        default=None,
        description="The data_ingestion_runs row this record was written by, once persisted.",
    )
    ingested_at: UtcDatetime = Field(
        default_factory=utc_now,
        description="When AutoTwin read the row (UTC).",
    )


class ChargingPointRecord(BaseModel):
    """A single connector at a charging site (``charging_points``)."""

    model_config = ConfigDict(frozen=True)

    ordinal: int = Field(
        ...,
        ge=1,
        description="1-based position of the connector within its station, "
        "as listed by the source.",
    )
    connector_type: ConnectorType = Field(
        default=ConnectorType.unknown,
        description="Physical connector standard, normalised by ConnectorType.parse.",
    )
    current_type: CurrentType = Field(
        default=CurrentType.unknown,
        description="AC or DC.",
    )
    power_kw: float | None = Field(
        default=None,
        ge=0.0,
        description="Rated power of this connector in kW, if the source states it.",
    )
    public_key: str | None = Field(
        default=None,
        description="Public key of the charging point as published in the Ladesäulenregister.",
    )


class ChargingStationRecord(BaseModel):
    """One physical charging site (``charging_stations``) with its connectors.

    The natural key is :attr:`external_id`. The Ladesäulenregister has no stable site id, so
    adapters synthesise one deterministically (``bnetza:<hash of address+location>``) — that
    is what makes re-ingesting the same file an idempotent upsert instead of a duplication.
    """

    model_config = ConfigDict(frozen=True)

    external_id: str = Field(
        ...,
        min_length=1,
        description="Stable natural key from the source, e.g. 'bnetza:<hash>'.",
    )
    operator: str | None = Field(default=None, description="Betreiber of the site.")
    street: str | None = Field(default=None, description="Straße.")
    house_number: str | None = Field(default=None, description="Hausnummer, kept as text.")
    postal_code: str | None = Field(default=None, description="Postleitzahl, kept as text.")
    city: str | None = Field(default=None, description="Ort.")
    bundesland: Bundesland | None = Field(
        default=None,
        description="Federal state as an ISO 3166-2:DE code, resolved by Bundesland.from_name.",
    )
    country: str = Field(default="DE", description="ISO 3166-1 alpha-2 country code.")
    coordinate: Coordinate = Field(..., description="Site location in WGS 84.")
    commissioned_on: date | None = Field(
        default=None,
        description="Inbetriebnahmedatum — the date the site went live.",
    )
    charging_points_count: int = Field(
        default=1,
        ge=1,
        description="Number of connectors at the site.",
    )
    max_power_kw: float | None = Field(
        default=None,
        ge=0.0,
        description="Strongest single connector in kW; drives the charging category.",
    )
    total_power_kw: float | None = Field(
        default=None,
        ge=0.0,
        description="Installed grid connection power of the whole site in kW.",
    )
    charging_category: ChargingCategory = Field(
        ...,
        description="Power class; defaults to ChargingCategory.from_power(max_power_kw).",
    )
    points: tuple[ChargingPointRecord, ...] = Field(
        default=(),
        description="The site's connectors, in source order.",
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Trimmed original source record, kept for auditability.",
    )
    provenance: ProvenanceInfo = Field(..., description="Provenance block (BUILD_SPEC §3.1).")

    @model_validator(mode="before")
    @classmethod
    def _default_category(cls, data: Any) -> Any:
        """Derive the power class when the caller did not state one.

        Adapters would otherwise repeat the same ``from_power`` call, and any adapter that
        forgot it would quietly misclassify a site in the fast-charger statistics.
        """
        if isinstance(data, dict) and data.get("charging_category") is None:
            category = ChargingCategory.from_power(data.get("max_power_kw"))
            data = {**data, "charging_category": category}
        return data

    @computed_field  # type: ignore[prop-decorator]  # mypy: decorated property (pydantic docs)
    @property
    def is_fast_charger(self) -> bool:
        """Whether the site reaches the 50 kW threshold used across the API and the UI."""
        return self.max_power_kw is not None and self.max_power_kw >= FAST_CHARGER_THRESHOLD_KW


class WeatherStationRecord(BaseModel):
    """A DWD measuring station (``weather_stations``)."""

    model_config = ConfigDict(frozen=True)

    dwd_station_id: str = Field(
        ...,
        min_length=1,
        description="DWD Stations-ID, five digits, zero-padded and kept as text.",
    )
    name: str = Field(..., min_length=1, description="Station name as published by the DWD.")
    coordinate: Coordinate = Field(..., description="Station location in WGS 84.")
    elevation_m: float | None = Field(
        default=None,
        description="Station height above sea level in metres (Stationshöhe).",
    )
    bundesland: Bundesland | None = Field(default=None, description="Federal state of the station.")
    valid_from: date | None = Field(default=None, description="First day the station reported.")
    valid_to: date | None = Field(
        default=None,
        description="Last day the station reported; null while it is active.",
    )
    provenance: ProvenanceInfo = Field(..., description="Provenance block (BUILD_SPEC §3.1).")


class WeatherRecord(BaseModel):
    """One weather observation at a point in time (``weather_observations``).

    Every field except the timestamp and the location is optional because DWD station
    coverage is uneven — a station that reports temperature but no humidity is normal, and
    dropping the row over it would thin the network for no reason.
    """

    model_config = ConfigDict(frozen=True)

    station_id: str | None = Field(
        default=None,
        description="DWD Stations-ID of the reporting station, if the observation has one.",
    )
    observed_at: UtcDatetime = Field(..., description="Observation timestamp (UTC).")
    coordinate: Coordinate = Field(..., description="Observation location in WGS 84.")
    temperature_c: float | None = Field(
        default=None,
        description="Air temperature 2 m above ground in °C — the strongest weather driver "
        "of EV consumption.",
    )
    precipitation_mm: float | None = Field(
        default=None,
        ge=0.0,
        description="Precipitation in mm for the observation interval.",
    )
    wind_speed_ms: float | None = Field(
        default=None,
        ge=0.0,
        description="Mean wind speed in m/s.",
    )
    wind_gust_ms: float | None = Field(
        default=None,
        ge=0.0,
        description="Maximum gust in m/s.",
    )
    humidity_percent: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="Relative humidity in percent.",
    )
    pressure_hpa: float | None = Field(
        default=None,
        gt=0.0,
        description="Air pressure in hPa.",
    )
    condition: WeatherCondition = Field(
        default=WeatherCondition.unknown,
        description="Coarse condition derived from the measured parameters.",
    )
    provenance: ProvenanceInfo = Field(..., description="Provenance block (BUILD_SPEC §3.1).")


class TrafficEventRecord(BaseModel):
    """A roadworks, closure, incident or warning report (``traffic_events``)."""

    model_config = ConfigDict(frozen=True)

    external_id: str | None = Field(
        default=None,
        description="Identifier in the source system; the upsert key when present.",
    )
    event_type: TrafficEventType = Field(..., description="Kind of disruption.")
    severity: TrafficSeverity = Field(
        default=TrafficSeverity.moderate,
        description="Expected impact on travel time; drives the delay factor in route analysis.",
    )
    road_name: str | None = Field(
        default=None,
        description="Road designation such as 'A5' or 'B27', used to match events to segments.",
    )
    direction: str | None = Field(
        default=None,
        description="Fahrtrichtung as published, e.g. 'Frankfurt' or 'Süd'.",
    )
    title: str = Field(..., min_length=1, description="Short German headline of the report.")
    description: str | None = Field(default=None, description="Full German report text.")
    coordinate: Coordinate = Field(
        ...,
        description="Representative point of the event, used for spatial queries.",
    )
    geometry: tuple[Coordinate, ...] = Field(
        default=(),
        description="Affected stretch of road, when the source publishes a line geometry.",
    )
    starts_at: UtcDatetime | None = Field(default=None, description="Validity start (UTC).")
    ends_at: UtcDatetime | None = Field(default=None, description="Validity end (UTC), if known.")
    is_blocked: bool = Field(
        default=False,
        description="Whether the road is fully blocked (Vollsperrung).",
    )
    delay_minutes: float | None = Field(
        default=None,
        ge=0.0,
        description="Reported delay in minutes, if the source quantifies it.",
    )
    raw: dict[str, Any] | None = Field(
        default=None,
        description="Trimmed original source record, kept for auditability.",
    )
    provenance: ProvenanceInfo = Field(..., description="Provenance block (BUILD_SPEC §3.1).")


class RouteStepRecord(BaseModel):
    """One manoeuvre-level step of a route, summarised for segmentation.

    Only what the energy model needs is kept — distance, duration, road class and speed limit.
    Turn-by-turn banners, exits and lane hints are dropped in the adapter.
    """

    model_config = ConfigDict(frozen=True)

    ordinal: int = Field(..., ge=0, description="0-based position of the step along the route.")
    name: str | None = Field(
        default=None,
        description="Street or road name reported by the routing engine, e.g. 'A5'.",
    )
    distance_m: float = Field(..., ge=0.0, description="Step length in metres.")
    duration_s: float = Field(..., ge=0.0, description="Free-flow step duration in seconds.")
    road_class: RoadClass = Field(
        default=RoadClass.unknown,
        description="Functional class of the road this step runs on.",
    )
    speed_limit_kmh: float | None = Field(
        default=None,
        gt=0.0,
        description="Posted speed limit in km/h; null on unrestricted Autobahn stretches.",
    )
    geometry: tuple[Coordinate, ...] = Field(
        default=(),
        description="Step geometry in WGS 84, when the engine returns per-step geometry.",
    )


class RouteResult(BaseModel):
    """A computed route (``routes``), as returned by a :class:`RoutingProvider`.

    The geometry is a plain list of coordinates rather than an encoded polyline so that the
    segmentation code, the simulator and the GeoJSON serialiser all read the same structure.
    """

    model_config = ConfigDict(frozen=True)

    geometry: list[Coordinate] = Field(
        ...,
        min_length=2,
        description="Route geometry in WGS 84, ordered from origin to destination.",
    )
    distance_m: float = Field(..., ge=0.0, description="Total route length in metres.")
    duration_s: float = Field(
        ...,
        ge=0.0,
        description="Total free-flow driving time in seconds, before traffic penalties.",
    )
    profile: str = Field(
        default="driving",
        description="Routing profile the engine was asked for.",
    )
    leg_count: int = Field(
        default=1,
        ge=1,
        description="Number of legs; one per waypoint pair, so 1 for a plain A→B route.",
    )
    steps: tuple[RouteStepRecord, ...] = Field(
        default=(),
        description="Step summaries in travel order; empty when steps were not requested.",
    )
    weight_name: str | None = Field(
        default=None,
        description="Name of the cost function the engine optimised, e.g. 'routability'.",
    )
    annotations: dict[str, Any] | None = Field(
        default=None,
        description="Raw per-node annotation arrays (speed, duration, distance) when requested.",
    )
    provenance: ProvenanceInfo | None = Field(
        default=None,
        description="Provenance block; set when the route is persisted as a corridor.",
    )

    @property
    def origin(self) -> Coordinate:
        """First coordinate of the geometry."""
        return self.geometry[0]

    @property
    def destination(self) -> Coordinate:
        """Last coordinate of the geometry."""
        return self.geometry[-1]

    @property
    def average_speed_kmh(self) -> float:
        """Free-flow average speed, or 0.0 for a zero-duration route."""
        if self.duration_s <= 0.0:
            return 0.0
        return (self.distance_m / 1000.0) / (self.duration_s / 3600.0)


class Place(BaseModel):
    """A geocoding result (Nominatim), used by ``GET /api/v1/routes/geocode``."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., min_length=1, description="Short name of the place, e.g. 'Stuttgart'.")
    display_name: str = Field(
        ...,
        min_length=1,
        description="Full human-readable address as returned by the geocoder.",
    )
    coordinate: Coordinate = Field(..., description="Representative point of the place.")
    place_type: str = Field(
        default="unknown",
        description="Geocoder class/type such as 'city', 'town', 'motorway_junction'.",
    )
    bundesland: Bundesland | None = Field(
        default=None,
        description="Federal state the place lies in, when the geocoder reports one.",
    )
    country_code: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 country code, lower-case as Nominatim reports it.",
    )
    bbox: BoundingBox | None = Field(
        default=None,
        description="Extent of the place, for fitting the map to a search result.",
    )
    source_identifier: str | None = Field(
        default=None,
        description="Identifier in the geocoder, e.g. 'osm:relation:62611'.",
    )

    @classmethod
    def of(cls, name: str, latitude: float, longitude: float) -> Self:
        """Build a minimal place — convenient for demo corridors and fixtures."""
        return cls(
            name=name,
            display_name=name,
            coordinate=Coordinate(latitude=latitude, longitude=longitude),
        )
