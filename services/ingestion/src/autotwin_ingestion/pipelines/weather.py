"""DWD 10-minute observations → ``weather_stations`` + ``weather_observations``.

Two tables, two upsert keys, one run. The station catalogue is metadata — stations do not move
— and is upserted on ``dwd_station_id``; the observations are facts about a moment and are
upserted on ``(weather_station_id, observed_at)``, which is what makes re-running the pipeline
every ten minutes idempotent instead of duplicating a station's current reading every time.

**Which stations get read** is decided by geography, not by a list. The DWD network has around
a thousand 10-minute stations; contacting all of them for a demo would be both pointless and
impolite towards a free public-sector file server. The pipeline therefore samples the demo
corridors already in ``routes`` — start, end and a handful of points along each — and asks the
adapter for the nearest station to each, capped at ``--stations``. Before any corridor exists
(a first run, or ``ingest weather`` on an empty database) it falls back to the corridor
endpoints in code, so the command works standalone and still lands on the same stations.

**The DWD attribution is not optional.** „Quelle: Deutscher Wetterdienst" is the first warning
on every provider result and is carried into the run's warnings so that it reaches the CLI
summary and the data-quality page (see ``DATA_LICENSES.md``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

import polars as pl
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_contracts import (
    Coordinate,
    SourceSystem,
    WeatherRecord,
    WeatherStationRecord,
    utc_now,
)
from autotwin_core.db import Route, WeatherObservation, WeatherStation, session_scope
from autotwin_core.db.types import from_wkb_linestring, to_shape_point
from autotwin_core.logging import get_logger
from autotwin_core.providers import WeatherProvider, close_provider
from autotwin_core.quality import (
    RecordValidator,
    no_duplicate_key,
    numeric_range,
    required_field,
    timestamp_not_future,
    timestamp_present,
    within_germany_bbox,
)
from autotwin_ingestion.lake import LakeZone
from autotwin_ingestion.pipelines.base import (
    DEFAULT_UPSERT_BATCH,
    IngestionPipeline,
    PipelineOutcome,
    RunContext,
    batched,
    provenance_columns,
)
from autotwin_ingestion.providers.weather import DWDWeatherProvider

__all__ = [
    "BRONZE_DATASET",
    "BRONZE_SCHEMA",
    "FALLBACK_SAMPLE_POINTS",
    "SILVER_DATASET",
    "SILVER_SCHEMA",
    "WeatherIngestionPipeline",
    "weather_observation_rules",
]

_logger = get_logger(__name__)

BRONZE_DATASET: Final[str] = "weather_observations"
"""Bronze dataset: every observation the adapter returned, accepted or not."""

SILVER_DATASET: Final[str] = "weather_observations"
"""Silver dataset: accepted observations only."""

SILVER_SCHEMA: Final[dict[str, pl.DataType]] = {
    "station_id": pl.String(),
    "observed_at": pl.Datetime("us", "UTC"),
    "latitude": pl.Float64(),
    "longitude": pl.Float64(),
    "temperature_c": pl.Float64(),
    "precipitation_mm": pl.Float64(),
    "wind_speed_ms": pl.Float64(),
    "wind_gust_ms": pl.Float64(),
    "humidity_percent": pl.Float64(),
    "pressure_hpa": pl.Float64(),
    "condition": pl.String(),
    "source": pl.String(),
    "source_url": pl.String(),
    "data_origin": pl.String(),
    "ingested_at": pl.Datetime("us", "UTC"),
}
"""Column types of the silver dataset, stated rather than inferred.

DWD station coverage is uneven — a station reporting temperature but neither wind nor gusts is
normal — so an inferred schema would type those columns ``Null`` whenever the first batch
happens to miss them, and reject the first real value later.
"""

BRONZE_SCHEMA: Final[dict[str, pl.DataType]] = {
    **SILVER_SCHEMA,
    "quality_status": pl.String(),
}
"""Silver's columns plus the verdict of the quality rules."""

FALLBACK_SAMPLE_POINTS: Final[tuple[Coordinate, ...]] = (
    Coordinate(latitude=50.1109, longitude=8.6821),  # Frankfurt am Main
    Coordinate(latitude=48.7784, longitude=9.1800),  # Stuttgart
    Coordinate(latitude=48.1371, longitude=11.5754),  # München
    Coordinate(latitude=48.7630, longitude=11.4250),  # Ingolstadt
    Coordinate(latitude=52.4206, longitude=10.7862),  # Wolfsburg
    Coordinate(latitude=52.5174, longitude=13.3951),  # Berlin
)
"""The demo corridor endpoints, used when no route has been seeded yet.

Identical to the endpoints :mod:`autotwin_ingestion.pipelines.routes` seeds, so a weather run
before and after ``seed routes`` resolves to the same stations and the observation history
stays continuous.
"""

