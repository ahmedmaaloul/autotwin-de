"""Response models for ``/api/v1/analytics`` (BUILD_SPEC §7).

Two very different analyses live behind one tag, and the distinction matters more than the
shared prefix:

* :class:`EnergyAnalysis` is computed from the ``telemetry`` table, which is **simulated** by
  construction (BUILD_SPEC §3.2 — the table has no provenance block because every row in it
  comes from the simulator). Every payload therefore carries ``data_origin``, ``is_simulated``
  and a bilingual disclaimer: BUILD_SPEC §0.2 forbids real and simulated data from being mixed
  invisibly, and a consumption-versus-temperature chart is exactly the sort of figure that gets
  screenshotted into a slide deck without its caption.
* :class:`RegionComparison` is computed from the Bundesnetzagentur register and is official
  data. Its densities carry the state areas they were divided by, so a reader can check the
  arithmetic instead of trusting it.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from autotwin_api.schemas.common import ApiModel
from autotwin_contracts import Bundesland, DataOrigin

__all__ = [
    "EnergyAnalysis",
    "EnergyBucket",
    "EnergyDimension",
    "RegionComparison",
    "RegionStat",
]


class EnergyDimension(StrEnum):
    """What ``/analytics/energy`` buckets consumption against."""

    temperature = "temperature"
    """Outside temperature in °C — the dominant driver of winter EV consumption."""

    speed = "speed"
    """Instantaneous speed in km/h; aerodynamic drag grows with its square."""

    traffic = "traffic"
    """Congestion, derived from speed against the posted limit. See `method_en`."""


class EnergyBucket(ApiModel):
    """One bucket of the consumption distribution.

    ``sample_count`` is not decoration. A bucket built from eleven telemetry rows and one built
    from eleven thousand look identical on a line chart, and only the count says which of the
    two means anything — which is why BUILD_SPEC §7 asks for buckets *and* sample counts.
    """

    bucket: str = Field(
        ...,
        description="Human label for the bucket, e.g. `0-5 °C` or `120-140 km/h`.",
        examples=["0-5 °C"],
    )
    lower_bound: float | None = Field(
        default=None,
        description="Inclusive lower edge in the dimension's unit; null for the open first bucket.",
    )
    upper_bound: float | None = Field(
        default=None,
        description="Exclusive upper edge in the dimension's unit; null for the open last bucket.",
    )
    sample_count: int = Field(..., description="Telemetry rows in this bucket.")
    mean_kwh_per_100km: float = Field(..., description="Mean consumption in kWh/100 km.")
    median_kwh_per_100km: float = Field(..., description="Median consumption in kWh/100 km.")
    p10_kwh_per_100km: float = Field(..., description="10th percentile of consumption.")
    p90_kwh_per_100km: float = Field(..., description="90th percentile of consumption.")
    mean_speed_kmh: float = Field(..., description="Mean speed of the rows in this bucket.")
    mean_temperature_c: float = Field(
        ...,
        description="Mean outside temperature of the rows in this bucket.",
    )


class EnergyAnalysis(ApiModel):
    """Energy consumption bucketed against one explanatory dimension."""

    dimension: EnergyDimension = Field(..., description="What the buckets are built over.")
    dimension_unit: str = Field(
        ...,
        description="Unit of `lower_bound`/`upper_bound`.",
        examples=["°C"],
    )
    buckets: list[EnergyBucket] = Field(
        ...,
        description="Buckets in ascending order of the dimension. Empty buckets are omitted.",
    )
    sample_count: int = Field(..., description="Telemetry rows the analysis is built on.")
    excluded_samples: int = Field(
        ...,
        description=(
            "Rows that matched the time window but could not be bucketed — a stationary "
            "vehicle, or (for `traffic`) a stretch with no posted speed limit."
        ),
    )
    overall_mean_kwh_per_100km: float | None = Field(
        default=None,
        description="Mean consumption across every included row; null when there are none.",
    )
    data_origin: DataOrigin = Field(
        ...,
        description="Always `simulated`: the telemetry table is written by the simulator.",
        examples=["simulated"],
    )
    is_simulated: bool = Field(
        ...,
        description="Always true. Present so a chart cannot render this without the label.",
    )
    disclaimer_de: str = Field(..., description="German label to show beside the chart.")
    disclaimer_en: str = Field(..., description="English label to show beside the chart.")
    method_de: str = Field(..., description="How the buckets were formed, in German.")
    method_en: str = Field(..., description="How the buckets were formed, in English.")
    generated_at: datetime = Field(..., description="When this answer was computed (UTC).")


class RegionStat(ApiModel):
    """Charging infrastructure of one federal state, absolute and per area."""

    bundesland: Bundesland = Field(..., description="ISO 3166-2:DE code.", examples=["BY"])
    label_de: str = Field(..., description="Official German name.", examples=["Bayern"])
    area_km2: float = Field(
        ...,
        description="Land area in km², from the Destatis figures used across the project.",
        examples=[70542.0],
    )
    stations: int = Field(..., description="Charging sites in the state.")
    fast_stations: int = Field(..., description="Of those, sites reaching 50 kW.")
    ultra_fast_stations: int = Field(..., description="Of those, sites reaching 150 kW.")
    charging_points: int = Field(..., description="Connectors across all sites.")
    fast_charging_points: int = Field(..., description="Connectors rated 50 kW or more.")
    installed_power_kw: float = Field(
        ...,
        description="Installed power in kW (site total where published, else strongest plug).",
    )
    operators: int = Field(..., description="Distinct operators active in the state.")
    max_power_kw: float | None = Field(
        default=None,
        description="Strongest single site in the state, in kW.",
    )
    median_station_power_kw: float | None = Field(
        default=None,
        description=(
            "Median site power. Together with `max_power_kw` it describes the *shape* of a "
            "state's network rather than its size: two states with the same site count and very "
            "different medians have built very different things."
        ),
    )
    stations_per_1000_km2: float = Field(
        ...,
        description="Sites per 1 000 km² — the figure that makes Bremen and Bayern comparable.",
    )
    fast_stations_per_1000_km2: float = Field(..., description="Fast sites per 1 000 km².")
    charging_points_per_1000_km2: float = Field(..., description="Connectors per 1 000 km².")
    share_of_resolved_stations_percent: float = Field(
        ...,
        description="Share of all sites with a resolved federal state that sit in this one.",
    )


class RegionComparison(ApiModel):
    """The sixteen federal states side by side (BUILD_SPEC §7)."""

    regions: list[RegionStat] = Field(
        ...,
        description=(
            "All sixteen states, densest first. A state with no charging site appears with "
            "zeros rather than vanishing — which is the entire point of a regional comparison."
        ),
    )
    stations_total: int = Field(..., description="Sites in the register snapshot.")
    stations_with_resolved_bundesland: int = Field(
        ...,
        description="Sites the register's spelling could be resolved to a state — the base of "
        "`regions` and of `share_of_resolved_stations_percent`.",
    )
    stations_without_bundesland: int = Field(
        ...,
        description=(
            "Sites with no resolvable state. Counted here rather than shown as a seventeenth "
            "region, because there is no honest state to attribute them to and a row labelled "
            "'unknown' would be read as one."
        ),
    )
    data_origin: DataOrigin = Field(
        ...,
        description="Always `official`: this is the Bundesnetzagentur register.",
        examples=["official"],
    )
    area_source: str = Field(
        ...,
        description="Where the state areas come from, so the densities can be checked.",
    )
    generated_at: datetime = Field(..., description="When this answer was computed (UTC).")
