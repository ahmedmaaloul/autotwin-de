"""Monthly ingestion of the Bundesnetzagentur Ladesäulenregister into PostGIS.

**Source.** `Ladesaeulenregister_BNetzA_<YYYY-MM-01>.csv` on `data.bundesnetzagentur.de`
(~117 000 public charging stations, CC BY 4.0). The Bundesnetzagentur publishes one edition
per month and **deletes the previous one** — there is no `latest` alias and no archive, so
the adapter resolves the absolute URL from the landing page at fetch time
(`docs/data/sources.md` §1).

**Why `0 4 3 * *`.** The file is dated the first of the month but appears during the first
few days, not at midnight on the 1st. Running on the **3rd at 04:00 UTC** gives the
publisher two days of slack while still landing well inside the month the data describes.
It is early enough that a failure leaves ~27 days of manual-retry room before the edition
is deleted. Running on the 1st would mostly schedule retries against a 404.

## Pipeline

```
fetch_bnetza_charging      cli ingest charging --mode live --dry-run --limit N
validate_raw_dataset       DAG-side gate on the downloaded bytes and the provider mode
source_changed_gate        short circuit: skip the load when the SHA-256 is unchanged
transform_charging_data    cli ingest charging --mode cached --dry-run
load_postgis               cli ingest charging --mode cached
run_dbt_models             dbt seed + dbt run  --select stg_charging_stations+ ...
run_data_quality_checks    cli quality report  +  dbt test --select ...
record_source_fingerprint  persist the SHA-256 that is now in the database

(`cli` is `python -m autotwin_ingestion.cli`.)
```

`transform_charging_data` and `load_postgis` are the same command with and without
`--dry-run`: a *plan* and an *apply*. The plan parses, normalises and quality-checks the
file and writes a `data_ingestion_runs` row with its `quality_report` (BUILD_SPEC §6)
**without touching the domain tables**, so a BNetzA schema change — a renamed column, a
flipped encoding, a changed preamble length — fails the run before any transaction opens
against PostGIS. The cost is one extra CSV parse (seconds); the benefit is that the
database is never the place where a broken source is discovered.

Both run with `--mode cached`, which BUILD_SPEC §5 defines as *"try live, fall back to
cache/fixture"* against a `CACHE_TTL_SECONDS` (6 h by default) on-disk cache. Within one
DAG run that means the plan and the apply see the bytes `fetch_bnetza_charging` just wrote
— the TTL is three orders of magnitude longer than the run. It is **not** a guarantee that
the network is out of the path; it is the mode that prefers the cache and still degrades
gracefully, which is the honest thing to ask for here. `--mode live` is reserved for the
fetch step, where re-resolving the landing-page URL is the whole job.

## The SHA-256 short circuit — what it actually does

BNetzA republishes the full register every month. Most of it is unchanged, and the load is
an upsert on `charging_stations.external_id`, so re-loading an identical file is expensive
and pointless. `source_changed_gate` is a `ShortCircuitOperator`: when the fingerprint of
the freshly fetched file equals the one stored by the last **fully successful** run, the
whole downstream chain is skipped and the run is marked success.

The fingerprint is resolved in this order, and the DAG logs which tier answered:

1. `source_file_sha256` as reported by the CLI's JSON log stream. Authoritative — it is the
   value the CLI itself persists on the `data_ingestion_runs` row (BUILD_SPEC §3.2).
2. A streaming SHA-256 the DAG computes over the newest matching artefact in `data/raw/`.
   Independent of the CLI's log format, dependent on the cache layout.
3. Nothing. **The gate then fails open and the load proceeds.** Re-loading identical data
   is wasteful; skipping a load because a fingerprint could not be read would be a silent
   data-freshness bug, and that is the worse failure.

Cross-run state lives in the Airflow Variable
`autotwin_last_loaded_sha256__ingest_charging_infrastructure`, written by
`record_source_fingerprint` — the last task, so the fingerprint is only remembered once the
data is loaded, the dbt models are rebuilt and the tests have passed. Delete the Variable,
or trigger with `{"force_reload": true}`, to force a full reload.

## Honest limits

* `--limit` on the fetch step is a *probe*: the file must be downloaded before it can be
  parsed at all, so the step always pays for the download (that is the point — it is the
  fetch), and `--limit` only keeps it from parsing all 117 000 rows twice. If the ingestion
  CLI ignores `--limit` in the dry-run path, this step simply costs a full parse. Nothing
  breaks either way.
* The fingerprint gate protects against *re-loading the same bytes*, not against a BNetzA
  edition that changes without changing size or content ordering. A byte-identical file is
  a byte-identical load; that is the whole guarantee, and it is exactly what SHA-256 gives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import common
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.models.param import Param
from airflow.operators.python import get_current_context

DAG_ID = "ingest_charging_infrastructure"

#: Name fragments of the cached BNetzA download. The register has been published as
#: `Ladesaeulenregister_BNetzA_<date>.csv`; older editions used `Ladesaeulenregister.xlsx`.
#: Matching on the source's own vocabulary keeps the DAG independent of the exact cache
#: filename, which the ingestion layer owns.
RAW_PATTERNS = (r"ladesaeul", r"bnetza", r"charging.*station")

#: The 2026 editions are ~117 000 rows over 47 columns. Anything under 4 MiB is a truncated
#: download, an error page saved as CSV, or a fixture — never a real register.
MIN_RAW_BYTES = 4 * 1024 * 1024

#: Below this, something silently dropped most of the register and the load must not be
#: fingerprinted as "done". The real figure is ~117 000.
MIN_ACCEPTED_ROWS = 50_000

#: More than one rejected row in ten means the parser and the source disagree about the
#: file's shape, not that a handful of stations have a bad coordinate.
MAX_REJECT_RATIO = 0.10

DBT_SELECTOR = ("stg_charging_stations+", "stg_charging_points+")

FETCH_LOG = common.scratch_file("fetch.jsonl")
TRANSFORM_LOG = common.scratch_file("transform.jsonl")


@dag(
    dag_id=DAG_ID,
    description="BNetzA Ladesäulenregister → PostGIS → dbt marts (monthly, official data)",
    schedule="0 4 3 * *",
    start_date=datetime(2026, 1, 3, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=3),
    default_args=common.default_args(execution_timeout=timedelta(minutes=45)),
    tags=[common.TAG_INGESTION, "bnetza", common.TAG_OFFICIAL],
    doc_md=__doc__,
    params={
        "force_reload": Param(
            False,
            type="boolean",
            title="Force reload",
            description="Bypass the SHA-256 short circuit and load even if the file is unchanged.",
        ),
        "probe_rows": Param(
            1,
            type="integer",
            minimum=1,
            title="Fetch probe rows",
            description="Rows the fetch step parses. The download happens regardless.",
        ),
    },
)
def ingest_charging_infrastructure() -> None:
    """Assemble the monthly charging-infrastructure pipeline."""

    fetch = common.bash_step(
        task_id="fetch_bnetza_charging",
        commands=[
            common.autotwin_command(
                "autotwin_ingestion.cli",
                "ingest",
                "charging",
                "--mode",
                "live",
                "--dry-run",
                "--limit",
                "{{ params.probe_rows }}",
            )
        ],
        capture_to=FETCH_LOG,
        execution_timeout=timedelta(minutes=20),
        doc=(
            "Resolves the current BNetzA download URL, fetches the CSV into `data/raw/` and "
            "records a `data_ingestion_runs` row. Writes no domain rows: this step exists to "
            "get the bytes and their fingerprint, nothing else."
        ),
    )

    @task(task_id="validate_raw_dataset", retries=0)
    def validate_raw_dataset(facts_path: str) -> dict[str, Any]:
        """Gate the downloaded artefact before anything parses all of it.

        Three hard failures, each of which a retry cannot fix — hence `retries=0` and
        `AirflowFailException`:

        * the provider answered from the **bundled fixture**. Degrading to a fixture is
          correct for a web request (BUILD_SPEC §4) and wrong for a scheduled load: it
          would write demo bytes into the warehouse under `data_origin = official`, which
          is precisely the dishonesty §0.2 forbids. A `cache` answer is fine — those are
          still the publisher's bytes.
        * the artefact is implausibly small.
        * the CLI reported an error.
        """
        facts = common.read_cli_facts(facts_path)
        mode = facts.get("provider_mode") or facts.get("mode")
        artefact = common.newest_raw_artefact(RAW_PATTERNS)

        if facts.get("error_message"):
            raise AirflowFailException(f"BNetzA fetch reported an error: {facts['error_message']}")

        if mode == "fixture":
            raise AirflowFailException(
                "BNetzA provider degraded to the bundled fixture. A scheduled official load "
                "must not write fixture bytes into PostGIS — check the landing-page URL "
                "resolution and re-run. (A 'cache' answer would have been accepted.)"
            )

        size_bytes: int | None = None
        fingerprint = facts.get("source_file_sha256")
        fingerprint_origin = "cli"

        if artefact is not None:
            size_bytes = artefact.stat().st_size
            if size_bytes < MIN_RAW_BYTES:
                raise AirflowFailException(
                    f"Cached BNetzA artefact {artefact} is {size_bytes} bytes, below the "
                    f"{MIN_RAW_BYTES}-byte floor for a ~117 000-row register. Treating this "
                    "as a truncated download rather than loading it."
                )
            if not isinstance(fingerprint, str) or not fingerprint:
                fingerprint = common.sha256_of_file(artefact)
                fingerprint_origin = "dag-computed"
        elif not isinstance(fingerprint, str) or not fingerprint:
            fingerprint_origin = "unresolved"

        if fingerprint_origin == "unresolved":
            common.LOGGER.warning(
                "No SHA-256 available: the CLI log carried none and no artefact under %s "
                "matched %s. The change gate will fail open and the load will proceed.",
                common.RAW_DIR,
                RAW_PATTERNS,
            )

        result = {
            "fingerprint": fingerprint if isinstance(fingerprint, str) else None,
            "fingerprint_origin": fingerprint_origin,
            "provider_mode": mode,
            "artefact": str(artefact) if artefact else None,
            "artefact_bytes": size_bytes,
            "source_url": facts.get("source_url"),
        }
        common.LOGGER.info("BNetzA raw dataset accepted: %s", result)
        return result

    @task.short_circuit(task_id="source_changed_gate")
    def source_changed_gate(validation: dict[str, Any]) -> bool:
        """Skip the load when this month's file is byte-identical to the last loaded one.

        Returning `False` skips every downstream task and marks the run successful, which
        is the honest outcome: nothing changed, so nothing needed doing.
        """
        params = get_current_context()["params"]
        if params.get("force_reload"):
            common.LOGGER.info("force_reload=true — bypassing the fingerprint gate.")
            return True

        fingerprint = validation.get("fingerprint")
        if not fingerprint:
            common.LOGGER.warning(
                "Fingerprint unresolved (%s) — failing open and loading. Re-loading identical "
                "data wastes minutes; skipping a real update loses a month of data.",
                validation.get("fingerprint_origin"),
            )
            return True

        previous = common.get_last_fingerprint(DAG_ID)
        if previous == fingerprint:
            common.LOGGER.info(
                "BNetzA file unchanged since the last successful load (sha256=%s, resolved "
                "via %s). Skipping transform, load, dbt and tests.",
                fingerprint,
                validation.get("fingerprint_origin"),
            )
            return False

        common.LOGGER.info(
            "BNetzA file changed: %s -> %s (resolved via %s). Proceeding with the load.",
            previous or "<no previous run>",
            fingerprint,
            validation.get("fingerprint_origin"),
        )
        return True

    transform = common.bash_step(
        task_id="transform_charging_data",
        commands=[
            common.autotwin_command(
                "autotwin_ingestion.cli",
                "ingest",
                "charging",
                "--mode",
                "cached",
                "--dry-run",
            )
        ],
        capture_to=TRANSFORM_LOG,
        execution_timeout=timedelta(minutes=25),
        doc=(
            "The *plan*. Parses and normalises the file the fetch step cached, builds the "
            "`QualityReport` and persists it on a `data_ingestion_runs` row — without "
            "writing a single domain row. A BNetzA schema change fails here, not inside "
            "the load transaction."
        ),
    )

    load = common.bash_step(
        task_id="load_postgis",
        commands=[
            common.autotwin_command(
                "autotwin_ingestion.cli",
                "ingest",
                "charging",
                "--mode",
                "cached",
            )
        ],
        execution_timeout=timedelta(minutes=40),
        doc=(
            "The *apply*. Same command without `--dry-run`: upserts `charging_stations` and "
            "`charging_points` on `external_id`, writing `geography(Point,4326)` locations "
            "and the provenance block (BUILD_SPEC §3.1)."
        ),
    )

    dbt_models = common.bash_step(
        task_id="run_dbt_models",
        commands=[
            common.dbt_deps_if_needed(),
            common.dbt_seed(),
            common.dbt_command("run", "--select", *DBT_SELECTOR),
        ],
        execution_timeout=timedelta(minutes=20),
        doc=(
            "Rebuilds the charging lineage: staging → `int_charging_station_power` → "
            "`dim_charging_station` / `mart_charging_coverage` / "
            "`mart_charging_infrastructure`. The `+` selectors follow the DAG downstream so "
            "the mart list does not have to be repeated here. `dbt seed` runs first because "
            "`stg_charging_stations.bundesland` has a `relationships` test against the "
            "`bundesland_reference` seed."
        ),
    )

    quality = common.bash_step(
        task_id="run_data_quality_checks",
        commands=[
            # The report first, deliberately: `set -e` means a failing dbt test ends the
            # step, and the ingestion counters are exactly what a reader needs in order to
            # interpret that failure. Printing them afterwards would print them never.
            common.autotwin_command("autotwin_ingestion.cli", "quality", "report"),
            common.dbt_command("test", "--select", *DBT_SELECTOR),
        ],
        execution_timeout=timedelta(minutes=20),
        doc=(
            "The ingestion quality report (rejection counters for this run, BUILD_SPEC §6) "
            "followed by dbt's `unique` / `not_null` / `accepted_values` / `relationships` "
            "tests over the charging lineage."
        ),
    )

    @task(task_id="record_source_fingerprint", retries=0)
    def record_source_fingerprint(validation: dict[str, Any], transform_log: str) -> None:
        """Remember what is now in the database — but only if the load was substantial.

        This is the last task for a reason: the fingerprint means "these bytes are loaded,
        modelled and tested", and recording it earlier would let a half-finished run
        suppress next month's load.

        The row-count floor is enforced *here* rather than in `validate_raw_dataset`
        because the fetch step only probes a few rows; the plan step is the first place a
        real count exists. A run that accepted 400 of 117 000 rows must fail loudly instead
        of quietly fingerprinting a broken load as complete.
        """
        facts = common.read_cli_facts(transform_log)
        received = facts.get("rows_received")
        accepted = facts.get("rows_accepted")
        rejected = facts.get("rows_rejected")

        if isinstance(accepted, int | float):
            if accepted < MIN_ACCEPTED_ROWS:
                raise AirflowFailException(
                    f"Only {int(accepted)} rows accepted (floor {MIN_ACCEPTED_ROWS}). The load "
                    "is not being fingerprinted, so the next run will retry the same file."
                )
            if isinstance(rejected, int | float) and (accepted + rejected) > 0:
                ratio = float(rejected) / float(accepted + rejected)
                if ratio > MAX_REJECT_RATIO:
                    raise AirflowFailException(
                        f"Rejection ratio {ratio:.1%} exceeds {MAX_REJECT_RATIO:.0%} "
                        f"({int(rejected)} of {int(accepted + rejected)}). That is a schema "
                        "disagreement, not dirty rows — see the quality report."
                    )
        else:
            common.LOGGER.warning(
                "No row counters in the plan log (%s); skipping the volume gate. The load "
                "itself succeeded, so the fingerprint is still recorded.",
                transform_log,
            )

        fingerprint = validation.get("fingerprint")
        if not fingerprint:
            common.LOGGER.warning(
                "No fingerprint to record — the next run will load again. This is the "
                "fail-open path, not an error."
            )
            return

        common.set_last_fingerprint(DAG_ID, str(fingerprint))
        common.LOGGER.info(
            "Recorded sha256=%s for %s (received=%s accepted=%s rejected=%s).",
            fingerprint,
            DAG_ID,
            received,
            accepted,
            rejected,
        )

    validation = validate_raw_dataset(FETCH_LOG)
    gate = source_changed_gate(validation)
    recorded = record_source_fingerprint(validation, TRANSFORM_LOG)

    fetch >> validation >> gate >> transform >> load >> dbt_models >> quality >> recorded


ingest_charging_infrastructure()