_POINTS_PER_ROUTE: Final[int] = 4
"""Sample points taken along each seeded corridor, endpoints included.

Four points over a 200 km corridor puts a station roughly every 60 km, which is the scale at
which German weather actually varies along a motorway — a finer grid would buy nothing but
downloads.
"""

_MIN_TEMPERATURE_C: Final[float] = -50.0
_MAX_TEMPERATURE_C: Final[float] = 60.0
"""Physically possible air temperatures in Germany, generously bounded.

The DWD missing-value sentinel is ``-999``; the adapter converts it, and this rule is the
backstop that catches an edition where it did not.
"""

_FUTURE_TOLERANCE_S: Final[float] = 900.0
"""Fifteen minutes. One 10-minute interval plus clock skew — beyond that, a parsing error."""

_OBSERVATION_COLUMNS: Final[tuple[str, ...]] = (
    "location",
    "temperature_c",
    "precipitation_mm",
    "wind_speed_ms",
    "wind_gust_ms",
    "humidity_percent",
    "pressure_hpa",
    "condition",
    "source",
    "source_identifier",
    "source_url",
    "source_timestamp",
    "data_origin",
    "ingestion_run_id",
    "ingested_at",
)
"""Columns refreshed when an observation for ``(station, timestamp)`` is re-read.

A re-read of the same ten-minute interval is normal — the ``now`` product is republished as
late measurements arrive — so the newer reading wins outright rather than being compared
column by column.
"""

_STATION_COLUMNS: Final[tuple[str, ...]] = (
    "name",
    "location",
    "elevation_m",
    "bundesland",
    "valid_from",
    "valid_to",
)
"""Columns refreshed when a station reappears in the catalogue."""


def weather_observation_rules() -> RecordValidator:
    """The rule set for DWD observations (BUILD_SPEC §6).

    ``station_id`` is required even though the record type allows it to be absent: the upsert
    key is ``(weather_station_id, observed_at)``, and PostgreSQL treats two NULL station ids as
    distinct, so a station-less observation would insert a fresh duplicate on every run instead
    of updating one row. The DWD always states the station; a record without one means the
    product file changed shape.
    """
    return RecordValidator(
        rules=[
            required_field("station_id"),
            timestamp_present("observed_at"),
            timestamp_not_future("observed_at", tolerance_s=_FUTURE_TOLERANCE_S),
            within_germany_bbox(),
            numeric_range("temperature_c", _MIN_TEMPERATURE_C, _MAX_TEMPERATURE_C),
            no_duplicate_key(
                lambda record: (record.station_id, record.observed_at),
                field="station_id",
            ),
        ],
        source_label="dwd_weather_observations",
        context_fn=lambda record: str(getattr(record, "station_id", "")) or None,
    )


