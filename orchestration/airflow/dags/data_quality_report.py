"""Daily data-quality report: source freshness plus the full dbt test suite.

This is the DAG that is allowed to be red. The ingestion DAGs answer "did this run work?";
this one answers the question a reviewer actually cares about — **"is what is in the
database still true?"** — and it answers it once a day whether or not anything ran.

**Why `0 6 * * *`.** Late enough that the previous night's traffic and weather runs have
landed, early enough that a stale-source failure is visible at the start of a working day
in CET/CEST.

## Pipeline

```
run_quality_report      python -m autotwin_ingestion.cli quality report
check_source_freshness  DAG-side gate: per-source age vs the thresholds in common.py
run_dbt_tests           dbt seed + dbt test   (every model, every test)
summarise               one consolidated log line, always runs
```

`run_dbt_tests` has `trigger_rule=ALL_DONE` on purpose: a stale source and a broken
invariant are independent failures, and finding out about both in one run beats
discovering the second one tomorrow. The run still fails — a downstream success does not
erase an upstream failure — but the report is complete.

## Freshness thresholds

Defined once in `common.FRESHNESS_THRESHOLDS`, roughly three publication intervals each:

| source | threshold | rationale |
|---|---|---|
| `bundesnetzagentur` | 40 days | one file per month, previous edition deleted |
| `dwd` | 3 hours | hourly DAG against a continuously refreshed feed |
| `autobahn` | 90 minutes | 15-minute polling |

Sources without a threshold — `osm`, `osrm`, `simulator`, `derived`, `mobilithek`
(BUILD_SPEC §2) — are reported but never fail the run: they have no publication schedule to
be late against.

## What this DAG reads, and what happens if it cannot

`quality report` is invoked with `--json-logs`, and the gate collects every JSON object in
its output that names a `source` and carries either an `age_minutes` or a timestamp
(`last_run_at` / `last_success_at` / `finished_at`). Those field names are not invented
here — they are the freshness block of `DashboardSummary` in BUILD_SPEC §7.1, produced by
the same `data_ingestion_runs` rows.

If **no** source record can be parsed, the gate **fails**. That is deliberate and it is the
opposite of the fail-open choice in the charging DAG: there, an unreadable fingerprint costs
a redundant load; here, an unreadable report *is* the failure being reported. A data-quality
DAG that goes green because it could not read its own input would be the single most
dishonest thing in this repository.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import common
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.operators.python import get_current_context
from airflow.utils.state import State
from airflow.utils.trigger_rule import TriggerRule

DAG_ID = "data_quality_report"

QUALITY_LOG = common.scratch_file("quality_report.jsonl")


@dag(
    dag_id=DAG_ID,
    description="Daily source-freshness gate and full dbt test suite",
    schedule="0 6 * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=1),
    default_args=common.default_args(
        retries=1,
        retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=20),
    ),
    tags=[common.TAG_QUALITY, common.TAG_DBT, "observability"],
    doc_md=__doc__,
)
def data_quality_report() -> None:
    """Assemble the daily quality report."""

    report = common.bash_step(
        task_id="run_quality_report",
        commands=[
            common.autotwin_command("autotwin_ingestion.cli", "quality", "report"),
        ],
        capture_to=QUALITY_LOG,
        doc=(
            "Prints the latest `data_ingestion_runs` row per source with its stored "
            "`quality_report` (BUILD_SPEC §6). Same data `GET /api/v1/data/quality` serves."
        ),
    )

    @task(task_id="check_source_freshness", retries=0)
    def check_source_freshness(report_path: str) -> dict[str, Any]:
        """Fail the run when a scheduled source has gone stale.

        Reports on every source it can see; only sources with a declared threshold can
        fail the run.
        """
        # Wall clock, deliberately not `logical_date`: for a daily schedule the logical
        # date is the *start* of the data interval, i.e. 24 h before the run actually
        # executes, and using it would inflate every age by a full interval.
        now = datetime.now(tz=UTC)
        records = common.collect_source_records(report_path)

        if not records:
            raise AirflowFailException(
                f"`quality report` produced no parsable per-source records in {report_path}. "
                "Expected JSON objects carrying `source` plus `age_minutes` or "
                "`last_run_at` (BUILD_SPEC §7.1). Failing closed: an unreadable quality "
                "report is itself a quality failure."
            )

        stale: list[str] = []
        unknown_age: list[str] = []
        lines: list[str] = []

        for record in sorted(records, key=lambda item: str(item.get("source"))):
            source = str(record.get("source"))
            threshold = common.FRESHNESS_THRESHOLDS.get(source)
            age = common.age_of(record, now=now)

            if age is None:
                unknown_age.append(source)
                lines.append(f"  {source:<20} age=unknown  status={record.get('status')}")
                continue

            marker = "ok"
            if threshold is not None and age > threshold:
                marker = "STALE"
                stale.append(
                    f"{source}: {age.total_seconds() / 3600:.1f} h old, "
                    f"threshold {threshold.total_seconds() / 3600:.1f} h"
                )
            elif threshold is None:
                marker = "unscheduled"

            lines.append(
                f"  {source:<20} age={age.total_seconds() / 3600:8.2f} h  "
                f"status={record.get('status')}  mode={record.get('mode')}  [{marker}]"
            )

        common.LOGGER.info("Source freshness at %s:\n%s", now.isoformat(), "\n".join(lines))

        for source in unknown_age:
            if source in common.FRESHNESS_THRESHOLDS:
                stale.append(f"{source}: no usable timestamp in the quality report")

        if stale:
            raise AirflowFailException(
                "Stale sources:\n  - " + "\n  - ".join(stale) + "\n"
                "Check the corresponding ingestion DAG; the API is still serving these rows "
                "and marking them with their true age."
            )

        return {
            "checked": len(records),
            "sources": [str(record.get("source")) for record in records],
            "unknown_age": unknown_age,
        }

    dbt_tests = common.bash_step(
        task_id="run_dbt_tests",
        commands=[
            common.dbt_deps_if_needed(),
            common.dbt_seed(),
            common.dbt_command("test"),
        ],
        trigger_rule=TriggerRule.ALL_DONE,
        execution_timeout=timedelta(minutes=30),
        doc=(
            "The whole suite: `unique` / `not_null` on every key, `accepted_values` on every "
            "enum column against BUILD_SPEC §2, `relationships` on every foreign key, and the "
            "singular tests in `dbt/tests/` (SOC in 0-100, coordinates inside the Germany "
            "bounding box, no future telemetry timestamps, no non-positive charging power). "
            "Runs even when the freshness gate failed — two independent questions, one report."
        ),
    )

    @task(task_id="summarise", retries=0, trigger_rule=TriggerRule.ALL_DONE)
    def summarise() -> None:
        """One consolidated line per daily report, whatever happened upstream.

        Reads the sibling task states from the metadata database rather than XCom, because
        the interesting case is precisely the one where an upstream task failed and pushed
        no XCom at all.
        """
        context = get_current_context()
        dag_run = context["dag_run"]
        states = {
            instance.task_id: instance.state
            for instance in dag_run.get_task_instances()
            if instance.task_id != "summarise"
        }
        failed = sorted(task_id for task_id, state in states.items() if state == State.FAILED)
        common.LOGGER.info(
            "data_quality_report %s: %s (states=%s)",
            context["ds"],
            "FAILED — " + ", ".join(failed) if failed else "all checks passed",
            states,
        )

    freshness = check_source_freshness(QUALITY_LOG)

    report >> freshness >> dbt_tests >> summarise()


data_quality_report()
