"""Response models for ``/api/v1/charging`` (BUILD_SPEC §7, §7.2).

Field names are copied from ``apps/web/types/domain.ts`` and must stay identical to it: the
frontend's types are generated from this service's OpenAPI document, and a renamed field is a
compile error in the web app rather than a negotiation.

Two shapes in here are worth a word:

* :class:`StationFeature` is spelled out rather than reusing the generic
  ``GeoJSONFeatureCollection`` from ``autotwin_contracts``, whose ``properties`` is an open
  mapping. The map layer reads six specific keys off every station feature, and writing them as
  a model is what puts them in the generated TypeScript instead of leaving ``unknown`` there.
* :class:`CorridorCoverageOut` carries both ``route_distance_km`` and
  ``route_geometry_length_km``. They differ by a few per mille and each column in the payload
  uses exactly one of them — see :mod:`autotwin_api.services.coverage`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field

from autotwin_api.schemas.common import ApiModel, GeoPointOut, ProvenanceOut
from autotwin_api.services.coverage import CorridorCoverage, CorridorGap
from autotwin_contracts import (
    Bundesland,
    ChargingCategory,
    ConnectorType,
    CurrentType,
    GeoJSONLineString,
)

__all__ = [
    "BundeslandStat",
    "ChargingPointOut",
    "ChargingStationDetail",
    "ChargingStationSummary",
    "ChargingStatistics",
    "CorridorCoverageOut",
    "CoverageGapOut",
    "GrowthPoint",
    "OperatorStat",
    "PowerClassStat",
    "StationFeature",
    "StationFeatureCollection",
    "StationFeatureProperties",
    "UnderservedCorridorOut",
    "UnderservedEvaluation",
    "UnderservedParameters",
    "UnderservedReport",
]


# ---------------------------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------------------------


class ChargingStationSummary(ApiModel):
    """One charging site as the list and map endpoints return it.

    Latitude and longitude are plain numbers rather than a geometry: the list view renders a
    table and a marker, and neither needs a GeoJSON envelope around two floats. The
    ``/stations/geojson`` endpoint is where the geometry form lives.
    """

    id: UUID = Field(..., description="AutoTwin identifier of the site.")
    external_id: str = Field(
        ...,
        description="Natural key in the source register, e.g. `bnetza:<hash>`.",
        examples=["bnetza:0a1b2c3d"],
    )
    operator: str | None = Field(
        default=None,
        description="Operator as the Bundesnetzagentur register spells it.",
        examples=["EnBW mobility+ AG und Co.KG"],
    )
    city: str | None = Field(default=None, description="Municipality.", examples=["Frankfurt"])
    postal_code: str | None = Field(default=None, description="Postal code.", examples=["60311"])
    street: str | None = Field(default=None, description="Street name.")
    house_number: str | None = Field(default=None, description="House number.")
    bundesland: Bundesland | None = Field(
        default=None,
        description="Federal state as an ISO 3166-2:DE code without the `DE-` prefix.",
        examples=["HE"],
    )
    latitude: float = Field(..., description="WGS 84 latitude.", examples=[50.1109])
    longitude: float = Field(..., description="WGS 84 longitude.", examples=[8.6821])
    max_power_kw: float | None = Field(
        default=None,
        description="Power of the strongest connector on site, in kW.",
        examples=[300.0],
    )
    total_power_kw: float | None = Field(
        default=None,
        description="Total installed power of the site, in kW.",
        examples=[600.0],
    )
    charging_points_count: int = Field(
        ...,
        description="Number of connectors (Ladepunkte) at the site.",
        examples=[4],
    )
    charging_category: ChargingCategory = Field(
        ...,
        description="`normal` (<22 kW), `fast` (22-149 kW) or `ultra_fast` (>=150 kW).",
        examples=["ultra_fast"],
    )
    is_fast_charger: bool = Field(
        ...,
        description="Whether the strongest connector reaches 50 kW (BUILD_SPEC §3.2).",
    )
    commissioned_on: date | None = Field(
        default=None,
        description="Date the register says the site went into service.",
        examples=["2023-04-17"],
    )


class ChargingPointOut(ApiModel):
    """A single connector (Ladepunkt) of a site."""

    id: UUID = Field(..., description="AutoTwin identifier of the connector.")
    ordinal: int = Field(..., description="Position of the connector within its site, from 1.")
    connector_type: ConnectorType = Field(
        ...,
        description="Plug standard: type2, ccs, chademo, schuko, tesla, cee, other, unknown.",
        examples=["ccs"],
    )
    current_type: CurrentType = Field(
        ...,
        description="`ac`, `dc` or `unknown`.",
        examples=["dc"],
    )
    power_kw: float | None = Field(
        default=None,
        description="Rated power of this connector, in kW.",
        examples=[150.0],
    )


class ChargingStationDetail(ChargingStationSummary):
    """One site with its connectors and the provenance block (BUILD_SPEC §3.1)."""

    charging_points: list[ChargingPointOut] = Field(
        default_factory=list,
        description="The site's connectors, in register order.",
    )
    provenance: ProvenanceOut = Field(
        ...,
        description="Where this row came from and when — the honesty rule made visible.",
    )


class StationFeatureProperties(ApiModel):
    """Attributes the map layer reads off a station feature.

    Deliberately small. The map draws ~20 000 of these at once and paints them by power class;
    anything the styling does not use belongs in `/stations` or `/stations/{id}`, not in every
    feature of a multi-megabyte collection.
    """

    id: UUID = Field(..., description="AutoTwin identifier of the site.")
    operator: str | None = Field(default=None, description="Operator, for the marker tooltip.")
    max_power_kw: float | None = Field(default=None, description="Strongest connector, in kW.")
    charging_category: ChargingCategory = Field(..., description="Power class, drives the colour.")
    is_fast_charger: bool = Field(..., description="Whether the site reaches 50 kW.")
    city: str | None = Field(default=None, description="Municipality, for the marker tooltip.")


class StationFeature(ApiModel):
    """RFC 7946 Feature for one charging site."""

    type: Literal["Feature"] = Field(default="Feature", description="Always `Feature`.")
    geometry: GeoPointOut = Field(
        ...,
        description="Point geometry; `coordinates` is [longitude, latitude], RFC 7946 order.",
    )
    properties: StationFeatureProperties = Field(..., description="Station attributes.")
    id: str | None = Field(
        default=None,
        description="Feature id, used by MapLibre for hover feature-state.",
    )


class StationFeatureCollection(ApiModel):
    """RFC 7946 FeatureCollection of charging sites, ready for a MapLibre source."""

    type: Literal["FeatureCollection"] = Field(
        default="FeatureCollection",
        description="Always `FeatureCollection`.",
    )
    features: list[StationFeature] = Field(
        default_factory=list,
        description="One feature per site, capped — see the endpoint description.",
    )
    bbox: tuple[float, float, float, float] | None = Field(
        default=None,
        description="Extent of the returned features as [west, south, east, north].",
    )
    total_matching: int = Field(
        ...,
        description=(
            "How many sites matched the filters in total. Greater than `len(features)` when the "
            "cap truncated the collection — narrow the filters or the bounding box."
        ),
    )
    truncated: bool = Field(
        ...,
        description="Whether the feature cap dropped matching sites from this collection.",
    )


# ---------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------


class BundeslandStat(ApiModel):
    """Infrastructure totals for one federal state."""

    bundesland: Bundesland = Field(..., description="ISO 3166-2:DE code.", examples=["BY"])
    label_de: str = Field(..., description="Official German name.", examples=["Bayern"])
    stations: int = Field(..., description="Charging sites in the state.")
    fast_points: int = Field(..., description="Connectors rated 50 kW or more.")
    total_kw: float = Field(
        ...,
        description="Installed power in kW, summed over sites (site total, else strongest plug).",
    )


class PowerClassStat(ApiModel):
    """Site count for one power class."""

    category: ChargingCategory = Field(..., description="Power class.", examples=["fast"])
    count: int = Field(..., description="Sites in this class.")


class OperatorStat(ApiModel):
    """Site count for one operator."""

    operator: str = Field(..., description="Operator name as the register spells it.")
    stations: int = Field(..., description="Sites the operator runs.")


class GrowthPoint(ApiModel):
    """One year of the commissioning curve."""

    year: int = Field(..., description="Calendar year of commissioning.", examples=[2023])
    stations: int = Field(..., description="Sites commissioned during that year.")
    cumulative: int = Field(..., description="Sites commissioned up to and including that year.")


class ChargingStatistics(ApiModel):
    """The four breakdowns behind the charging dashboard."""

    by_bundesland: list[BundeslandStat] = Field(
        ...,
        description="One row per federal state that has at least one site, largest first.",
    )
    by_power_class: list[PowerClassStat] = Field(
        ...,
        description="Site counts by power class; all three classes are always present.",
    )
    by_operator: list[OperatorStat] = Field(
        ...,
        description="The largest operators by site count — see `top_operators`.",
    )
    growth: list[GrowthPoint] = Field(
        ...,
        description=(
            "Commissioning curve by year, oldest first. Only sites whose commissioning date "
            "the register publishes are counted, so the cumulative total is a lower bound on "
            "the network."
        ),
    )
    stations_total: int = Field(..., description="All sites in the register snapshot.")
    stations_with_commissioning_date: int = Field(
        ...,
        description="How many of them carry a commissioning date — the base of `growth`.",
    )
    generated_at: datetime = Field(..., description="When this answer was computed (UTC).")


# ---------------------------------------------------------------------------------------------
# Corridor coverage
# ---------------------------------------------------------------------------------------------


class CoverageGapOut(ApiModel):
    """One stretch of a corridor with no qualifying charger on it."""

    start_offset_km: float = Field(
        ...,
        description="Distance from the origin, measured along the route geometry.",
    )
    end_offset_km: float = Field(..., description="Distance from the origin where the gap ends.")
    gap_km: float = Field(..., description="Length of the gap in kilometres.")
    kind: str = Field(
        ...,
        description=(
            "`origin` (start → first charger), `between` (two chargers), `destination` (last "
            "charger → end) or `whole_route` (no qualifying charger anywhere). The three kinds "
            "are very different planning problems, which is why they are not flattened."
        ),
        examples=["between"],
    )
    geometry: GeoJSONLineString | None = Field(
        default=None,
        description="The stretch of road itself, for the map. Null for a zero-length gap.",
    )

    @classmethod
    def of(cls, gap: CorridorGap) -> Self:
        """Adapt the service-layer gap onto the wire type."""
        return cls(
            start_offset_km=gap.start_offset_km,
            end_offset_km=gap.end_offset_km,
            gap_km=gap.gap_km,
            kind=gap.kind,
            geometry=gap.geometry,
        )


class CorridorCoverageOut(ApiModel):
    """Charging coverage of one corridor at one set of parameters (BUILD_SPEC §7.2)."""

    route_id: UUID = Field(..., description="The analysed route.")
    route_slug: str | None = Field(default=None, description="Its slug, for demo corridors.")
    route_name: str = Field(..., description="Human name, e.g. `Frankfurt am Main → Stuttgart`.")

    buffer_km: float = Field(..., description="Corridor half-width used, in kilometres.")
    min_power_kw: float = Field(
        ...,
        description="Power from which a site counted as a charging opportunity.",
    )

    route_distance_km: float = Field(
        ...,
        description="Driving distance from the routing engine — the base for the densities.",
    )
    route_geometry_length_km: float = Field(
        ...,
        description=(
            "Length of the stored polyline in EPSG:25832 — the base for every offset and gap. "
            "A few per mille shorter than the driving distance, because a polyline is a chord "
            "approximation of the road."
        ),
    )

    stations_in_corridor: int = Field(..., description="All sites within the buffer.")
    fast_stations_in_corridor: int = Field(..., description="Of those, sites reaching 50 kW.")
    qualifying_stations_in_corridor: int = Field(
        ...,
        description="Of those, sites reaching `min_power_kw` — the set the gaps come from.",
    )
    stations_per_100km: float = Field(..., description="Sites per 100 km of driving distance.")
    qualifying_stations_per_100km: float = Field(
        ...,
        description="Qualifying sites per 100 km of driving distance.",
    )

    gap_count: int = Field(
        ...,
        description="Number of gaps in the decomposition, including the two edge gaps.",
    )
    max_gap_km: float = Field(
        ...,
        description="Longest gap — the figure the whole analysis exists to report.",
    )
    mean_gap_km: float = Field(..., description="Mean gap length over all `gap_count` gaps.")
    gaps: list[CoverageGapOut] = Field(
        ...,
        description=(
            "The longest gaps, largest first, capped at `top_gaps`. The aggregates above are "
            "computed over all of them, so `sum(gaps)` need not equal the route length."
        ),
    )

    coverage_score: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="0-100 planning heuristic blending the worst gap and the corridor density.",
    )
    methodology: str = Field(
        ...,
        description="German sentence stating exactly how the figures above were produced.",
    )
    generated_at: datetime = Field(..., description="When this answer was computed (UTC).")

    @classmethod
    def of(cls, coverage: CorridorCoverage, *, generated_at: datetime) -> Self:
        """Adapt the service-layer result onto the wire type.

        ``stations_in_corridor`` and its siblings are non-optional here even though the service
        may leave them unset: this endpoint always asks for the full rollup, and declaring them
        nullable would push a null check into the frontend for a case it cannot reach.
        """
        stations = coverage.stations_in_corridor or 0
        return cls(
            route_id=coverage.route_id,
            route_slug=coverage.route_slug,
            route_name=coverage.route_name,
            buffer_km=coverage.buffer_km,
            min_power_kw=coverage.min_power_kw,
            route_distance_km=coverage.route_distance_km,
            route_geometry_length_km=coverage.route_geometry_length_km,
            stations_in_corridor=stations,
            fast_stations_in_corridor=coverage.fast_stations_in_corridor or 0,
            qualifying_stations_in_corridor=coverage.qualifying_stations_in_corridor,
            stations_per_100km=coverage.stations_per_100km or 0.0,
            qualifying_stations_per_100km=coverage.qualifying_stations_per_100km,
            gap_count=coverage.gap_count,
            max_gap_km=coverage.max_gap_km,
            mean_gap_km=coverage.mean_gap_km,
            gaps=[CoverageGapOut.of(gap) for gap in coverage.gaps],
            coverage_score=coverage.coverage_score,
            methodology=coverage.methodology,
            generated_at=generated_at,
        )


class UnderservedParameters(ApiModel):
    """The thresholds an underserved report was produced with."""

    min_power_kw: float = Field(..., description="Power from which a site counts as a charger.")
    max_gap_km: float = Field(..., description="Gap length above which a stretch is reported.")
    corridor_buffer_km: float = Field(..., description="Corridor half-width, in kilometres.")


class UnderservedCorridorOut(ApiModel):
    """A corridor with at least one stretch longer than the threshold."""

    route_slug: str = Field(..., description="Slug of the corridor.")
    name: str = Field(..., description="Human name of the corridor.")
    worst_gap_km: float = Field(..., description="Its longest gap, in kilometres.")
    coverage_score: float = Field(..., description="Its 0-100 coverage score.")
    segments: list[CoverageGapOut] = Field(
        ...,
        description="The stretches longer than `max_gap_km`, longest first.",
    )


class UnderservedEvaluation(ApiModel):
    """One line of the ranking of every corridor that was examined.

    Present so that a report naming no underserved corridor is still an answer rather than an
    empty array: "all five demo corridors were checked and the worst gap anywhere is 13 km" is
    the useful form of *nothing to report*.
    """

    route_slug: str = Field(..., description="Slug of the corridor.")
    name: str = Field(..., description="Human name of the corridor.")
    worst_gap_km: float = Field(..., description="Its longest gap, in kilometres.")
    coverage_score: float = Field(..., description="Its 0-100 coverage score.")
    is_underserved: bool = Field(
        ...,
        description="Whether its worst gap exceeds `parameters.max_gap_km`.",
    )


class UnderservedReport(ApiModel):
    """Corridors whose charging gaps exceed the planning threshold (BUILD_SPEC §7.2)."""

    parameters: UnderservedParameters = Field(
        ...,
        description="The thresholds this report was produced with.",
    )
    corridors: list[UnderservedCorridorOut] = Field(
        ...,
        description="Only the corridors that breach the threshold, worst first.",
    )
    evaluated: list[UnderservedEvaluation] = Field(
        ...,
        description="Every corridor that was examined, worst gap first.",
    )
    generated_at: datetime = Field(..., description="When this answer was computed (UTC).")
    methodology: str = Field(
        ...,
        description=(
            "German sentence stating exactly how the gaps were measured and scored, so the UI "
            "never has to describe the method from memory."
        ),
    )