class WeatherIngestionPipeline(IngestionPipeline):
    """Ingests the DWD station catalogue and the observations along the demo corridors."""

    name = "dwd_weather_observations"
    source = SourceSystem.dwd

    def __init__(
        self,
        *,
        provider: WeatherProvider | None = None,
        max_stations: int | None = None,
        points: tuple[Coordinate, ...] | None = None,
        at: datetime | None = None,
        **kwargs: Any,
    ) -> None:
        """Configure the pipeline.

        Args:
            provider: Adapter to read from; defaults to a
                :class:`~autotwin_ingestion.providers.weather.DWDWeatherProvider` bound to this
                run's data mode and station cap.
            max_stations: Upper bound on stations contacted — the CLI's ``--stations``.
            points: Locations to cover. ``None`` samples the seeded corridors, then
                :data:`FALLBACK_SAMPLE_POINTS`.
            at: Observation time; ``None`` takes each station's most recent reading.
            **kwargs: Forwarded to :class:`IngestionPipeline`.

        Raises:
            ValueError: ``max_stations`` is given and not positive.
        """
        super().__init__(**kwargs)
        if max_stations is not None and max_stations < 1:
            msg = f"max_stations must be positive, got {max_stations!r}"
            raise ValueError(msg)
        self._points = points
        self._at = at
        if provider is not None:
            self._provider: WeatherProvider = provider
        elif max_stations is None:
            self._provider = DWDWeatherProvider(
                settings=self.settings,
                data_mode=self.data_mode,
            )
        else:
            self._provider = DWDWeatherProvider(
                settings=self.settings,
                data_mode=self.data_mode,
                max_stations=max_stations,
            )
        self._owns_provider = provider is None

    async def aclose(self) -> None:
        """Release the provider if this pipeline created it."""
        if self._owns_provider:
            await close_provider(self._provider)

    async def _execute(self, context: RunContext) -> PipelineOutcome:
        """Seed the station catalogue, then read and persist the observations."""
        outcome = PipelineOutcome()

        stations_written, station_ids, catalogue_warnings = await self._ingest_stations(context)
        outcome.details["stations known"] = len(station_ids)
        outcome.details["stations upserted"] = stations_written
        outcome.warnings.extend(catalogue_warnings)

        points = await self._sample_points()
        result = await self._provider.fetch_observations(points, self._at)
        outcome.mode = result.mode
        outcome.source_url = result.source_url
        outcome.warnings.extend(result.warnings)
        outcome.details["points sampled"] = len(points)

        validator = weather_observation_rules()
        accepted, report = validator.validate(result.data)
        outcome.report = report

        accepted_ids = {id(record) for record in accepted}
        if context.writes_enabled:
            self._write_lake(context, list(result.data), accepted, accepted_ids)
            context.lake.write_snapshot(
                [record.model_dump(mode="json") for record in result.data],
                source=SourceSystem.dwd.value,
                filename=f"observations_{context.started_at:%Y%m%dT%H%M%SZ}.json",
                source_url=result.source_url,
                fetched_at=result.fetched_at,
            )
            outcome.rows_written = await self._upsert_observations(
                accepted, station_ids, context.run_id
            )
        if accepted:
            outcome.details["observed at"] = max(
                record.observed_at for record in accepted
            ).isoformat()
        return outcome

    # ------------------------------------------------------------------ stations

    async def _ingest_stations(self, context: RunContext) -> tuple[int, dict[str, UUID], list[str]]:
        """Upsert the catalogue and return ``(rows written, id by DWD id, warnings)``.

        The id map is what the observation upsert needs: ``weather_observations`` points at
        the ``weather_stations`` UUID, while every record only knows the five-digit DWD id.
        """
        if not isinstance(self._provider, DWDWeatherProvider):
            # Only the DWD models stations as first-class objects; another adapter's
            # observations are written against whatever catalogue is already persisted.
            return 0, await self._known_station_ids(), []

        result = await self._provider.fetch_stations()
        stations: list[WeatherStationRecord] = list(result.data)
        if not context.writes_enabled:
            return 0, await self._known_station_ids(), list(result.warnings)

        written = 0
        async with session_scope() as session:
            for batch in batched(stations, DEFAULT_UPSERT_BATCH):
                statement = pg_insert(WeatherStation).values(
                    [_station_values(record) for record in batch]
                )
                excluded = statement.excluded
                upsert = statement.on_conflict_do_update(
                    index_elements=[WeatherStation.dwd_station_id],
                    set_={column: getattr(excluded, column) for column in _STATION_COLUMNS},
                ).returning(WeatherStation.id)
                written += len((await session.execute(upsert)).all())
        _logger.info(
            "ingest.weather.catalogue",
            pipeline=self.name,
            stations=len(stations),
            upserted=written,
            mode=result.mode.value,
        )
        return written, await self._known_station_ids(), list(result.warnings)

    @staticmethod
    async def _known_station_ids() -> dict[str, UUID]:
        """Map every persisted ``dwd_station_id`` to its row id."""
        async with session_scope() as session:
            rows = await session.execute(
                sa.select(WeatherStation.dwd_station_id, WeatherStation.id)
            )
            return {str(dwd_id): identifier for dwd_id, identifier in rows.all()}

    # ------------------------------------------------------------------ sampling

    async def _sample_points(self) -> tuple[Coordinate, ...]:
        """Points to cover: the configured ones, else the seeded corridors, else the fallback."""
        if self._points:
            return self._points
        corridor_points = await self._corridor_points()
        if corridor_points:
            return corridor_points
        return FALLBACK_SAMPLE_POINTS

    async def _corridor_points(self) -> tuple[Coordinate, ...]:
        """Sample :data:`_POINTS_PER_ROUTE` evenly spaced vertices from each demo corridor.

        Vertex sampling rather than distance interpolation: the geometry is dense enough that
        the difference is metres, and picking vertices keeps this a pure read of what is
        already in the database with no geometry maths in the query.
        """
        async with session_scope() as session:
            rows = await session.execute(
                sa.select(Route.geometry).where(Route.is_demo.is_(True)).order_by(Route.slug)
            )
            geometries = [row[0] for row in rows.all()]

        points: list[Coordinate] = []
        seen: set[tuple[float, float]] = set()
        for geometry in geometries:
            vertices = from_wkb_linestring(geometry)
            if not vertices:
                continue
            step = max(1, (len(vertices) - 1) // max(1, _POINTS_PER_ROUTE - 1))
            sampled = [*vertices[::step], vertices[-1]]
            for vertex in sampled:
                key = (round(vertex.latitude, 3), round(vertex.longitude, 3))
                if key in seen:
                    continue
                seen.add(key)
                points.append(vertex)
        return tuple(points)

    # ------------------------------------------------------------------ persistence

    async def _upsert_observations(
        self,
        records: list[WeatherRecord],
        station_ids: dict[str, UUID],
        run_id: UUID | None,
    ) -> int:
        """Upsert observations on ``(weather_station_id, observed_at)``."""
        if run_id is None or not records:
            return 0
        ingested_at = utc_now()
        values = [
            _observation_values(record, station_ids, run_id, ingested_at)
            for record in records
            if record.station_id in station_ids
        ]
        missing = len(records) - len(values)
        if missing:
            _logger.warning(
                "ingest.weather.station_unknown",
                pipeline=self.name,
                count=missing,
                detail="observations whose station is not in weather_stations were skipped",
            )
        if not values:
            return 0

        written = 0
        async with session_scope() as session:
            for batch in batched(values, DEFAULT_UPSERT_BATCH):
                written += await self._upsert_batch(session, batch)
        return written

    @staticmethod
    async def _upsert_batch(session: AsyncSession, values: list[dict[str, Any]]) -> int:
        """Execute one observation upsert statement."""
        statement = pg_insert(WeatherObservation).values(values)
        excluded = statement.excluded
        upsert = statement.on_conflict_do_update(
            index_elements=[
                WeatherObservation.weather_station_id,
                WeatherObservation.observed_at,
            ],
            set_={column: getattr(excluded, column) for column in _OBSERVATION_COLUMNS},
        ).returning(WeatherObservation.id)
        return len((await session.execute(upsert)).all())

    def _write_lake(
        self,
        context: RunContext,
        received: list[WeatherRecord],
        accepted: list[WeatherRecord],
        accepted_ids: set[int],
    ) -> None:
        """Mirror the batch into bronze (everything) and silver (accepted only)."""
        partition = context.started_at.date()
        context.lake.write_table(
            [
                {
                    **_lake_row(record),
                    "quality_status": ("accepted" if id(record) in accepted_ids else "rejected"),
                }
                for record in received
            ],
            zone=LakeZone.bronze,
            dataset=BRONZE_DATASET,
            partition=partition,
            schema=BRONZE_SCHEMA,
        )
        context.lake.write_table(
            [_lake_row(record) for record in accepted],
            zone=LakeZone.silver,
            dataset=SILVER_DATASET,
            partition=partition,
            schema=SILVER_SCHEMA,
        )


def _station_values(record: WeatherStationRecord) -> dict[str, Any]:
    """Render a catalogue entry as ``weather_stations`` column values."""
    return {
        "dwd_station_id": record.dwd_station_id,
        "name": record.name,
        "location": to_shape_point(record.coordinate.latitude, record.coordinate.longitude),
        "elevation_m": record.elevation_m,
        "bundesland": record.bundesland,
        "valid_from": record.valid_from,
        "valid_to": record.valid_to,
    }


def _observation_values(
    record: WeatherRecord,
    station_ids: dict[str, UUID],
    run_id: UUID,
    ingested_at: datetime,
) -> dict[str, Any]:
    """Render an observation as ``weather_observations`` column values."""
    return {
        "weather_station_id": station_ids[record.station_id] if record.station_id else None,
        "observed_at": record.observed_at,
        "location": to_shape_point(record.coordinate.latitude, record.coordinate.longitude),
        "temperature_c": record.temperature_c,
        "precipitation_mm": record.precipitation_mm,
        "wind_speed_ms": record.wind_speed_ms,
        "wind_gust_ms": record.wind_gust_ms,
        "humidity_percent": record.humidity_percent,
        "pressure_hpa": record.pressure_hpa,
        "condition": record.condition,
        **provenance_columns(record.provenance, run_id=run_id, ingested_at=ingested_at),
    }


def _lake_row(record: WeatherRecord) -> dict[str, Any]:
    """Render an observation as the flat analytical row of the Parquet zones."""
    return {
        "station_id": record.station_id,
        "observed_at": record.observed_at,
        "latitude": record.coordinate.latitude,
        "longitude": record.coordinate.longitude,
        "temperature_c": record.temperature_c,
        "precipitation_mm": record.precipitation_mm,
        "wind_speed_ms": record.wind_speed_ms,
        "wind_gust_ms": record.wind_gust_ms,
        "humidity_percent": record.humidity_percent,
        "pressure_hpa": record.pressure_hpa,
        "condition": record.condition.value,
        "source": record.provenance.source.value,
        "source_url": record.provenance.source_url,
        "data_origin": record.provenance.data_origin.value,
        "ingested_at": record.provenance.ingested_at,
    }
