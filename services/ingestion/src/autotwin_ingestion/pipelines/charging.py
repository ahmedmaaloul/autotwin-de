"""Ladesäulenregister → ``charging_stations`` + ``charging_points``.

The biggest ingestion in the project: about 117 000 sites and 300 000 connectors in a 55 MB
CSV, re-published monthly. Three decisions shape this module, and all three exist because of
that size.

**It streams.** The records are pulled one at a time from
:func:`~autotwin_ingestion.providers.charging.iter_records` and pushed into PostgreSQL in
batches of :data:`~autotwin_ingestion.pipelines.base.DEFAULT_UPSERT_BATCH`. Nothing ever holds
the whole register — not as Pydantic models, not as ORM objects. The upserts go through
SQLAlchemy Core so a batch is one statement with bound parameters rather than 2 000 flushed
identity-mapped objects.

**It upserts on ``external_id`` and only writes what changed.** The conflict clause carries an
``IS DISTINCT FROM`` predicate over every payload column, so re-ingesting an unchanged edition
updates zero rows, creates zero dead tuples and leaves ``updated_at`` alone. The edition date
(``source_timestamp``) is part of that predicate, so a genuinely new monthly edition does
refresh every row. Because an unchanged row produces no ``RETURNING`` output, the station's
connectors are only replaced when the station itself changed — which is what keeps a re-run
from deleting and re-inserting 300 000 child rows for nothing.

**It rejects rather than repairs.** A site outside the Germany bounding box is a swapped
latitude/longitude or a comma-decimal read as a thousands separator; a site with a zero power
rating is a mis-parsed column. Both are written to the bronze zone with the rule that fired
and kept out of PostgreSQL entirely (BUILD_SPEC §6), so the rejected population stays
analysable without ever polluting the map.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import polars as pl
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_contracts import (
    ChargingStationRecord,
    ProviderMode,
    SourceSystem,
    utc_now,
)
from autotwin_core.db import ChargingPoint, ChargingStation, session_scope
from autotwin_core.db.types import to_shape_point
from autotwin_core.logging import get_logger
from autotwin_core.providers import (
    ChargingInfrastructureProvider,
    FileCache,
    close_provider,
)
from autotwin_core.quality import (
    RecordValidator,
    Violation,
    no_duplicate_key,
    positive_power,
    required_field,
    within_germany_bbox,
)
from autotwin_ingestion.fixtures import BNETZA_CSV_FIXTURE, resolve_fixture
from autotwin_ingestion.lake import LakeZone, ParquetPartWriter, sha256_of
from autotwin_ingestion.pipelines.base import (
    DEFAULT_UPSERT_BATCH,
    IngestionPipeline,
    PipelineOutcome,
    RunContext,
    StreamingValidation,
    batched,
    provenance_columns,
)
from autotwin_ingestion.providers.charging import (
    BundesnetzagenturChargingProvider,
    iter_records,
)

__all__ = [
    "BRONZE_DATASET",
    "BRONZE_SCHEMA",
    "SILVER_DATASET",
    "SILVER_SCHEMA",
    "ChargingIngestionPipeline",
    "charging_station_rules",
]

_logger = get_logger(__name__)

BRONZE_DATASET: Final[str] = "charging_stations"
"""Bronze dataset: every parsed site, including the rejected ones, with the rule that fired."""

SILVER_DATASET: Final[str] = "charging_stations"
"""Silver dataset: accepted sites only, typed and ready to join."""

SILVER_SCHEMA: Final[dict[str, pl.DataType]] = {
    "external_id": pl.String(),
    "operator": pl.String(),
    "street": pl.String(),
    "house_number": pl.String(),
    "postal_code": pl.String(),
    "city": pl.String(),
    "bundesland": pl.String(),
    "country": pl.String(),
    "latitude": pl.Float64(),
    "longitude": pl.Float64(),
    "commissioned_on": pl.Date(),
    "charging_points_count": pl.Int32(),
    "max_power_kw": pl.Float64(),
    "total_power_kw": pl.Float64(),
    "charging_category": pl.String(),
    "is_fast_charger": pl.Boolean(),
    "connector_types": pl.String(),
    "source": pl.String(),
    "source_identifier": pl.String(),
    "source_url": pl.String(),
    "source_timestamp": pl.Datetime("us", "UTC"),
    "data_origin": pl.String(),
    "ingested_at": pl.Datetime("us", "UTC"),
}
"""Column types of the silver dataset, stated rather than inferred.

