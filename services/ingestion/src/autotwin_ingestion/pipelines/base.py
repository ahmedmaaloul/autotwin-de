"""The ingestion template: one run, one ``data_ingestion_runs`` row, no exceptions.

Every pipeline in this package is the same five steps in the same order, and the bookkeeping
around them is the point of the abstraction rather than an afterthought:

1. **Open the run row before fetching.** A run that is killed during a 55 MB download still
   left evidence that it started, and ``GET /api/v1/data/quality`` can say "the
   Bundesnetzagentur pipeline started at 09:14 and never finished" instead of showing nothing
   at all.
2. **Call the provider and keep its verdict.** ``ProviderResult.mode`` is copied onto the run
   verbatim. A run that was served from a bundled fixture says ``fixture``, always — that is
   the database half of the honesty rule (BUILD_SPEC §0.2).
3. **Validate through** :class:`~autotwin_core.quality.RecordValidator`, rejecting bad rows
   rather than writing them (§6).
4. **Upsert on the natural key**, stamping the provenance block with this run's id.
5. **Close the run row** with the counters, the quality report, the bytes and the SHA-256 —
   and on *any* exception close it as ``failed`` with the message, then re-raise. A failed
   ingestion that leaves no row is a failed ingestion nobody can see.

Two transaction boundaries, deliberately separate: the run row is opened and closed in its own
short transaction, and the data write gets another. If the write rolls back, the run row must
survive to say so — sharing one transaction would roll the evidence back along with the data.

``--dry-run`` runs steps 2 and 3 and stops. Nothing is written, not even the run row: a dry run
is a question about the source, not an event in the ingestion history.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Final
from uuid import UUID, uuid4

import sqlalchemy as sa

from autotwin_contracts import (
    IngestionOutcome,
    ProvenanceInfo,
    ProviderMode,
    SourceSystem,
    utc_now,
)
from autotwin_core.config import DataMode, Settings, get_settings
from autotwin_core.db import DataIngestionRun, session_scope
from autotwin_core.logging import get_logger
from autotwin_core.quality import (
    DUPLICATE_RULE_NAME,
    QualityReport,
    RecordValidator,
    Violation,
    reset_rules,
)
from autotwin_ingestion.lake import DataLake, default_lake

__all__ = [
    "DEFAULT_UPSERT_BATCH",
    "IngestionPipeline",
    "IngestionResult",
    "PipelineOutcome",
    "RunContext",
    "StreamingValidation",
    "batched",
    "provenance_columns",
]

_logger = get_logger(__name__)

DEFAULT_UPSERT_BATCH: Final[int] = 2_000
"""Rows per ``INSERT ... ON CONFLICT`` statement.

