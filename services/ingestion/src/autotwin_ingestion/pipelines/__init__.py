"""Ingestion pipelines — the layer that turns provider records into PostGIS rows.

Four pipelines, one template (:mod:`autotwin_ingestion.pipelines.base`):

=================================  =======================================================
pipeline                           writes
=================================  =======================================================
:class:`ChargingIngestionPipeline` ``charging_stations`` + ``charging_points``
:class:`WeatherIngestionPipeline`  ``weather_stations`` + ``weather_observations``
:class:`TrafficIngestionPipeline`  ``traffic_events``
:class:`RouteSeedPipeline`         ``routes`` + ``route_segments``
=================================  =======================================================

Each of them opens a ``data_ingestion_runs`` row before fetching, records the provider's mode
truthfully, rejects rows that fail the quality rules of BUILD_SPEC §6 instead of writing them,
upserts on a natural key, and closes the run row with the counters and the quality report —
including on failure. The template enforces that; a pipeline body only fetches, validates and
upserts::

    pipeline = ChargingIngestionPipeline(data_mode=DataMode.fixture, limit=500)
    result = await pipeline.run()
    print("\\n".join(result.summary_lines()))
"""

from __future__ import annotations

from autotwin_ingestion.pipelines.base import (
    DEFAULT_UPSERT_BATCH,
    IngestionPipeline,
    IngestionResult,
    PipelineOutcome,
    RunContext,
    StreamingValidation,
)
from autotwin_ingestion.pipelines.charging import (
    ChargingIngestionPipeline,
    charging_station_rules,
)
from autotwin_ingestion.pipelines.routes import (
    DEMO_CORRIDORS,
    DemoCorridor,
    RouteSeedPipeline,
)
from autotwin_ingestion.pipelines.traffic import (
    TrafficIngestionPipeline,
    traffic_event_rules,
)
from autotwin_ingestion.pipelines.weather import (
    WeatherIngestionPipeline,
    weather_observation_rules,
)

__all__ = [
    "DEFAULT_UPSERT_BATCH",
    "DEMO_CORRIDORS",
    "ChargingIngestionPipeline",
    "DemoCorridor",
    "IngestionPipeline",
    "IngestionResult",
    "PipelineOutcome",
    "RouteSeedPipeline",
    "RunContext",
    "StreamingValidation",
    "TrafficIngestionPipeline",
    "WeatherIngestionPipeline",
    "charging_station_rules",
    "traffic_event_rules",
    "weather_observation_rules",
]
