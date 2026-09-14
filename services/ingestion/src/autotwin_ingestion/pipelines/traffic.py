"""Autobahn GmbH roadworks, closures and warnings → ``traffic_events``.

Upsert on ``external_id``, and — the decision worth spelling out — **events are never
deleted**.

The Autobahn API publishes what is disrupted *right now*. An event that was in yesterday's
answer and is missing from today's has not been retracted: it happened, it affected traffic,
and a trip analysed against it was analysed correctly. Deleting it would quietly rewrite
history and make yesterday's route analysis unreproducible, which is the opposite of what a
digital twin is for.

So a vanished event is **closed, not removed**: if it carries no ``ends_at``, the sweep at the
end of a run sets ``ends_at`` to the moment the disappearance was *observed*. That timestamp is
therefore an upper bound on when the disruption really ended — the API does not say — and it is
the value ``active_only=true`` filters on. The rule for the sweep:

* only events written by this source (``source = autobahn``),
* only on roads that answered this poll with at least one event — a road whose request
  failed returns nothing, and sweeping it would turn a network error into a fabricated end
  timestamp,
* only ones still open (``ends_at IS NULL``),
* and **never in fixture mode**, because the bundled sample covers a single road and answers
  every other road with that road's events; a sweep would then "end" every event on every road
  the sample does not describe.

Timestamps are deliberately *not* checked against the clock. A roadworks zone starting in three
weeks is the normal case for this source (``startTimestamp`` is simply absent for short-term
roadworks), so ``timestamp_not_future`` would reject exactly the rows that matter most.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID

import polars as pl
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from autotwin_contracts import (
    BoundingBox,
    ProviderMode,
    SourceSystem,
    TrafficEventRecord,
    utc_now,
)
from autotwin_core.db import TrafficEvent, session_scope
from autotwin_core.db.types import to_shape_linestring, to_shape_point
from autotwin_core.logging import get_logger
from autotwin_core.providers import TrafficProvider, close_provider
from autotwin_core.quality import (
    RecordValidator,
    no_duplicate_key,
    required_field,
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
from autotwin_ingestion.providers.traffic import DEFAULT_ROADS, AutobahnTrafficProvider

__all__ = [
    "BRONZE_DATASET",
    "BRONZE_SCHEMA",
    "SILVER_DATASET",
    "SILVER_SCHEMA",
    "TrafficIngestionPipeline",
    "traffic_event_rules",
]

_logger = get_logger(__name__)

BRONZE_DATASET: Final[str] = "traffic_events"
"""Bronze dataset: every event the adapter returned, accepted or not."""

SILVER_DATASET: Final[str] = "traffic_events"
"""Silver dataset: accepted events only."""

SILVER_SCHEMA: Final[dict[str, pl.DataType]] = {
    "external_id": pl.String(),
    "event_type": pl.String(),
    "severity": pl.String(),
    "road_name": pl.String(),
    "direction": pl.String(),
    "title": pl.String(),
    "description": pl.String(),
    "latitude": pl.Float64(),
    "longitude": pl.Float64(),
    "geometry_points": pl.Int32(),
    "starts_at": pl.Datetime("us", "UTC"),
    "ends_at": pl.Datetime("us", "UTC"),
    "is_blocked": pl.Boolean(),
    "delay_minutes": pl.Float64(),
    "source": pl.String(),
    "source_url": pl.String(),
    "data_origin": pl.String(),
    "ingested_at": pl.Datetime("us", "UTC"),
}
"""Column types of the silver dataset, stated rather than inferred.

``starts_at`` is absent from every ``SHORT_TERM_ROADWORKS`` item and ``delay_minutes`` from
almost all of them, so a batch of short-term roadworks would otherwise type those columns
``Null`` and then refuse the next batch that has values.
"""

BRONZE_SCHEMA: Final[dict[str, pl.DataType]] = {
    **SILVER_SCHEMA,
    "quality_status": pl.String(),
}
"""Silver's columns plus the verdict of the quality rules."""

_EVENT_COLUMNS: Final[tuple[str, ...]] = (
    "event_type",
    "severity",
    "road_name",
    "direction",
    "title",
    "description",
    "location",
    "geometry",
    "starts_at",
    "ends_at",
    "is_blocked",
    "delay_minutes",
    "raw",
    "source",
    "source_identifier",
    "source_url",
    "source_timestamp",
    "data_origin",
    "ingestion_run_id",
    "ingested_at",
)
"""Columns refreshed when an event is re-published.

Everything, unconditionally: a re-published event is the source's current statement about a
live disruption, and its severity, extent and end time are exactly what changes between polls.
"""