Polars infers a schema from the first rows of a batch, and several of these columns are
legitimately empty for the first few thousand sites of the register — ``house_number`` and
``commissioned_on`` above all. An inferred ``Null`` column then fails to accept the first real
value 40 000 rows later, which is a failure mode that only ever appears at full scale. Stating
the schema also makes the analytical contract of the table reviewable in one place.
"""

BRONZE_SCHEMA: Final[dict[str, pl.DataType]] = {
    **SILVER_SCHEMA,
    "quality_status": pl.String(),
    "quality_rule": pl.String(),
    "quality_rules": pl.String(),
    "quality_message": pl.String(),
}
"""Silver's columns plus the verdict of the quality rules — see :func:`_bronze_row`."""

_BNETZA_CACHE_NAMESPACE: Final[str] = "bnetza"
"""Cache namespace the Bundesnetzagentur adapter archives its download under.

Coupled to the adapter on purpose and only here: the pointer it writes is what tells this
pipeline *which file on disk* the provider just decided to serve, which is the one thing a
``ProviderResult`` full of parsed records cannot say. The fallback below (newest CSV in
``data/raw/bnetza``) keeps the pipeline working if that pointer is ever missing.
"""

_BNETZA_CACHE_POINTER_KEY: Final[str] = "ladesaeulenregister"
"""Key of that pointer."""

_MAX_STATION_POWER_KW: Final[float] = 1_000.0
"""Plausibility ceiling for a site's strongest connector — above it, a kW/W unit mix-up."""

# Payload columns compared by the upsert's change predicate. Deliberately excludes
# ``ingestion_run_id`` and ``ingested_at``: those describe the *reading* of the row, and
# including them would mark every row changed on every run, which is precisely the churn the
# predicate exists to avoid.
_STATION_CHANGE_COLUMNS: Final[tuple[str, ...]] = (
    "operator",
    "street",
    "house_number",
    "postal_code",
    "city",
    "bundesland",
    "country",
    "commissioned_on",
    "charging_points_count",
    "max_power_kw",
    "total_power_kw",
    "charging_category",
    "is_fast_charger",
    "raw",
    "source",
    "source_identifier",
    "source_url",
    "source_timestamp",
    "data_origin",
)

_STATION_UPDATE_COLUMNS: Final[tuple[str, ...]] = (
    *_STATION_CHANGE_COLUMNS,
    "location",
    "ingestion_run_id",
    "ingested_at",
)
"""Columns the conflict clause writes once the change predicate fires — the payload plus the
two columns that record *which run* observed it."""


@dataclass(frozen=True, slots=True)
class _SourceDocument:
    """Where the register was read from, and what the provider said about it."""

    path: Path | None
    """CSV on disk when the pipeline can stream it; ``None`` for a non-BNetzA provider."""

    mode: ProviderMode
    """The provider's own verdict on how it answered."""

    source_url: str | None
    """URL the file came from."""

    fetched_at: datetime
    """When the bytes were obtained (UTC)."""

    warnings: tuple[str, ...]
    """Provider warnings to carry into the run summary."""

    records: tuple[ChargingStationRecord, ...] | None = None
    """Pre-parsed records, for a provider that does not expose a file."""


