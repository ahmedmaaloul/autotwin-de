"""``/api/v1/data`` — where every number on this platform came from, and how good it is.

This is the endpoint that makes the rest of the API checkable. It reports the failed run as
loudly as the successful one, the partial run as *degraded* rather than *healthy*, and it
restates each publisher's licence and attribution beside their data so that anyone using a
figure can see the terms without leaving the page (``DATA_LICENSES.md``, BUILD_SPEC §6).

``/data/quality`` describes the sources this deployment has actually ingested, plus the
simulator. A source the enum knows but nothing has ever fetched is deliberately absent: a card
reading "Mobilithek — failed" would be a false alarm about a pipeline that was never run.
``/data/sources`` is the complementary view — the whole catalogue, including what has not been
ingested here — so nothing is hidden, only sorted by which question is being asked.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Query

from autotwin_api.deps import DbSession, Pagination
from autotwin_api.metrics import INGESTION_ROWS_PROCESSED, INGESTION_ROWS_REJECTED
from autotwin_api.pagination import paginate
from autotwin_api.schemas.data import (
    SOURCE_TERMS,
    DataSourceInfo,
    IngestionRunOut,
    QualityReportOut,
    SourceQuality,
    acceptance_rate_percent,
    age_minutes_of,
    derive_status,
)
from autotwin_contracts import (
    DataOrigin,
    IngestionOutcome,
    IngestionStatus,
    Page,
    ProviderMode,
    SourceSystem,
    utc_now,
)
from autotwin_core.db.models import DataIngestionRun

__all__ = ["router"]

router = APIRouter()

_MAX_VIOLATIONS: Final[int] = 50
"""Violations returned per run.

A pipeline that rejected forty thousand malformed coordinates recorded forty thousand
violations. Fifty is enough to see the pattern; the rest are counted, not shipped.
"""

_ROW_COUNT_SQL: Final[str] = """
SELECT 'bundesnetzagentur' AS source, count(*) AS rows FROM charging_stations
UNION ALL SELECT 'dwd', count(*) FROM weather_observations
UNION ALL SELECT 'autobahn', count(*) FROM traffic_events
UNION ALL SELECT 'osrm', count(*) FROM routes
UNION ALL SELECT 'simulator', count(*) FROM telemetry
"""
"""How many rows each source currently accounts for.

Hard-coded rather than derived, because the mapping from a source to the table it populates is
domain knowledge, not schema metadata: ``routes`` comes from OSRM, ``telemetry`` from the
simulator, and no amount of introspection would work that out.
"""

_INGESTION_TOTALS_SQL: Final[str] = """
SELECT coalesce(sum(rows_accepted), 0) AS accepted,
       coalesce(sum(rows_rejected), 0) AS rejected
  FROM data_ingestion_runs
"""
"""Rows every pipeline execution has ever accepted and rejected into this database.

Read from the run history rather than accumulated in this process: the pipelines run as
separate processes (and as Airflow tasks), so an API process has no way to observe them
directly, and a counter that reset whenever the API restarted would be worse than useless on a
dashboard.
"""

_LATEST_RUN_SQL: Final[str] = """
SELECT DISTINCT ON (source)
       source, pipeline, started_at, status, provider_mode, source_url, error_message,
       rows_received, rows_accepted, rows_rejected, rows_duplicate
  FROM data_ingestion_runs
 ORDER BY source, started_at DESC
"""


def _simulator_quality(telemetry_rows: int) -> SourceQuality:
    """The simulator's row, which has no ingestion run because it ingests nothing.

    Reported as ``simulation`` rather than ``healthy`` so that a green tick on this page can
    never be read as "a German authority confirmed this" (BUILD_SPEC §0.2). The row counts are
    the telemetry it has produced: accepted by definition, since the simulator is the
    authority on its own output.
    """
    terms = SOURCE_TERMS[SourceSystem.simulator]
    return SourceQuality(
        source=SourceSystem.simulator,
        status=IngestionStatus.simulation,
        data_origin=DataOrigin.simulated,
        last_run_at=None,
        age_minutes=None,
        rows_received=telemetry_rows,
        rows_accepted=telemetry_rows,
        rows_rejected=0,
        rows_duplicate=0,
        acceptance_rate=100.0 if telemetry_rows else 0.0,
        provider_mode=None,
        licence=terms.licence,
        attribution=terms.attribution,
        source_url=terms.url,
        pipeline="autotwin_simulator",
        error_message=None,
    )


@router.get(
    "/quality",
    response_model=list[SourceQuality],
    summary="Per-source data quality",
    description=(
        "One card per source: the state of its most recent run, how many rows survived "
        "validation, and the licence and attribution its data carries.\n\n"
        "**Status is not a traffic light on 'did it answer'.** A run that fell back to cached "
        "or fixture data reports `degraded`, a run that rejected rows reports `degraded`, a "
        "run that failed reports `failed`, and a source older than its publisher's own refresh "
        "cadence reports `delayed`. The simulator reports `simulation`, never `healthy`, "
        "because it is not an external source (BUILD_SPEC §6)."
    ),
)
async def data_quality(session: DbSession) -> list[SourceQuality]:
    """Read the newest run per source, classify it, and attach the publisher's terms."""
    now = utc_now()
    runs = (await session.execute(sa.text(_LATEST_RUN_SQL))).mappings().all()
    counts = {
        str(row["source"]): int(row["rows"])
        for row in (await session.execute(sa.text(_ROW_COUNT_SQL))).mappings().all()
    }

    qualities: list[SourceQuality] = []
    for run in runs:
        try:
            source = SourceSystem(run["source"])
        except ValueError:
            continue
        terms = SOURCE_TERMS.get(source)
        age = age_minutes_of(run["started_at"], now=now)
        outcome = IngestionOutcome(run["status"])
        mode = ProviderMode(run["provider_mode"])
        qualities.append(
            SourceQuality(
                source=source,
                status=derive_status(
                    source=source,
                    outcome=outcome,
                    provider_mode=mode,
                    age_minutes=age,
                ),
                data_origin=terms.data_origin if terms else DataOrigin.official,
                last_run_at=run["started_at"],
                age_minutes=age,
                rows_received=int(run["rows_received"]),
                rows_accepted=int(run["rows_accepted"]),
                rows_rejected=int(run["rows_rejected"]),
                rows_duplicate=int(run["rows_duplicate"]),
                acceptance_rate=acceptance_rate_percent(
                    rows_received=int(run["rows_received"]),
                    rows_accepted=int(run["rows_accepted"]),
                ),
                provider_mode=mode,
                licence=terms.licence if terms else None,
                attribution=terms.attribution if terms else None,
                source_url=run["source_url"] or (terms.url if terms else None),
                pipeline=run["pipeline"],
                error_message=run["error_message"],
            )
        )

    totals = (await session.execute(sa.text(_INGESTION_TOTALS_SQL))).mappings().one()
    INGESTION_ROWS_PROCESSED.set(int(totals["accepted"]))
    INGESTION_ROWS_REJECTED.set(int(totals["rejected"]))

    qualities.append(_simulator_quality(counts.get(SourceSystem.simulator.value, 0)))
    # Worst first: the point of this page is the source that needs attention, and a reader
    # should not have to scan four healthy cards to find the failed one.
    severity = {
        IngestionStatus.failed: 0,
        IngestionStatus.degraded: 1,
        IngestionStatus.delayed: 2,
        IngestionStatus.healthy: 3,
        IngestionStatus.simulation: 4,
    }
    qualities.sort(key=lambda item: (severity[item.status], item.source.value))
    return qualities