Two thousand keeps a single statement's parameter list well inside PostgreSQL's 65 535 bound
(the widest table here binds 27 parameters per row) while still amortising the round trip over
enough rows that 117 000 of them take seconds rather than minutes.
"""


@dataclass(frozen=True, slots=True)
class RunContext:
    """Everything a pipeline's body needs about the run it is executing.

    Passed rather than stored on the pipeline so that the template method owns all mutable
    state: a pipeline instance is reusable and two concurrent runs cannot read each other's
    run id.
    """

    run_id: UUID | None
    """``data_ingestion_runs.id``, or ``None`` on a dry run where no row was opened."""

    started_at: datetime
    """When the run began (UTC)."""

    dry_run: bool
    """Validate and report, write nothing."""

    limit: int | None
    """Stop after this many source records; ``None`` means the whole source."""

    settings: Settings
    """Resolved configuration for this run."""

    lake: DataLake
    """The data zone this run writes its raw document and Parquet tables into."""

    @property
    def writes_enabled(self) -> bool:
        """Whether this run may touch the database and the lake."""
        return not self.dry_run


@dataclass(slots=True)
class PipelineOutcome:
    """What a pipeline body reports back to the template.

    Mutable, because a body fills it in as it learns: the provider mode is known after the
    fetch, the counters after validation, the SHA-256 only once the source file is on disk.
    """

    mode: ProviderMode = ProviderMode.fixture
    """How the provider answered. Defaults to the most degraded value, never to ``live``."""

    report: QualityReport = field(default_factory=QualityReport)
    """Counters and violations of this run (BUILD_SPEC §6)."""

    rows_written: int = 0
    """Rows the database actually inserted or updated — never larger than ``rows_accepted``."""

    rows_closed: int = 0
    """Rows marked as ended because they vanished from the source (traffic only)."""

    source_url: str | None = None
    """Document or endpoint behind the answer."""

    bytes_downloaded: int | None = None
    """Size of the source document, when the pipeline handled one."""

    source_file_sha256: str | None = None
    """Digest of that document."""

    warnings: list[str] = field(default_factory=list)
    """Provider and pipeline warnings, surfaced in the CLI summary."""

    details: dict[str, Any] = field(default_factory=dict)
    """Pipeline-specific facts for the human summary (stations contacted, roads polled, …)."""


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """The outcome of one pipeline run, as the CLI prints it and a caller inspects it."""

    pipeline: str
    """Pipeline name, matching ``data_ingestion_runs.pipeline``."""

    source: SourceSystem
    """Source system the run read from."""

    run_id: UUID | None
    """The persisted run row, or ``None`` for a dry run."""

    status: IngestionOutcome
    """``success``, ``partial`` or ``failed`` — the value stored on the run row."""

    mode: ProviderMode
    """How the provider answered: ``live``, ``cache`` or ``fixture``."""

    report: QualityReport
    """The quality report persisted with the run."""

    rows_written: int
    """Rows inserted or updated in PostgreSQL."""

    rows_closed: int
    """Rows marked ended because they disappeared from the source."""

    duration_s: float
    """Wall-clock duration of the run."""

    dry_run: bool
    """Whether the run was a dry run."""

    source_url: str | None = None
    """Document or endpoint behind the answer."""

    bytes_downloaded: int | None = None
    """Size of the source document, when there was one."""

    source_file_sha256: str | None = None
    """Digest of that document."""

    warnings: tuple[str, ...] = ()
    """Non-fatal problems worth showing next to the numbers, de-duplicated in encounter order.

    The DWD attribution and "serving a bundled fixture" notices arrive once per layer a
    provider consulted; repeating them three times in a summary hides the warnings that only
    appeared once."""

    details: dict[str, Any] = field(default_factory=dict)
    """Pipeline-specific facts for the human summary."""

    error_message: str | None = None
    """Why the run failed, when it did."""

    @property
    def succeeded(self) -> bool:
        """Whether the run is one the CLI should exit ``0`` on.

        ``partial`` counts as success: German open data routinely ships a handful of rows with
        a broken coordinate, and failing the nightly job over twelve rejected stations out of
        117 000 would train everyone to ignore the exit code.
        """
        return self.status is not IngestionOutcome.failed

    def summary_lines(self) -> list[str]:
        """Human-readable summary, one fact per line — what a command prints when it ends."""
        report = self.report
        lines = [
            f"pipeline        {self.pipeline}",
            f"source          {self.source.value}",
            f"mode            {self.mode.value}"
            + ("  (degraded — not the live source)" if self.mode is not ProviderMode.live else ""),
            f"status          {self.status.value}" + ("  (dry run)" if self.dry_run else ""),
            f"rows received   {report.rows_received}",
            f"rows accepted   {report.rows_accepted}",
            f"rows rejected   {report.rows_rejected}",
            f"rows duplicate  {report.rows_duplicate}",
            f"rows written    {self.rows_written}"
            + ("  (nothing written — dry run)" if self.dry_run else ""),
        ]
        if self.rows_closed:
            lines.append(f"rows ended      {self.rows_closed}  (vanished from the source)")
        for key, value in self.details.items():
            lines.append(f"{key:<15} {value}")
        if self.bytes_downloaded is not None:
            lines.append(f"source bytes    {self.bytes_downloaded:,}")
        if self.source_file_sha256:
            lines.append(f"source sha256   {self.source_file_sha256[:16]}…")
        if self.source_url:
            lines.append(f"source url      {self.source_url}")
        lines.append(f"duration        {self.duration_s:.1f}s")
        if self.run_id is not None:
            lines.append(f"ingestion run   {self.run_id}")
        if report.rule_stats:
            worst = sorted(report.rule_stats.items(), key=lambda item: -item[1])[:5]
            lines.append("rules fired     " + ", ".join(f"{rule} x{n}" for rule, n in worst))
        for warning in self.warnings[:5]:
            lines.append(f"warning         {warning}")
        if self.error_message:
            lines.append(f"error           {self.error_message}")
        return lines


class IngestionPipeline(ABC):
    """Template for every ingestion pipeline (BUILD_SPEC §6, §15).

    Subclasses implement :meth:`_execute` and nothing else about bookkeeping: opening,
    closing and failing the ``data_ingestion_runs`` row, timing, logging and the result
    envelope are all handled here, identically, for every source.
    """

    name: ClassVar[str]
    """Pipeline name written to ``data_ingestion_runs.pipeline``."""

    source: ClassVar[SourceSystem]
    """Source system written to ``data_ingestion_runs.source``."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        data_mode: DataMode | None = None,
        dry_run: bool = False,
        limit: int | None = None,
        lake: DataLake | None = None,
    ) -> None:
        """Configure the run.

        Args:
            settings: Configuration source; defaults to the process-wide settings.
            data_mode: Override the configured provider policy for this run — what the CLI's
                ``--mode`` flag sets.
            dry_run: Validate and report without writing anything.
            limit: Stop after this many source records.
            lake: Data zone to write into; defaults to ``AUTOTWIN_DATA_DIR``.

        Raises:
            ValueError: ``limit`` is not positive.
        """
        if limit is not None and limit < 1:
            msg = f"limit must be positive, got {limit!r}"
            raise ValueError(msg)
        self._settings = settings or get_settings()
        self._data_mode = data_mode or self._settings.data_mode
        self._dry_run = dry_run
        self._limit = limit
        self._lake = lake or default_lake()

    @property
    def settings(self) -> Settings:
        """Resolved configuration."""
        return self._settings

    @property
    def data_mode(self) -> DataMode:
        """Provider policy this run uses."""
        return self._data_mode

    async def run(self) -> IngestionResult:
        """Execute the pipeline, keeping ``data_ingestion_runs`` truthful either way.

        Returns:
            The run's outcome, including the quality report the run row now carries.

        Raises:
            Exception: whatever the body raised, re-raised after the run row was closed as
                ``failed``. Callers that want an exit code rather than a traceback catch
                :class:`~autotwin_core.errors.AutoTwinError` around this.
        """
        started_at = utc_now()
        started_perf = time.perf_counter()
        run_id = None if self._dry_run else await self._open_run(started_at)
        context = RunContext(
            run_id=run_id,
            started_at=started_at,
            dry_run=self._dry_run,
            limit=self._limit,
            settings=self._settings,
            lake=self._lake,
        )
        _logger.info(
            "ingestion.started",
            pipeline=self.name,
            source=self.source.value,
            data_mode=self._data_mode.value,
            dry_run=self._dry_run,
            run_id=str(run_id) if run_id else None,
        )
        try:
            outcome = await self._execute(context)
        except Exception as error:
            await self._close_failed(run_id, error)
            _logger.error(
                "ingestion.failed",
                pipeline=self.name,
                source=self.source.value,
                error=type(error).__name__,
                detail=str(error),
            )
            raise
        status = outcome.report.outcome
        await self._close_run(run_id, outcome, status)
        duration_s = time.perf_counter() - started_perf
        _logger.info(
            "ingestion.finished",
            pipeline=self.name,
            source=self.source.value,
            status=status.value,
            mode=outcome.mode.value,
            rows_received=outcome.report.rows_received,
            rows_accepted=outcome.report.rows_accepted,
            rows_rejected=outcome.report.rows_rejected,
            rows_written=outcome.rows_written,
            duration_ms=round(duration_s * 1000.0, 1),
        )
        return IngestionResult(
            pipeline=self.name,
            source=self.source,
            run_id=run_id,
            status=status,
            mode=outcome.mode,
            report=outcome.report,
            rows_written=outcome.rows_written,
            rows_closed=outcome.rows_closed,
            duration_s=duration_s,
            dry_run=self._dry_run,
            source_url=outcome.source_url,
            bytes_downloaded=outcome.bytes_downloaded,
            source_file_sha256=outcome.source_file_sha256,
            warnings=tuple(dict.fromkeys(outcome.warnings)),
            details=dict(outcome.details),
            # A body that rejected everything it received did not raise — it ran, and every
            # row failed. The first warning is why, and without it the summary would report a
            # failed run with no reason attached.
            error_message=(
                outcome.warnings[0]
                if status is IngestionOutcome.failed and outcome.warnings
                else None
            ),
        )

    @abstractmethod
    async def _execute(self, context: RunContext) -> PipelineOutcome:
        """Fetch, validate and persist. Implemented by each source's pipeline."""

    # ------------------------------------------------------------------ run bookkeeping

    async def _open_run(self, started_at: datetime) -> UUID:
        """Insert the in-flight run row and return its id.

        The row opens as ``failed`` with ``finished_at`` null, because ``IngestionOutcome`` has
        no in-flight member and the direction of the error matters: a process killed mid-run
        leaves a row that under-claims rather than one that claims a success it never had.
        ``provider_mode`` opens at ``fixture`` for the same reason — nothing may claim to have
        reached a live German source until a provider says it did.
        """
        run_id = uuid4()
        async with session_scope() as session:
            session.add(
                DataIngestionRun(
                    id=run_id,
                    source=self.source,
                    pipeline=self.name,
                    started_at=started_at,
                    finished_at=None,
                    status=IngestionOutcome.failed,
                    provider_mode=ProviderMode.fixture,
                )
            )
        return run_id

    async def _close_run(
        self,
        run_id: UUID | None,
        outcome: PipelineOutcome,
        status: IngestionOutcome,
    ) -> None:
        """Write the counters, the quality report and the provenance onto the run row."""
        if run_id is None:
            return
        await self._update_run(
            run_id,
            finished_at=utc_now(),
            status=status,
            provider_mode=outcome.mode,
            rows_received=outcome.report.rows_received,
            rows_accepted=outcome.report.rows_accepted,
            rows_rejected=outcome.report.rows_rejected,
            rows_duplicate=outcome.report.rows_duplicate,
            bytes_downloaded=outcome.bytes_downloaded,
            source_url=outcome.source_url,
            source_file_sha256=outcome.source_file_sha256,
            quality_report=outcome.report.as_dict(),
            error_message=None,
        )

    async def _close_failed(self, run_id: UUID | None, error: BaseException) -> None:
        """Close the run row as failed, recording the exception type and message.

        Best-effort by design: if the database is what failed, re-raising a second exception
        from the error path would replace the real cause with a connection error.
        """
        if run_id is None:
            return
        message = f"{type(error).__name__}: {error}"
        try:
            await self._update_run(
                run_id,
                finished_at=utc_now(),
                status=IngestionOutcome.failed,
                error_message=message[:2000],
            )
        except Exception as secondary:  # pragma: no cover - only on a database outage
            _logger.error(
                "ingestion.run_row_not_closed",
                pipeline=self.name,
                run_id=str(run_id),
                error=type(secondary).__name__,
                detail=str(secondary),
            )

    async def _update_run(self, run_id: UUID, **values: Any) -> None:
        """Apply an UPDATE to the run row in its own transaction."""
        async with session_scope() as session:
            await session.execute(
                sa.update(DataIngestionRun).where(DataIngestionRun.id == run_id).values(**values)
            )