def charging_station_rules() -> RecordValidator:
    """The rule set for the Ladesäulenregister (BUILD_SPEC §6).

    Each rule corresponds to a defect the real file actually contains:

    * ``external_id`` — the upsert key; without it a row cannot be written idempotently.
    * ``operator`` / ``city`` — a site with neither an operator nor a town cannot be shown or
      attributed, and the register does publish a handful of such rows.
    * ``within_germany_bbox`` — catches swapped lat/lon, a comma-decimal read as a thousands
      separator, and the 0/0 placeholder.
    * ``positive_power[max_power_kw]`` — the register uses an empty cell for "unknown", so a
      literal ``0`` means the column was mis-parsed; the ceiling catches kW/W confusion.
      ``None`` is tolerated: a site without a stated rating is still infrastructure.
    * ``no_duplicate_key`` — one ``external_id`` per run, so the batch upsert can never try to
      touch the same row twice inside one statement (PostgreSQL rejects that outright).
    """
    return RecordValidator(
        rules=[
            required_field("external_id"),
            required_field("operator"),
            required_field("city"),
            within_germany_bbox(),
            positive_power("max_power_kw", maximum_kw=_MAX_STATION_POWER_KW),
            no_duplicate_key(lambda record: record.external_id, field="external_id"),
        ],
        source_label="bnetza_charging_stations",
        context_fn=lambda record: str(getattr(record, "external_id", "")) or None,
    )


