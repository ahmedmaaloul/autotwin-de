"""Request and response models of the route endpoints — including the flagship §7.3 payload.

``POST /api/v1/routes/analyze`` is the endpoint this whole project exists to serve, and its
response is the widest in the API. It is modelled here in full rather than as a loose ``dict``
for one reason: the frontend's types are generated from this schema, so every field name,
nullability and unit written here is a compile-time guarantee on the other side of the wire.

Three conventions run through the module.

* **Units are in the name.** Distances in the database are metres (BUILD_SPEC §14); anything a
  human reads is exposed in kilometres and says ``_km``. ``route.distance_m`` keeps metres
  because the map layer does arithmetic with it; every per-segment figure is kilometres because
  the Streckenband renders it.
* **Null means "not known", never "zero".** A missing speed limit is an unrestricted Autobahn
  stretch, not a 0 km/h one; a missing ``model_kwh_100km`` means no model is trained, and the
  frontend has to say so rather than plot a zero.
* **Every number the analysis produced is reported beside the number it was derived from.** The
  ML prediction sits next to the physical baseline, the arrival SOC next to the minimum that was
  required, the penalties next to the counterfactual they were measured against. A figure a
  reader cannot check is a figure a reader cannot trust.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from autotwin_api.schemas.common import ApiModel, ProvenanceOut
from autotwin_api.schemas.traffic import TrafficEventOut
from autotwin_api.schemas.weather import WeatherObservationOut
from autotwin_contracts import (
    Bundesland,
    ChargingCategory,
    EnergyIntensity,
    GeoJSONLineString,
    ProviderMode,
    RoadClass,
    TrafficSeverity,
    VehicleClass,
)

__all__ = [
    "ChargingOptimizeRequest",
    "ChargingPlanOut",
    "ChargingStationSummaryOut",
    "ChargingStopOut",
    "DataModesOut",
    "EnergyDriverOut",
    "EnergyExplanationOut",
    "PlaceOut",
    "PointIn",
    "RouteAnalysisOut",
    "RouteAnalyzeRequest",
    "RouteDetailOut",
    "RouteEndpointOut",
    "RoutePlanRequest",
    "RoutePlanResponse",
    "RouteSegmentAnalysisOut",
    "RouteSegmentOut",
    "RouteSelector",
    "RouteSummaryOut",
    "VehicleProfileOut",
]

DEFAULT_VEHICLE_CODE = "sedan_ev"
"""Vehicle used when a request does not name one — the mid-size profile of BUILD_SPEC §9."""


# ======================================================================================
# Shared pieces
# ======================================================================================


class PointIn(ApiModel):
    """A caller-supplied WGS 84 position, latitude first.

    Separate from :class:`~autotwin_api.schemas.common.CoordinateOut` because a request body
    and a response body are different contracts even when they happen to share a shape today;
    merging them would make a future response field a breaking change to every client's request
    validation.
    """

    latitude: float = Field(
        ...,
        ge=-90.0,
        le=90.0,
        description="Latitude in WGS 84 decimal degrees (north positive).",
        examples=[50.1109],
    )
    longitude: float = Field(
        ...,
        ge=-180.0,
        le=180.0,
        description="Longitude in WGS 84 decimal degrees (east positive).",
        examples=[8.6821],
    )


class RouteEndpointOut(ApiModel):
    """A named end of a route — the ``origin`` / ``destination`` object of BUILD_SPEC §7.3."""

    name: str = Field(..., description="Place name, as geocoded or as stored on the corridor.")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude in WGS 84 degrees.")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude in WGS 84 degrees.")


class PlaceOut(ApiModel):
    """One geocoding candidate — the payload of ``GET /api/v1/routes/geocode``."""

    name: str = Field(..., description="Short name of the place, e.g. 'Stuttgart'.")
    display_name: str = Field(..., description="Full address as the geocoder returned it.")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude in WGS 84 degrees.")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude in WGS 84 degrees.")
    place_type: str = Field(
        default="unknown",
        description="Geocoder class/type, e.g. 'city', 'town', 'motorway_junction'.",
    )
    bundesland: Bundesland | None = Field(
        default=None,
        description="Federal state, when the geocoder reports one (ISO 3166-2:DE without the "
        "'DE-' prefix).",
    )
    country_code: str | None = Field(
        default=None,
        description="ISO 3166-1 alpha-2 country code, lower-case as Nominatim reports it.",
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Extent as [west, south, east, north] for fitting the map to the result.",
    )
    source_identifier: str | None = Field(
        default=None,
        description="Identifier in the geocoder, e.g. 'osm:relation:62611'.",
    )


class VehicleProfileOut(ApiModel):
    """The vehicle an analysis was run for (BUILD_SPEC §9).

    Echoed in full rather than as a code so that the page can state its own assumptions: a
    consumption figure means nothing without the mass and drag area it was computed from.
    """

    code: str = Field(..., description="Stable profile id, e.g. 'sedan_ev'.", examples=["sedan_ev"])
    display_name: str = Field(..., description="Label shown in the UI.")
    vehicle_class: VehicleClass = Field(..., description="Segment this profile represents.")
    battery_capacity_kwh: float = Field(..., description="Gross battery capacity in kWh.")
    usable_capacity_kwh: float = Field(
        ...,
        description="Usable capacity in kWh — the denominator every SOC in this payload is "
        "expressed against.",
    )
    nominal_consumption_kwh_100km: float = Field(
        ...,
        description="Datasheet consumption in kWh/100 km; the reference the explanation's "
        "drivers are measured against.",
    )
    max_dc_power_kw: float = Field(..., description="Peak DC charging power in kW.")
    max_ac_power_kw: float = Field(..., description="Peak AC charging power in kW.")
    mass_kg: float = Field(..., description="Kerb mass in kg, battery included.")
    drag_area_m2: float = Field(..., description="c_d · A in m², the aerodynamic road-load term.")


class DataModesOut(ApiModel):
    """How each source answered (BUILD_SPEC §4), so the UI can flag degraded data.

    Three independent values rather than one, because they degrade independently: routing can be
    live while the weather is a six-hour-old cached file. The response header
    ``X-AutoTwin-Data-Mode`` carries the *worst* of the three; this object says which one it was.
    """

    routing: ProviderMode = Field(..., description="Route geometry: live, cache or fixture.")
    weather: ProviderMode = Field(..., description="Weather observations: live, cache or fixture.")
    traffic: ProviderMode = Field(..., description="Traffic events: live, cache or fixture.")


# ======================================================================================
# Route catalogue
# ======================================================================================


class RouteSummaryOut(ApiModel):
    """A stored corridor as the list endpoint returns it."""

    id: UUID = Field(..., description="Primary key of the route row.")
    slug: str | None = Field(
        default=None,
        description="Stable handle of a demo corridor, e.g. 'frankfurt-stuttgart'; null for a "
        "route that was planned ad hoc and persisted without one.",
        examples=["frankfurt-stuttgart"],
    )
    name: str = Field(..., description="Human label, e.g. 'Frankfurt am Main → Stuttgart'.")
    origin_name: str = Field(..., description="Start place name.")
    destination_name: str = Field(..., description="End place name.")
    origin: RouteEndpointOut = Field(..., description="Start point with its name.")
    destination: RouteEndpointOut = Field(..., description="End point with its name.")
    distance_m: float = Field(..., description="Total route length in metres.")
    duration_s: float = Field(
        ...,
        description="Free-flow driving time in seconds, as the routing engine computed it — "
        "before any traffic delay is applied.",
    )
    is_demo: bool = Field(
        ...,
        description="Whether this is one of the seeded demo corridors. The list endpoint "
        "returns these first.",
    )


class RouteSegmentOut(ApiModel):
    """One stored analysis chunk of a route (``route_segments``, BUILD_SPEC §3.2)."""

    ordinal: int = Field(..., ge=0, description="0-based position along the route.")
    start_offset_km: float = Field(
        ...,
        ge=0.0,
        description="Distance from the origin to the start of this segment, in kilometres.",
    )
    distance_km: float = Field(..., gt=0.0, description="Segment length in kilometres.")
    road_class: RoadClass = Field(
        ...,
        description="Class covering most of the segment, from the routing engine's steps.",
    )
    speed_limit_kmh: float | None = Field(
        default=None,
        description="Posted limit in km/h; **null on an unrestricted Autobahn stretch**, which "
        "is not the same as a missing value of zero.",
    )
    assumed_speed_kmh: float | None = Field(
        default=None,
        description="Free-flow speed the routing engine implies for this segment, in km/h.",
    )
    geometry: GeoJSONLineString | None = Field(
        default=None,
        description="Segment geometry as an RFC 7946 LineString.",
    )


class RouteDetailOut(RouteSummaryOut):
    """A stored corridor with its geometry, its segments and its provenance."""

    geometry: GeoJSONLineString = Field(
        ...,
        description="Route geometry as an RFC 7946 LineString, simplified for transport (see "
        "the endpoint description for the tolerance).",
    )
    routing_profile: str = Field(..., description="Routing profile the corridor was built with.")
    segment_count: int = Field(..., ge=0, description="Number of stored analysis segments.")
    segments: list[RouteSegmentOut] = Field(
        default_factory=list,
        description="The stored segments in route order.",
    )
    provenance: ProvenanceOut = Field(..., description="Where the corridor came from (§3.1).")


# ======================================================================================
# Planning
# ======================================================================================


class RoutePlanRequest(ApiModel):
    """Plan a fresh route between two places — geometry only, no energy analysis."""

    origin: str | None = Field(
        default=None,
        description="Free-text start, e.g. 'Frankfurt am Main'. Geocoded with Nominatim. "
        "Supply this **or** `origin_point`.",
        examples=["Frankfurt am Main"],
    )
    destination: str | None = Field(
        default=None,
        description="Free-text destination. Supply this **or** `destination_point`.",
        examples=["Stuttgart"],
    )
    origin_point: PointIn | None = Field(
        default=None,
        description="Exact start coordinate, bypassing the geocoder.",
    )
    destination_point: PointIn | None = Field(
        default=None,
        description="Exact destination coordinate, bypassing the geocoder.",
    )
    profile: Literal["driving"] = Field(
        default="driving",
        description="Routing profile. `driving` is the only one AutoTwin models.",
    )

    @model_validator(mode="after")
    def _check_endpoints(self) -> Self:
        """Both ends must be given somehow — as text or as a coordinate."""
        if self.origin is None and self.origin_point is None:
            msg = "supply either 'origin' or 'origin_point'"
            raise ValueError(msg)
        if self.destination is None and self.destination_point is None:
            msg = "supply either 'destination' or 'destination_point'"
            raise ValueError(msg)
        return self


class RoutePlanResponse(ApiModel):
    """A planned route: the geometry and its provenance, nothing energy-related."""

    origin: RouteEndpointOut = Field(..., description="Resolved start point.")
    destination: RouteEndpointOut = Field(..., description="Resolved destination.")
    distance_m: float = Field(..., ge=0.0, description="Route length in metres.")
    duration_s: float = Field(..., ge=0.0, description="Free-flow driving time in seconds.")
    geometry: GeoJSONLineString = Field(..., description="Route geometry (RFC 7946 LineString).")
    profile: str = Field(..., description="Routing profile actually used.")
    data_mode: ProviderMode = Field(
        ...,
        description="How the routing engine answered: live, cache or fixture.",
    )
    source_url: str | None = Field(default=None, description="Routing request behind the answer.")
    fetched_at: datetime | None = Field(
        default=None,
        description="When the underlying route was obtained (UTC) — cache age, not response age.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal problems reported by the provider, in English.",
    )


# ======================================================================================
# Analysis (BUILD_SPEC §7.3)
# ======================================================================================


class RouteSelector(ApiModel):
    """The four mutually-exclusive ways to name the route an analysis should run on.

    A seeded corridor (``route_slug`` / ``route_id``) is the fast path: its geometry and its
    segments are already in PostGIS, so the analysis never calls the routing engine. Free text
    geocodes and routes on the request path, which is slower and depends on two external
    services — hence both are offered rather than one.
    """

    route_id: UUID | None = Field(
        default=None,
        description="Primary key of a stored route. Mutually exclusive with the other selectors.",
    )
    route_slug: str | None = Field(
        default=None,
        description="Slug of a stored corridor, e.g. 'frankfurt-stuttgart'.",
        examples=["frankfurt-stuttgart"],
    )
    origin: str | None = Field(
        default=None,
        description="Free-text start; geocoded, then routed. Requires `destination`.",
        examples=["Frankfurt am Main"],
    )
    destination: str | None = Field(
        default=None,
        description="Free-text destination. Requires `origin`.",
        examples=["Stuttgart"],
    )
    origin_point: PointIn | None = Field(
        default=None,
        description="Exact start coordinate, bypassing the geocoder.",
    )
    destination_point: PointIn | None = Field(
        default=None,
        description="Exact destination coordinate, bypassing the geocoder.",
    )

    @model_validator(mode="after")
    def _check_selector(self) -> Self:
        """Exactly one selector, and a free-text pair must name both ends."""
        has_stored = self.route_id is not None or self.route_slug is not None
        has_origin = self.origin is not None or self.origin_point is not None
        has_destination = self.destination is not None or self.destination_point is not None
        if has_stored and (has_origin or has_destination):
            msg = (
                "supply either a stored route (route_id/route_slug) or origin/destination, not both"
            )
            raise ValueError(msg)
        if not has_stored and not (has_origin and has_destination):
            msg = "supply a stored route (route_id/route_slug) or both an origin and a destination"
            raise ValueError(msg)
        return self


class RouteAnalyzeRequest(RouteSelector):
    """Everything ``POST /api/v1/routes/analyze`` needs beyond the route itself."""

    vehicle_code: str = Field(
        default=DEFAULT_VEHICLE_CODE,
        description="Vehicle profile code (BUILD_SPEC §9): compact_ev, sedan_ev, "
        "performance_ev, suv_ev or van_ev.",
        examples=["sedan_ev"],
    )
    start_soc_percent: float = Field(
        default=80.0,
        ge=0.0,
        le=100.0,
        description="State of charge at the origin, in percent of the **usable** capacity.",
        examples=[70.0],
    )
    min_arrival_soc_percent: float = Field(
        default=10.0,
        ge=0.0,
        le=100.0,
        description="Reserve the driver wants left at the destination. `charging_required` is "
        "true when the computed arrival SOC falls below it.",
        examples=[15.0],
    )
    at: datetime | None = Field(
        default=None,
        description="Reference time for the weather lookup (UTC). Defaults to now; an "
        "observation newer than this is ignored, which makes a past analysis reproducible.",
    )
    weather_radius_km: float = Field(
        default=100.0,
        gt=0.0,
        le=300.0,
        description="A segment adopts the nearest observation within this radius of its "
        "midpoint; beyond it the segment is treated as having no observation rather than "
        "borrowing a temperature from the other end of the country.",
    )
    weather_max_age_hours: float = Field(
        default=24.0,
        gt=0.0,
        le=720.0,
        description="Observations older than this are not used for the physics. They still "
        "appear in `weather_points` with their timestamp, so the gap is visible.",
    )
    traffic_buffer_km: float = Field(
        default=2.0,
        gt=0.0,
        le=25.0,
        description="Corridor half-width for matching traffic events to segments. 2 km keeps "
        "a jam on the parallel B-road out of the Autobahn's severity.",
    )


class RouteSegmentAnalysisOut(ApiModel):
    """One analysed segment — a row of the Streckenband and a feature on the map.

    ``ordinal`` is the join key the frontend uses between the chart and the map layer, so it is
    dense, 0-based and in route order.
    """

    ordinal: int = Field(..., ge=0, description="0-based position along the route.")
    start_offset_km: float = Field(
        ...,
        ge=0.0,
        description="Distance from the origin to the start of this segment, in kilometres.",
    )
    distance_km: float = Field(..., gt=0.0, description="Segment length in kilometres.")
    road_class: RoadClass = Field(..., description="Dominant road class of the segment.")
    speed_limit_kmh: float | None = Field(
        default=None,
        description="Posted limit in km/h; null on an unrestricted stretch.",
    )
    free_flow_speed_kmh: float = Field(
        ...,
        gt=0.0,
        description="Speed assumed without traffic, in km/h.",
    )
    assumed_speed_kmh: float = Field(
        ...,
        gt=0.0,
        description="Speed the energy was computed at, in km/h: the free-flow speed divided by "
        "the matched severity's delay factor (BUILD_SPEC §2).",
    )
    duration_s: float = Field(
        ...,
        ge=0.0,
        description="Expected travel time on this segment in seconds, traffic included.",
    )
    temperature_c: float | None = Field(
        default=None,
        description="Air temperature used for this segment in °C — the nearest recent DWD "
        "observation, or the corridor mean where no station was in range. Null only when the "
        "corridor has no usable observation at all, in which case the model's 20 °C reference "
        "was used and `assumptions` says so.",
    )
    traffic_severity: TrafficSeverity | None = Field(
        default=None,
        description="Worst severity among the events matched to this segment; null when the "
        "segment is clear.",
    )
    traffic_event_count: int = Field(
        default=0,
        ge=0,
        description="How many events were matched to this segment.",
    )
    kwh: float = Field(..., description="Energy this segment costs, in kWh.")
    kwh_per_100km: float = Field(..., description="Consumption on this segment in kWh/100 km.")
    baseline_kwh_per_100km: float = Field(
        ...,
        description="What the physical road-load model alone predicts for this segment, in "
        "kWh/100 km. Equal to `kwh_per_100km` when no ML model is active.",
    )
    soc_at_end_percent: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="State of charge when the segment ends, clamped to 0-100. A route that "
        "cannot be completed pins at 0 and reports the shortfall in `energy_deficit_kwh`.",
    )
    energy_intensity: EnergyIntensity = Field(
        ...,
        description="This segment's consumption against the vehicle's nominal figure: low "
        "(<95 %), medium (<110 %), high (<130 %) or critical.",
    )
    geometry: GeoJSONLineString | None = Field(
        default=None,
        description="Segment geometry as an RFC 7946 LineString — what the map draws.",
    )


class EnergyDriverOut(ApiModel):
    """One entry of ``explanation.drivers`` (BUILD_SPEC §11).

    The drivers sum exactly to ``total_delta_percent``: each is the difference between two
    consecutive re-runs of the physical model, not a single-factor sensitivity, so no
    interaction term goes missing. No language model is involved in producing any of this.
    """

    factor: str = Field(
        ...,
        description="Stable snake_case id: speed_profile, auxiliary, gradient, traffic, "
        "temperature, wind or model_correction.",
        examples=["temperature"],
    )
    delta_percent: float = Field(
        ...,
        description="Signed contribution in percentage points of the nominal energy.",
    )
    direction: Literal["increase", "decrease", "neutral"] = Field(
        ...,
        description="Sign of `delta_percent`, as a rendering hint.",
    )
    label_de: str = Field(..., description="Short German label.")
    label_en: str = Field(..., description="Short English label.")
    delta_kwh: float = Field(..., description="Signed contribution over the whole route, in kWh.")
    detail_de: str = Field(..., description="One German clause naming the numbers behind it.")
    detail_en: str = Field(..., description="The same clause in English.")


class EnergyExplanationOut(ApiModel):
    """The ``explanation`` object of BUILD_SPEC §7.3 — deterministic, auditable, no LLM."""

    headline: str = Field(..., description="One sentence in the default locale (German).")
    headline_de: str = Field(..., description="German headline.")
    headline_en: str = Field(..., description="English headline.")
    drivers: list[EnergyDriverOut] = Field(
        default_factory=list,
        description="Drivers ranked by absolute contribution, largest first.",
    )
    nominal_kwh_100km: float = Field(..., description="Datasheet consumption of the vehicle.")
    actual_kwh_100km: float = Field(..., description="Consumption predicted for this route.")
    total_delta_percent: float = Field(
        ...,
        description="`actual / nominal - 1` in percent. The drivers sum to this.",
    )
    method: str = Field(
        ...,
        description="How the numbers were produced, for the UI's methodology note.",
    )


class RouteAnalysisRouteOut(ApiModel):
    """The ``route`` object of BUILD_SPEC §7.3."""

    id: UUID | None = Field(
        default=None,
        description="Primary key of the stored corridor; null for a route planned ad hoc.",
    )
    slug: str | None = Field(default=None, description="Slug of the stored corridor, if any.")
    name: str = Field(..., description="Human label of the route.")
    distance_m: float = Field(..., ge=0.0, description="Route length in metres.")
    duration_s: float = Field(
        ...,
        ge=0.0,
        description="Free-flow driving time in seconds, as the routing engine computed it.",
    )
    geometry: GeoJSONLineString = Field(..., description="Route geometry (RFC 7946 LineString).")
    origin: RouteEndpointOut = Field(..., description="Start point with its name.")
    destination: RouteEndpointOut = Field(..., description="End point with its name.")
    is_demo: bool = Field(default=False, description="Whether this is a seeded demo corridor.")


class RouteAnalysisOut(ApiModel):
    """The flagship payload (BUILD_SPEC §7.3): what this trip costs, and why."""

    route: RouteAnalysisRouteOut = Field(..., description="The corridor that was analysed.")
    vehicle: VehicleProfileOut = Field(..., description="The vehicle it was analysed for.")
    start_soc_percent: float = Field(..., description="State of charge at the origin, in percent.")
    arrival_soc_percent: float = Field(
        ...,
        description="State of charge at the destination, in percent, clamped to 0-100. See "
        "`energy_deficit_kwh` for how far short a non-feasible trip falls.",
    )
    min_soc_percent_required: float = Field(
        ...,
        description="The reserve the request asked to arrive with.",
    )
    energy_kwh_total: float = Field(..., ge=0.0, description="Total battery energy in kWh.")
    energy_deficit_kwh: float = Field(
        ...,
        ge=0.0,
        description="Energy the trip is short of arriving with `min_soc_percent_required`, in "
        "kWh. Zero when no charging is needed; positive is exactly what the charging optimiser "
        "has to supply.",
    )
    estimated_duration_s: float = Field(
        ...,
        ge=0.0,
        description="Driving time in seconds **with** the matched traffic delays applied — the "
        "route's own `duration_s` is the free-flow figure.",
    )
    avg_consumption_kwh_100km: float = Field(
        ...,
        description="Route consumption in kWh/100 km: the figure that was actually integrated.",
    )
    baseline_kwh_100km: float = Field(
        ...,
        description="What the physical road-load model alone predicts for this route.",
    )
    model_kwh_100km: float | None = Field(
        default=None,
        description="What the trained ML model predicts. **Null when no model is trained** — "
        "the analysis then runs on the physical baseline alone and `model_name` is null too.",
    )
    model_name: str | None = Field(
        default=None,
        description="Name of the ML model that produced `model_kwh_100km`; null when none is "
        "active.",
    )
    model_version: str | None = Field(default=None, description="Version of that model.")
    weather_penalty_percent: float = Field(
        ...,
        description="What the real air and pack temperatures, precipitation and wind cost this "
        "trip, in percent — measured by re-running the physical model at 20 °C in still, dry "
        "air and comparing. A real attribution, not an estimate.",
    )
    traffic_penalty_percent: float = Field(
        ...,
        description="What the matched traffic costs, in percent — the same model re-run at "
        "free-flow speeds.\n\n"
        "**Routinely negative on a motorway corridor, and that is the correct answer.** Traffic "
        "costs *time*, not energy: aerodynamic drag grows with the square of speed, so a jam "
        "that drops a segment from 110 to 55 km/h lowers its consumption while raising its "
        "travel time. `estimated_duration_s` is where traffic shows up as a cost.",
    )
    total_penalty_percent: float = Field(
        ...,
        description="Both effects together, from a single re-run with weather **and** traffic "
        "neutralised. Not the sum of the two above: the terms interact (cold air is denser, and "
        "denser air costs more the faster you drive).",
    )
    charging_required: bool = Field(
        ...,
        description="Whether the trip cannot be completed with `min_soc_percent_required` left.",
    )
    segments: list[RouteSegmentAnalysisOut] = Field(
        default_factory=list,
        description="The analysed segments in route order.",
    )
    traffic_events: list[TrafficEventOut] = Field(
        default_factory=list,
        description="Distinct events matched to the corridor, in route order.",
    )
    weather_points: list[WeatherObservationOut] = Field(
        default_factory=list,
        description="Distinct observations the segments adopted, nearest-station-per-segment "
        "deduplicated.",
    )
    explanation: EnergyExplanationOut = Field(..., description="Why the trip costs what it does.")
    data_modes: DataModesOut = Field(..., description="Freshness of each source consulted.")
    assumptions: list[str] = Field(
        default_factory=list,
        description="Modelling assumptions and data gaps that apply to *this* answer, in "
        "English, for the UI's methodology note. An empty list means none applied.",
    )
    generated_at: datetime = Field(..., description="When this analysis was produced (UTC).")


# ======================================================================================
# Charging optimisation (BUILD_SPEC §7.4)
# ======================================================================================


class ChargingOptimizeRequest(RouteAnalyzeRequest):
    """``POST /api/v1/routes/optimize-charging`` — the analysis inputs plus corridor settings."""

    corridor_buffer_km: float = Field(
        default=5.0,
        gt=0.0,
        le=50.0,
        description="Half-width of the corridor candidate stations are drawn from.",
    )
    min_power_kw: float = Field(
        default=50.0,
        gt=0.0,
        description="Minimum peak power of a candidate site in kW. The default is the "
        "fast-charging threshold of BUILD_SPEC §2.",
    )
    max_stops: int = Field(
        default=3,
        ge=0,
        le=3,
        description="Beam depth — the most stops the plan may contain (BUILD_SPEC §10.3).",
    )
    candidate_spacing_km: float = Field(
        default=10.0,
        gt=0.0,
        le=100.0,
        description="Corridor stations are thinned to the strongest site per stretch of this "
        "length before the search runs. Two ultra-fast sites 400 m apart are one decision, and "
        "keeping both only multiplies the search space.",
    )


class ChargingStationSummaryOut(ApiModel):
    """A charging site as a plan renders it — the ``station`` object of BUILD_SPEC §7.4."""

    id: UUID = Field(..., description="Primary key of the station row.")
    external_id: str = Field(..., description="Natural key from the Bundesnetzagentur register.")
    operator: str | None = Field(default=None, description="Betreiber of the site.")
    street: str | None = Field(default=None, description="Straße.")
    house_number: str | None = Field(default=None, description="Hausnummer, kept as text.")
    postal_code: str | None = Field(default=None, description="Postleitzahl, kept as text.")
    city: str | None = Field(default=None, description="Ort.")
    bundesland: Bundesland | None = Field(default=None, description="Federal state.")
    latitude: float = Field(..., ge=-90.0, le=90.0, description="Latitude in WGS 84 degrees.")
    longitude: float = Field(..., ge=-180.0, le=180.0, description="Longitude in WGS 84 degrees.")
    max_power_kw: float | None = Field(default=None, description="Strongest connector in kW.")
    total_power_kw: float | None = Field(default=None, description="Installed power in kW.")
    charging_points_count: int = Field(..., ge=0, description="Connectors at the site.")
    charging_category: ChargingCategory = Field(
        ...,
        description="normal (<22 kW), fast (22-149 kW) or ultra_fast (>=150 kW).",
    )
    is_fast_charger: bool = Field(..., description="Whether `max_power_kw` reaches 50 kW.")


class ChargingStopOut(ApiModel):
    """One stop of a plan."""

    station: ChargingStationSummaryOut = Field(..., description="Where the stop happens.")
    arrival_soc_percent: float = Field(..., description="SOC on arrival, after the detour.")
    departure_soc_percent: float = Field(..., description="SOC when the plan leaves.")
    detour_km: float = Field(
        ...,
        ge=0.0,
        description="Extra distance for this stop in kilometres — off the corridor **and back**.",
    )
    charge_time_min: float = Field(..., ge=0.0, description="Minutes plugged in.")
    energy_added_kwh: float = Field(..., ge=0.0, description="Energy delivered into the pack.")
    max_power_kw: float = Field(
        ...,
        description="Highest power reached in the session: min(station, vehicle) at the arrival "
        "SOC, which is where the charging curve is highest.",
    )
    avg_power_kw: float = Field(
        ...,
        description="Session average. Always below `max_power_kw` because of the taper — the "
        "gap between the two is what a driver actually feels.",
    )
    offset_km: float = Field(
        ...,
        ge=0.0,
        description="Distance from the route origin to this stop, in kilometres. The "
        "Streckenband places the charging tick with it.",
    )
    rationale_de: str = Field(
        ..., description="One German sentence built from this stop's own numbers."
    )
    rationale_en: str = Field(..., description="The same sentence in English.")


class ChargingPlanOut(ApiModel):
    """The §7.4 payload. ``feasible: false`` is an answer, not an error."""

    feasible: bool = Field(
        ...,
        description="Whether a stop sequence exists that finishes the route without dropping "
        "below the requested reserve.",
    )
    reason: str | None = Field(
        default=None,
        description="Null when feasible; otherwise a bilingual explanation, German first.",
    )
    reason_de: str | None = Field(default=None, description="German half of `reason`.")
    reason_en: str | None = Field(default=None, description="English half of `reason`.")
    stops: list[ChargingStopOut] = Field(
        default_factory=list,
        description="Chosen stops in route order; empty when the route needs no charging.",
    )
    total_time_min: float = Field(
        ...,
        description="Wall-clock total: driving + charging + detour, each counted once.",
    )
    driving_time_min: float = Field(
        ...,
        description="On-route driving time, traffic delays already in the segment durations.",
    )
    charging_time_min: float = Field(..., description="Total time plugged in.")
    detour_km_total: float = Field(..., description="Sum of the stops' detours in kilometres.")
    detour_time_min: float = Field(..., description="Time spent on detours, counted once.")
    arrival_soc_percent: float = Field(..., description="SOC at the destination.")
    min_soc_percent_reached: float = Field(
        ...,
        description="Lowest SOC anywhere in the plan — the range-anxiety number.",
    )
    alternatives_considered: int = Field(
        ...,
        ge=0,
        description="Partial plans the beam search evaluated, so the UI can say the answer was "
        "searched for rather than guessed.",
    )
    candidates_considered: int = Field(
        ...,
        ge=0,
        description="Charging sites offered to the search after corridor selection and thinning.",
    )
    objective: str = Field(..., description="The quantity that was minimised (BUILD_SPEC §10.3).")
    objective_value: float | None = Field(
        default=None,
        description="Value of that objective for this plan, in minutes; null when no plan exists.",
    )
    analysis: RouteAnalysisOut = Field(
        ...,
        description="The energy analysis the plan was built on — returned so the page can show "
        "the plan and the consumption it assumes without a second request.",
    )