class StreamingValidation:
    """Row-by-row validation with the same accounting as :meth:`RecordValidator.validate`.

    The batch API materialises every accepted record before returning, which is exactly what
    the Ladesäulenregister pipeline must not do — 117 000 Pydantic models is hundreds of
    megabytes. This wrapper drives the same rules through
    :meth:`~autotwin_core.quality.RecordValidator.validate_one`, which leaves stateful rules
    (duplicate detection above all) intact across the whole stream rather than resetting them
    per chunk, and keeps the four counters adding up exactly as the batch validator does:
    ``received == accepted + rejected + duplicate``.
    """

    __slots__ = ("_index", "_max_violations", "_report", "_validator")

    def __init__(self, validator: RecordValidator, *, max_violations: int = 5_000) -> None:
        """Start a fresh stream, resetting the validator's stateful rules."""
        self._validator = validator
        self._report = QualityReport()
        self._index = 0
        self._max_violations = max_violations
        reset_rules(validator.rules)

    @property
    def report(self) -> QualityReport:
        """The report accumulated so far."""
        return self._report

    def check(self, record: object) -> list[Violation]:
        """Validate one record, update the counters, and return the violations it produced.

        An empty list means the record is accepted. The caller decides what to do with a
        rejected record — the Ladesäulenregister pipeline writes it to the bronze zone so the
        rejected population stays analysable even though it never reaches PostgreSQL.
        """
        violations = self._validator.validate_one(record, row_index=self._index)
        self._index += 1
        self._report.rows_received += 1
        if not violations:
            self._report.rows_accepted += 1
        elif {violation.rule for violation in violations} == {DUPLICATE_RULE_NAME}:
            self._report.rows_duplicate += 1
        else:
            self._report.rows_rejected += 1
        for violation in violations:
            if len(self._report.violations) < self._max_violations:
                self._report.record_violation(violation)
            else:
                stats = self._report.rule_stats
                stats[violation.rule] = stats.get(violation.rule, 0) + 1
        return violations