class ChargingIngestionPipeline(IngestionPipeline):
    """Ingests the Bundesnetzagentur Ladesäulenregister."""

    name = "bnetza_charging_stations"
    source = SourceSystem.bundesnetzagentur

    def __init__(
        self,
        *,
        provider: ChargingInfrastructureProvider | None = None,
        batch_size: int = DEFAULT_UPSERT_BATCH,
        **kwargs: Any,
    ) -> None:
        """Configure the pipeline.

        Args:
            provider: Adapter to read from. Defaults to a
                :class:`~autotwin_ingestion.providers.charging.BundesnetzagenturChargingProvider`
                bound to this run's data mode. Any other
                :class:`~autotwin_core.providers.base.ChargingInfrastructureProvider` also
                works — it is read through the list-returning interface instead of streamed
                from disk, which is fine for the row counts a test double produces.
            batch_size: Rows per upsert statement.
            **kwargs: Forwarded to :class:`IngestionPipeline`.

        Raises:
            ValueError: ``batch_size`` is not positive.
        """
        super().__init__(**kwargs)
        if batch_size < 1:
            msg = f"batch_size must be positive, got {batch_size!r}"
            raise ValueError(msg)
        self._batch_size = batch_size
        self._provider = provider or BundesnetzagenturChargingProvider(
            settings=self.settings,
            data_mode=self.data_mode,
        )
        self._owns_provider = provider is None

    async def aclose(self) -> None:
        """Release the provider if this pipeline created it."""
        if self._owns_provider:
            await close_provider(self._provider)

    async def _execute(self, context: RunContext) -> PipelineOutcome:
        """Resolve the register, stream it through validation, and upsert it."""
        document = await self._resolve_document(context)
        outcome = PipelineOutcome(
            mode=document.mode,
            source_url=document.source_url,
            warnings=list(document.warnings),
        )

        if document.path is not None:
            if context.writes_enabled:
                artifact = context.lake.adopt_raw(
                    document.path,
                    source=_BNETZA_CACHE_NAMESPACE,
                    source_url=document.source_url,
                    fetched_at=document.fetched_at,
                    content_type="text/csv; charset=utf-8",
                )
                outcome.bytes_downloaded = artifact.size_bytes
                outcome.source_file_sha256 = artifact.sha256
            else:
                # A dry run reports the same digest without writing a sidecar: it must leave
                # no trace anywhere, including in the raw zone.
                outcome.bytes_downloaded = document.path.stat().st_size
                outcome.source_file_sha256 = sha256_of(document.path)
            outcome.details["source file"] = document.path.name

        validation = StreamingValidation(charging_station_rules())
        written = 0
        partition = context.started_at.date()
        bronze = ParquetPartWriter(
            directory=context.lake.partition_dir(LakeZone.bronze, BRONZE_DATASET, partition),
            schema=BRONZE_SCHEMA,
        )
        silver = ParquetPartWriter(
            directory=context.lake.partition_dir(LakeZone.silver, SILVER_DATASET, partition),
            schema=SILVER_SCHEMA,
        )
        accepted = self._accepted(document, context, validation, bronze, silver)
        if not context.writes_enabled:
            # A dry run never opens a transaction: it answers "what would this file do to the
            # database", and opening one would make that question itself an event.
            for _ in accepted:
                pass
        else:
            # One transaction for the whole register. 117 000 upserts is a large but
            # unremarkable transaction for PostgreSQL, and the atomicity is worth it: a run
            # that dies half way must not leave the table describing two editions.
            async with session_scope() as session:
                for batch in batched(accepted, self._batch_size):
                    written += await self._upsert_batch(session, batch, context.run_id)
                # Flushed inside the transaction: without its bronze evidence the run is not
                # reproducible, so a lake failure must roll the rows back rather than leave
                # PostgreSQL and the lake disagreeing about what was ingested.
                bronze.close()
                silver.close()

        outcome.report = validation.report
        outcome.rows_written = written
        outcome.details["bronze parts"] = len(bronze.parts)
        outcome.details["silver parts"] = len(silver.parts)
        return outcome

    # ------------------------------------------------------------------ source resolution

    async def _resolve_document(self, context: RunContext) -> _SourceDocument:
        """Run the provider's fallback chain and find the CSV it decided to serve.

        The provider owns the chain — configured URL, landing-page scrape, date template,
        cache, fixture — and the one-record fetch below is what drives it. Parsing one record
        costs nothing next to the download it triggers, and the resulting
        :class:`~autotwin_core.providers.result.ProviderResult` is the authority on the mode
        this run reports.
        """
        if not isinstance(self._provider, BundesnetzagenturChargingProvider):
            result = await self._provider.fetch_stations()
            return _SourceDocument(
                path=None,
                mode=result.mode,
                source_url=result.source_url,
                fetched_at=result.fetched_at,
                warnings=tuple(result.warnings),
                records=tuple(result.data),
            )

        probe = await self._provider.fetch_stations(limit=1)
        path = self._locate_csv(probe.mode)
        if path is None:
            # Every rung of the chain failed to leave a file behind; fall back to the records
            # the probe itself produced rather than failing the run outright.
            result = await self._provider.fetch_stations(limit=context.limit)
            return _SourceDocument(
                path=None,
                mode=result.mode,
                source_url=result.source_url,
                fetched_at=result.fetched_at,
                warnings=(*result.warnings, "no register file on disk; parsed in memory"),
                records=tuple(result.data),
            )
        return _SourceDocument(
            path=path,
            mode=probe.mode,
            source_url=probe.source_url,
            fetched_at=probe.fetched_at,
            warnings=tuple(probe.warnings),
        )

    def _locate_csv(self, mode: ProviderMode) -> Path | None:
        """Find the CSV behind the provider's answer.

        Fixture mode resolves the bundled excerpt directly. Live and cache mode read the
        pointer the adapter writes into the provider cache, and fall back to the newest
        archived CSV in ``data/raw/bnetza`` if that pointer is gone.
        """
        if mode is ProviderMode.fixture:
            try:
                return resolve_fixture(BNETZA_CSV_FIXTURE)
            except FileNotFoundError:  # pragma: no cover - a broken installation
                return None

        pointer = FileCache().get_json(
            _BNETZA_CACHE_NAMESPACE,
            _BNETZA_CACHE_POINTER_KEY,
            allow_stale=True,
        )
        if isinstance(pointer, dict):
            candidate = Path(str(pointer.get("path", "")))
            if candidate.is_file():
                return candidate

        archive = Path(self.settings.raw_dir) / _BNETZA_CACHE_NAMESPACE
        archived = sorted(archive.glob("*.csv"), key=lambda path: path.stat().st_mtime)
        return archived[-1] if archived else None

    def _records(
        self, document: _SourceDocument, context: RunContext
    ) -> Iterator[ChargingStationRecord]:
        """Stream the register's records, from disk when there is a file to stream."""
        if document.path is None:
            records = document.records or ()
            yield from records[: context.limit] if context.limit else records
            return
        skipped: list[str] = []
        yield from iter_records(
            document.path,
            source_url=document.source_url,
            fetched_at=document.fetched_at,
            limit=context.limit,
            on_skip=skipped.append,
        )
        if skipped:
            _logger.warning(
                "ingest.charging.rows_unparsable",
                pipeline=self.name,
                count=len(skipped),
                first=skipped[0],
            )

    def _accepted(
        self,
        document: _SourceDocument,
        context: RunContext,
        validation: StreamingValidation,
        bronze: ParquetPartWriter,
        silver: ParquetPartWriter,
    ) -> Iterator[ChargingStationRecord]:
        """Validate the stream, mirror every record into bronze, and yield the survivors."""
        for record in self._records(document, context):
            violations = validation.check(record)
            if context.writes_enabled:
                bronze.add(_bronze_row(record, violations))
            if violations:
                continue
            if context.writes_enabled:
                silver.add(_silver_row(record))
            yield record

    # ------------------------------------------------------------------ persistence

    async def _upsert_batch(
        self,
        session: AsyncSession,
        records: list[ChargingStationRecord],
        run_id: UUID | None,
    ) -> int:
        """Upsert one batch of sites and replace the connectors of the ones that changed.

        Returns the number of station rows the database actually inserted or updated.
        """
        if run_id is None or not records:
            return 0

        ingested_at = utc_now()
        values = [_station_values(record, run_id, ingested_at) for record in records]
        statement = pg_insert(ChargingStation).values(values)
        excluded = statement.excluded
        changed = sa.or_(
            *(
                getattr(ChargingStation, column).is_distinct_from(getattr(excluded, column))
                for column in _STATION_CHANGE_COLUMNS
            ),
            # The point is compared as text: both sides are the same EWKB hex encoding of
            # the same SRID, so a byte difference is a coordinate difference and nothing
            # else. Comparing the geography columns directly would go through PostGIS's
            # ``=`` operator, whose semantics differ between geometry and geography.
            sa.cast(ChargingStation.location, sa.Text()).is_distinct_from(
                sa.cast(excluded.location, sa.Text())
            ),
        )
        upsert = statement.on_conflict_do_update(
            index_elements=[ChargingStation.external_id],
            set_={column: getattr(excluded, column) for column in _STATION_UPDATE_COLUMNS},
            where=changed,
        ).returning(ChargingStation.id, ChargingStation.external_id)

        touched = {
            str(external_id): station_id
            for station_id, external_id in (await session.execute(upsert)).all()
        }
        await self._replace_points(session, records, touched, run_id, ingested_at)
        return len(touched)

    async def _replace_points(
        self,
        session: AsyncSession,
        records: list[ChargingStationRecord],
        touched: dict[str, UUID],
        run_id: UUID,
        ingested_at: datetime,
    ) -> None:
        """Delete and re-insert the connectors of every station this batch changed.

        Connectors carry no natural key of their own — the register identifies them only by
        their position in the row — so replace is the only correct upsert. Stations the
        predicate left untouched keep their existing connectors, which is what turns a re-run
        of an unchanged edition into a genuine no-op.
        """
        if not touched:
            return
        await self._delete_points(session, list(touched.values()))

        by_external = {record.external_id: record for record in records}
        rows: list[dict[str, Any]] = []
        for external_id, station_id in touched.items():
            record = by_external[external_id]
            if not record.points:
                continue
            provenance = provenance_columns(
                record.provenance,
                run_id=run_id,
                ingested_at=ingested_at,
            )
            rows.extend(
                {
                    "station_id": station_id,
                    "ordinal": point.ordinal,
                    "connector_type": point.connector_type,
                    "current_type": point.current_type,
                    "power_kw": point.power_kw,
                    "public_key": point.public_key,
                    **provenance,
                }
                for point in record.points
            )
        for chunk in batched(rows, self._batch_size):
            await session.execute(sa.insert(ChargingPoint), chunk)

    @staticmethod
    async def _delete_points(session: AsyncSession, station_ids: list[UUID]) -> None:
        """Remove every connector of the given stations."""
        if not station_ids:
            return
        await session.execute(
            sa.delete(ChargingPoint).where(ChargingPoint.station_id.in_(station_ids))
        )