_MIN_LINESTRING_VERTICES: Final[int] = 2
"""Vertices a PostGIS LineString needs; below it an event keeps only its representative point."""


def traffic_event_rules() -> RecordValidator:
    """The rule set for Autobahn events (BUILD_SPEC §6).

    * ``external_id`` — the upsert key; an event without one cannot be tracked across polls,
      and re-inserting it every run would fill the table with copies of the same roadworks.
    * ``title`` — the only text the map marker has; a blank one means the payload changed.
    * ``within_germany_bbox`` — the API's ``coordinate`` key is ``long``, not ``lon``, and a
      parser reading the wrong key silently produces a point in the Atlantic.
    * ``no_duplicate_key`` — one row per id per statement, which PostgreSQL requires.
    """
    return RecordValidator(
        rules=[
            required_field("external_id"),
            required_field("title"),
            within_germany_bbox(),
            no_duplicate_key(lambda record: record.external_id, field="external_id"),
        ],
        source_label="autobahn_traffic_events",
        context_fn=lambda record: str(getattr(record, "external_id", "")) or None,
    )


class TrafficIngestionPipeline(IngestionPipeline):
    """Ingests the Autobahn GmbH traffic feed."""

    name = "autobahn_traffic_events"
    source = SourceSystem.autobahn

    def __init__(
        self,
        *,
        provider: TrafficProvider | None = None,
        roads: tuple[str, ...] | None = None,
        bbox: BoundingBox | None = None,
        close_vanished: bool = True,
        **kwargs: Any,
    ) -> None:
        """Configure the pipeline.

        Args:
            provider: Adapter to read from; defaults to an
                :class:`~autotwin_ingestion.providers.traffic.AutobahnTrafficProvider` bound to
                this run's data mode and road list.
            roads: Road ids to poll — the CLI's ``--roads A5,A8``. Defaults to A1 through A9.
            bbox: Optional spatial filter applied by the adapter.
            close_vanished: Whether to close events that disappeared from the source. Left on
                except where a caller knows the poll was not exhaustive.
            **kwargs: Forwarded to :class:`IngestionPipeline`.
        """
        super().__init__(**kwargs)
        self._bbox = bbox
        self._close_vanished = close_vanished
        self._roads = tuple(roads) if roads else DEFAULT_ROADS
        if provider is not None:
            self._provider: TrafficProvider = provider
        else:
            self._provider = AutobahnTrafficProvider(
                settings=self.settings,
                data_mode=self.data_mode,
                roads=self._roads,
            )
        self._owns_provider = provider is None

    async def aclose(self) -> None:
        """Release the provider if this pipeline created it."""
        if self._owns_provider:
            await close_provider(self._provider)

    async def _execute(self, context: RunContext) -> PipelineOutcome:
        """Fetch the feed, upsert it, and close whatever vanished from it."""
        result = await self._provider.fetch_events(self._bbox)
        outcome = PipelineOutcome(
            mode=result.mode,
            source_url=result.source_url,
            warnings=list(result.warnings),
        )
        outcome.details["roads polled"] = ",".join(self._roads)

        validator = traffic_event_rules()
        accepted, report = validator.validate(result.data)
        outcome.report = report
        accepted_ids = {id(record) for record in accepted}

        if not context.writes_enabled:
            return outcome

        self._write_lake(context, list(result.data), accepted, accepted_ids)
        context.lake.write_snapshot(
            [record.model_dump(mode="json") for record in result.data],
            source=SourceSystem.autobahn.value,
            filename=f"events_{context.started_at:%Y%m%dT%H%M%SZ}.json",
            source_url=result.source_url,
            fetched_at=result.fetched_at,
        )

        observed_at = utc_now()
        outcome.rows_written = await self._upsert(accepted, context.run_id, observed_at)
        outcome.rows_closed = await self._close_vanished_events(
            accepted,
            mode=result.mode,
            observed_at=observed_at,
        )
        return outcome

    # ------------------------------------------------------------------ persistence

    async def _upsert(
        self,
        records: list[TrafficEventRecord],
        run_id: UUID | None,
        observed_at: datetime,
    ) -> int:
        """Upsert events on ``external_id``, returning how many rows were written."""
        if run_id is None or not records:
            return 0
        written = 0
        async with session_scope() as session:
            for batch in batched(records, DEFAULT_UPSERT_BATCH):
                statement = pg_insert(TrafficEvent).values(
                    [_event_values(record, run_id, observed_at) for record in batch]
                )
                excluded = statement.excluded
                upsert = statement.on_conflict_do_update(
                    index_elements=[TrafficEvent.external_id],
                    set_={column: getattr(excluded, column) for column in _EVENT_COLUMNS},
                ).returning(TrafficEvent.id)
                written += len((await session.execute(upsert)).all())
        return written

    async def _close_vanished_events(
        self,
        records: list[TrafficEventRecord],
        *,
        mode: ProviderMode,
        observed_at: datetime,
    ) -> int:
        """Mark still-open events that this poll no longer sees as ended.

        Returns the number of rows closed. See the module docstring for why this closes rather
        than deletes, and why fixture mode is exempt.

        The sweep is restricted to roads that **answered with at least one event**. One call
        fans out to ``roads x 3`` HTTP requests and a single failed one simply yields no
        events for that road; sweeping it anyway would turn a network error into a fabricated
        end timestamp on every roadworks zone of that Autobahn. The cost of the restriction is
        that a road which genuinely cleared keeps its events open until its next non-empty
        poll — a stale ``ends_at IS NULL`` is a far smaller lie than an invented ``ends_at``,
        and on A1 through A9 a road with no disruption at all is close to hypothetical.
        """
        if not self._close_vanished:
            return 0
        if mode is ProviderMode.fixture:
            _logger.info(
                "ingest.traffic.sweep_skipped",
                pipeline=self.name,
                reason="fixture mode answers every road from one bundled sample",
            )
            return 0

        answered = {record.road_name for record in records if record.road_name}
        swept = sorted(answered.intersection(self._roads))
        if not swept:
            _logger.info(
                "ingest.traffic.sweep_skipped",
                pipeline=self.name,
                reason="no polled road returned an event, so none can be shown to have cleared",
            )
            return 0

        present = [record.external_id for record in records if record.external_id]
        condition = sa.and_(
            TrafficEvent.source == SourceSystem.autobahn,
            TrafficEvent.ends_at.is_(None),
            TrafficEvent.road_name.in_(swept),
        )
        if present:
            condition = sa.and_(condition, TrafficEvent.external_id.notin_(present))

        async with session_scope() as session:
            result = await session.execute(
                sa.update(TrafficEvent)
                .where(condition)
                .values(ends_at=observed_at)
                .returning(TrafficEvent.id)
            )
            closed = len(result.all())
        if closed:
            _logger.info(
                "ingest.traffic.events_closed",
                pipeline=self.name,
                count=closed,
                roads=",".join(swept),
                observed_at=observed_at.isoformat(),
            )
        return closed

    def _write_lake(
        self,
        context: RunContext,
        received: list[TrafficEventRecord],
        accepted: list[TrafficEventRecord],
        accepted_ids: set[int],
    ) -> None:
        """Mirror the batch into bronze (everything) and silver (accepted only)."""
        partition = context.started_at.date()
        context.lake.write_table(
            [
                {
                    **_lake_row(record),
                    "quality_status": "accepted" if id(record) in accepted_ids else "rejected",
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


def _event_values(
    record: TrafficEventRecord,
    run_id: UUID,
    ingested_at: datetime,
) -> dict[str, Any]:
    """Render an event as ``traffic_events`` column values."""
    vertices = list(record.geometry)
    # A one-vertex "line" is what the API publishes for a point event; PostGIS accepts it in
    # some code paths and then fails much later inside ST_Length, so it is dropped here.
    geometry = to_shape_linestring(vertices) if len(vertices) >= _MIN_LINESTRING_VERTICES else None
    return {
        "external_id": record.external_id,
        "event_type": record.event_type,
        "severity": record.severity,
        "road_name": record.road_name,
        "direction": record.direction,
        "title": record.title,
        "description": record.description,
        "location": to_shape_point(record.coordinate.latitude, record.coordinate.longitude),
        "geometry": geometry,
        "starts_at": record.starts_at,
        "ends_at": record.ends_at,
        "is_blocked": record.is_blocked,
        "delay_minutes": record.delay_minutes,
        "raw": record.raw,
        **provenance_columns(record.provenance, run_id=run_id, ingested_at=ingested_at),
    }


def _lake_row(record: TrafficEventRecord) -> dict[str, Any]:
    """Render an event as the flat analytical row of the Parquet zones."""
    return {
        "external_id": record.external_id,
        "event_type": record.event_type.value,
        "severity": record.severity.value,
        "road_name": record.road_name,
        "direction": record.direction,
        "title": record.title,
        "description": record.description,
        "latitude": record.coordinate.latitude,
        "longitude": record.coordinate.longitude,
        "geometry_points": len(record.geometry),
        "starts_at": record.starts_at,
        "ends_at": record.ends_at,
        "is_blocked": record.is_blocked,
        "delay_minutes": record.delay_minutes,
        "source": record.provenance.source.value,
        "source_url": record.provenance.source_url,
        "data_origin": record.provenance.data_origin.value,
        "ingested_at": record.provenance.ingested_at,
    }