def batched[ItemT](items: Iterable[ItemT], size: int) -> Iterator[list[ItemT]]:
    """Yield consecutive lists of at most ``size`` items.

    ``itertools.batched`` yields tuples; the upsert helpers want lists they can extend, and
    spelling it here keeps the batch size decision next to the reason for it.

    Raises:
        ValueError: ``size`` is not positive.
    """
    if size < 1:
        msg = f"batch size must be positive, got {size!r}"
        raise ValueError(msg)
    batch: list[ItemT] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def provenance_columns(
    provenance: ProvenanceInfo,
    *,
    run_id: UUID | None,
    ingested_at: datetime | None = None,
) -> dict[str, Any]:
    """Render a record's provenance block as the seven columns of BUILD_SPEC §3.1.

    One function so that no pipeline can forget ``ingestion_run_id`` — the column that turns
    "this station exists" into "this station came from the 2026-09-01 edition, read by run
    ``…``, which rejected twelve rows for broken coordinates".
    """
    return {
        "source": provenance.source,
        "source_identifier": provenance.source_identifier,
        "source_url": provenance.source_url,
        "source_timestamp": provenance.source_timestamp,
        "data_origin": provenance.data_origin,
        "ingestion_run_id": run_id,
        "ingested_at": ingested_at or provenance.ingested_at,
    }