def _station_values(
    record: ChargingStationRecord,
    run_id: UUID,
    ingested_at: datetime,
) -> dict[str, Any]:
    """Render one record as the ``charging_stations`` column values."""
    return {
        "external_id": record.external_id,
        "operator": record.operator,
        "street": record.street,
        "house_number": record.house_number,
        "postal_code": record.postal_code,
        "city": record.city,
        "bundesland": record.bundesland,
        "country": record.country,
        "location": to_shape_point(record.coordinate.latitude, record.coordinate.longitude),
        "commissioned_on": record.commissioned_on,
        "charging_points_count": record.charging_points_count,
        "max_power_kw": record.max_power_kw,
        "total_power_kw": record.total_power_kw,
        "charging_category": record.charging_category,
        "is_fast_charger": record.is_fast_charger,
        "raw": record.raw,
        **provenance_columns(record.provenance, run_id=run_id, ingested_at=ingested_at),
    }


def _bronze_row(record: ChargingStationRecord, violations: list[Violation]) -> dict[str, Any]:
    """Render one parsed record for the bronze zone, accepted or not.

    ``quality_status`` and ``quality_rule`` are the columns that make this zone worth keeping:
    they are the only place a rejected station survives, and ``SELECT quality_rule, count(*)``
    over them is the question behind every data-quality conversation about this source.
    """
    rules = sorted({violation.rule for violation in violations})
    duplicate_only = rules == ["no_duplicate_key"]
    status = "accepted" if not rules else ("duplicate" if duplicate_only else "rejected")
    return {
        **_silver_row(record),
        "quality_status": status,
        "quality_rule": rules[0] if rules else None,
        "quality_rules": ",".join(rules) if rules else None,
        "quality_message": violations[0].message if violations else None,
    }


def _silver_row(record: ChargingStationRecord) -> dict[str, Any]:
    """Render one accepted record as the flat analytical row of the silver zone."""
    provenance = record.provenance
    return {
        "external_id": record.external_id,
        "operator": record.operator,
        "street": record.street,
        "house_number": record.house_number,
        "postal_code": record.postal_code,
        "city": record.city,
        "bundesland": record.bundesland.value if record.bundesland else None,
        "country": record.country,
        "latitude": record.coordinate.latitude,
        "longitude": record.coordinate.longitude,
        "commissioned_on": record.commissioned_on,
        "charging_points_count": record.charging_points_count,
        "max_power_kw": record.max_power_kw,
        "total_power_kw": record.total_power_kw,
        "charging_category": record.charging_category.value,
        "is_fast_charger": record.is_fast_charger,
        "connector_types": ",".join(sorted({point.connector_type.value for point in record.points}))
        or None,
        "source": provenance.source.value,
        "source_identifier": provenance.source_identifier,
        "source_url": provenance.source_url,
        "source_timestamp": provenance.source_timestamp,
        "data_origin": provenance.data_origin.value,
        "ingested_at": provenance.ingested_at,
    }
