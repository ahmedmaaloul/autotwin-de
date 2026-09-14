"""Autobahn traffic events — roadworks, closures and warnings — every 15 minutes.

**Source.** `https://verkehr.autobahn.de/o/autobahn/` — the Autobahn GmbH des Bundes public
API, keyless and anonymous, with its OpenAPI description maintained in the federal
`bundesAPI` organisation. Three endpoints per road: `services/roadworks`,
`services/closure`, `services/warning` (`docs/data/sources.md` §3).

## Licence caveat — this is why the request rate is what it is

**No licence statement is published with this API.** It is served publicly, without
authentication, and its specification lives in a federal repository, but nothing declares
terms of use. `DATA_LICENSES.md` records the position AutoTwin DE takes: *available for
development and demonstration, licence unconfirmed* — responses are cached locally, the
dataset is never redistributed, and **the request rate is deliberately kept low**.

That last point is a property of this DAG, not a footnote. Concretely:

* The API lists **108 roads**. This DAG polls **8** — the corridors the demo routes actually
  traverse. 8 roads x 3 endpoints = **24 requests per run**, 4 runs per hour ≈ **96
  requests/hour**. Polling all 108 roads at this cadence would be 1 296 requests/hour
  against an unmetered public service for data nobody in this project looks at.
* `max_active_runs=1` means a slow run delays the next one instead of stacking a second
  fan-out of requests on top of it.
* `catchup=False` means an Airflow restart never replays a backlog of 15-minute windows —
  traffic events are a *current-state* feed, so a backfill would re-request the same live
  state dozens of times and land identical rows.
* Retries back off exponentially and the run is capped well inside the 15-minute interval,
  so a struggling upstream is never retried into the next scheduled run.

If this were ever more than a portfolio deployment, the licensed route is Mobilithek's
DATEX II feeds; `TrafficProvider` exists so `MobilithekTrafficProvider` can replace the
adapter without touching this DAG.

## Pipeline

```
fetch_autobahn_events   cli ingest traffic --mode live --roads A1,A3,...
validate_ingestion      DAG-side gate on the run's counters and provider mode
run_dbt_models          dbt run --select stg_traffic_events+

(`cli` is `python -m autotwin_ingestion.cli`.)
```

**dbt *tests* are not run here.** They run once a day in `data_quality_report`. Executing
the full traffic test suite 96 times a day would spend more database time asserting than
loading, and a schema invariant that holds at 04:00 does not stop holding at 04:15 because
six roadworks were added. The models *are* rebuilt every run, because `mart_traffic_impact`
is what the dashboard reads.

## Parsing facts the adapter handles (measured, not assumed)

`coordinate` uses the key **`long`**, not `lon`. `isBlocked` is the string `"false"` in
3 327 of 3 327 observed items and is therefore dead — the real blocking signal is
`"CLOSED" in impact.symbols`. `startTimestamp` is absent exactly when
`display_type == "SHORT_TERM_ROADWORKS"`. Road ids come back with trailing whitespace
(`"A60 "`). None of that is this DAG's business — it is listed here so the 15-minute cadence
is understood as polling a feed whose shape is known, not one being probed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import common
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.models.param import Param

DAG_ID = "ingest_traffic"

#: The corridors the seeded demo routes use (BUILD_SPEC §3.2 `routes.slug`, e.g.
#: `frankfurt-stuttgart` runs A5 → A8/A81). Kept short on purpose — see the licence note.
DEFAULT_ROADS = "A1,A3,A5,A7,A8,A9,A61,A81"

#: Traffic is genuinely allowed to be empty: at 03:00 on a Sunday a road can have no active
#: roadworks at all. Zero accepted rows is therefore logged, never failed. The only hard
#: failure is a non-zero CLI exit, which the BashOperator already surfaces.
MAX_REJECT_RATIO = 0.25

DBT_SELECTOR = ("stg_traffic_events+",)

FETCH_LOG = common.scratch_file("fetch.jsonl")


@dag(
    dag_id=DAG_ID,
    description="Autobahn GmbH roadworks/closures/warnings → PostGIS → dbt (every 15 min)",
    schedule="*/15 * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=13),
    default_args=common.default_args(
        retries=2,
        retry_delay=timedelta(seconds=90),
        max_retry_delay=timedelta(minutes=5),
        execution_timeout=timedelta(minutes=6),
    ),
    tags=[common.TAG_INGESTION, "autobahn", common.TAG_OFFICIAL],
    doc_md=__doc__,
    params={
        "roads": Param(
            DEFAULT_ROADS,
            type="string",
            title="Roads to poll",
            description=(
                "Comma-separated Autobahn ids. Each road costs three HTTPS requests per run. "
                "Raising this raises the request rate against a source with no declared "
                "licence — read the DAG docs before widening it."
            ),
        ),
    },
)
def ingest_traffic() -> None:
    """Assemble the 15-minute traffic pipeline."""

    fetch = common.bash_step(
        task_id="fetch_autobahn_events",
        commands=[
            common.autotwin_command(
                "autotwin_ingestion.cli",
                "ingest",
                "traffic",
                "--mode",
                "live",
                "--roads",
                "{{ params.roads }}",
            )
        ],
        capture_to=FETCH_LOG,
        doc=(
            "Polls roadworks, closures and warnings for each configured road, maps them onto "
            "`TrafficEventType`/`TrafficSeverity` (BUILD_SPEC §2) and upserts "
            "`traffic_events` on `external_id`, keeping the GeoJSON `LineString` as "
            "`geometry` and the lat/long point as `location`."
        ),
    )

    @task(task_id="validate_ingestion", retries=0)
    def validate_ingestion(facts_path: str) -> dict[str, Any]:
        """Check the shape of the run, not the volume.

        An empty result is a legitimate answer from this source, so the gate only fires on
        a reported error or on a rejection ratio that indicates the payload shape moved —
        for example the coordinate key changing from `long`, which would reject every row
        while the HTTP call still returned 200.
        """
        facts = common.read_cli_facts(facts_path)
        mode = facts.get("provider_mode") or facts.get("mode")
        accepted = facts.get("rows_accepted")
        rejected = facts.get("rows_rejected")

        if facts.get("error_message"):
            raise AirflowFailException(
                f"Autobahn ingestion reported an error: {facts['error_message']}"
            )

        if isinstance(accepted, int | float) and isinstance(rejected, int | float):
            total = float(accepted) + float(rejected)
            if total > 0:
                ratio = float(rejected) / total
                if ratio > MAX_REJECT_RATIO:
                    raise AirflowFailException(
                        f"Autobahn rejection ratio {ratio:.1%} exceeds {MAX_REJECT_RATIO:.0%} "
                        f"({int(rejected)} of {int(total)}). The API returned data the parser "
                        "could not accept — check the `coordinate.long` key and the "
                        "`impact.symbols` blocking signal."
                    )
            if accepted == 0:
                common.LOGGER.info(
                    "No traffic events accepted this run. Legitimate for this source at "
                    "off-peak hours — not treated as a failure."
                )
        else:
            common.LOGGER.warning("No row counters in %s; the shape gate was skipped.", facts_path)

        summary = {"provider_mode": mode, "rows_accepted": accepted, "rows_rejected": rejected}
        common.LOGGER.info("Autobahn ingestion accepted: %s", summary)
        return summary

    dbt_models = common.bash_step(
        task_id="run_dbt_models",
        commands=[common.dbt_command("run", "--select", *DBT_SELECTOR)],
        execution_timeout=timedelta(minutes=6),
        doc=(
            "Rebuilds `stg_traffic_events` → `fct_traffic_events` → `mart_traffic_impact`, "
            "which is what the dashboard and the route analysis read. Tests for this "
            "lineage run daily in `data_quality_report`, not on every 15-minute poll."
        ),
    )

    validation = validate_ingestion(FETCH_LOG)
    fetch >> validation >> dbt_models


ingest_traffic()