def _quality_report(raw: Any) -> QualityReportOut | None:
    """Trim a stored quality report to what is worth sending.

    Returns ``None`` for a run that stored nothing, rather than an empty report: "no violations
    were recorded" and "this pipeline does not record violations" are different facts.
    """
    if not isinstance(raw, dict):
        return None
    violations = raw.get("violations")
    listed = violations if isinstance(violations, list) else []
    stats = raw.get("rule_stats")
    return QualityReportOut(
        violations=[item for item in listed[:_MAX_VIOLATIONS] if isinstance(item, dict)],
        rule_stats={
            str(key): int(value)
            for key, value in (stats if isinstance(stats, dict) else {}).items()
            if isinstance(value, int)
        },
        violations_truncated=max(0, len(listed) - _MAX_VIOLATIONS),
    )


@router.get(
    "/ingestions",
    response_model=Page[IngestionRunOut],
    summary="Ingestion run history",
    description=(
        "Every pipeline execution, newest first, including the ones that failed — a run row is "
        "written whether or not the source answered (BUILD_SPEC §15).\n\n"
        f"Each run carries its validation report, capped at {_MAX_VIOLATIONS} individual "
        "violations with `violations_truncated` counting the rest."
    ),
)
async def ingestion_runs(
    session: DbSession,
    page: Pagination,
    source: Annotated[
        SourceSystem | None,
        Query(description="Only runs of this source.", examples=["dwd"]),
    ] = None,
) -> Page[IngestionRunOut]:
    """Page the run history."""
    statement = sa.select(DataIngestionRun).order_by(DataIngestionRun.started_at.desc())
    if source is not None:
        statement = statement.where(DataIngestionRun.source == source)
    return await paginate(
        session,
        statement,
        page,
        lambda row: IngestionRunOut(
            id=row.id,
            source=row.source,
            pipeline=row.pipeline,
            started_at=row.started_at,
            finished_at=row.finished_at,
            status=row.status,
            provider_mode=row.provider_mode,
            rows_received=row.rows_received,
            rows_accepted=row.rows_accepted,
            rows_rejected=row.rows_rejected,
            rows_duplicate=row.rows_duplicate,
            bytes_downloaded=row.bytes_downloaded,
            source_url=row.source_url,
            error_message=row.error_message,
            quality_report=_quality_report(row.quality_report),
        ),
    )


@router.get(
    "/sources",
    response_model=list[DataSourceInfo],
    summary="Data source catalogue",
    description=(
        "Every source AutoTwin draws on, with its licence, the attribution line that must "
        "accompany anything derived from it, when it last answered here and how many rows this "
        "deployment holds from it.\n\n"
        "The Autobahn GmbH entry reports `licence: null` on purpose: the publisher declares no "
        "terms, and stating a plausible one would be the most damaging kind of convenience "
        "(`DATA_LICENSES.md`)."
    ),
)
async def data_sources(session: DbSession) -> list[DataSourceInfo]:
    """List the catalogue, enriched with this deployment's own run history and row counts."""
    latest = {
        str(row["source"]): row
        for row in (await session.execute(sa.text(_LATEST_RUN_SQL))).mappings().all()
    }
    counts = {
        str(row["source"]): int(row["rows"])
        for row in (await session.execute(sa.text(_ROW_COUNT_SQL))).mappings().all()
    }
    return [
        DataSourceInfo.of(
            source,
            last_run_at=latest[source.value]["started_at"] if source.value in latest else None,
            last_status=(
                IngestionOutcome(latest[source.value]["status"]) if source.value in latest else None
            ),
            rows_in_database=counts.get(source.value),
        )
        for source in SOURCE_TERMS
    ]
