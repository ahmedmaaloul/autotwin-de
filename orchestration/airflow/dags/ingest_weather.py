"""Hourly ingestion of DWD station observations into PostGIS.

**Source.** Deutscher Wetterdienst Open Data, the 10-minute *"now"* products
(`10_minutes/air_temperature/now`, `.../wind/now`, `.../precipitation/now`) — one ZIP per
station, ISO-8859-1, `;`-delimited with space padding, `-999` for missing, `MESS_DATUM` in
UTC (`docs/data/sources.md` §2). CC BY 4.0; „Quelle: Deutscher Wetterdienst" is rendered
wherever the data surfaces.

**Why `15 * * * *`.** The "now" directory is refreshed continuously in ten-minute steps,
so there is no publication instant to chase. Running at quarter past the hour puts the load
comfortably after the top-of-hour rollover and away from the hour boundary, where every
other naive scheduler on the internet is hammering the same server. One run per hour is
also all the energy model needs: segment temperature is interpolated from the nearest
station, and ten-minute resolution does not change a kWh/100 km figure.

## Pipeline

```
fetch_dwd_observations   python -m autotwin_ingestion.cli ingest weather --mode live --stations N
validate_ingestion       DAG-side gate on the run's counters and provider mode
run_dbt_models           dbt run  --select stg_weather_observations+
run_dbt_tests            dbt test --select stg_weather_observations+
```

Lighter than the charging DAG on purpose, and the differences are deliberate rather than
accidental:

* **No plan/apply split.** A DWD "now" file is a few kilobytes per station and the write is
  an upsert on `UNIQUE(weather_station_id, observed_at)` (BUILD_SPEC §3.2). Re-parsing
  before loading would buy nothing a failed transaction does not already give us.
* **No fingerprint short circuit.** Every hour genuinely brings new observations. A change
  gate here would be pure ceremony — and worse, it would hide a frozen upstream feed behind
  a green "skipped" run instead of surfacing it. Staleness is detected where it belongs, in
  `data_quality_report`, which fails when DWD has not produced a fresh row in three hours.
* **Three retries instead of two.** An hourly job against a public open-data server sees
  transient 5xx and connection resets; the next scheduled run is only an hour away, so
  spending a few extra minutes on backoff is cheap insurance against a one-hour gap.

## Honest limits

Only *observations* are ingested. MOSMIX forecasts (KML inside KMZ, column-oriented
`<dwd:Forecast>` elements) are documented in the source-verification record but are not
modelled: the energy model is evaluated against what the weather *was*, not what it was
predicted to be, and a forecast table nobody reads would be schema for its own sake.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import common
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.models.param import Param

DAG_ID = "ingest_weather"

#: A run that accepts nothing is a broken run: the "now" directory always holds current
#: observations for hundreds of stations, so zero accepted rows means parsing failed, not
#: that Germany had no weather.
MIN_ACCEPTED_ROWS = 1

#: DWD marks missing measurements as `-999`, which the parser drops before validation, so a
#: healthy run rejects very little. A third of the payload failing the quality rules means
#: the column layout moved (the 10-minute and hourly products differ in `MESS_DATUM` width).
MAX_REJECT_RATIO = 0.33

DBT_SELECTOR = ("stg_weather_observations+",)

FETCH_LOG = common.scratch_file("fetch.jsonl")


@dag(
    dag_id=DAG_ID,
    description="DWD 10-minute station observations → PostGIS → dbt (hourly, official data)",
    schedule="15 * * * *",
    start_date=datetime(2026, 1, 1, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=50),
    default_args=common.default_args(
        retries=3,
        retry_delay=timedelta(minutes=2),
        max_retry_delay=timedelta(minutes=15),
        execution_timeout=timedelta(minutes=15),
    ),
    tags=[common.TAG_INGESTION, "dwd", common.TAG_OFFICIAL],
    doc_md=__doc__,
    params={
        "stations": Param(
            120,
            type="integer",
            minimum=1,
            maximum=600,
            title="Stations per run",
            description=(
                "Number of DWD stations to pull. One HTTPS request per station per product; "
                "120 covers Germany densely enough for nearest-station interpolation."
            ),
        ),
    },
)
def ingest_weather() -> None:
    """Assemble the hourly weather pipeline."""

    fetch = common.bash_step(
        task_id="fetch_dwd_observations",
        commands=[
            common.autotwin_command(
                "autotwin_ingestion.cli",
                "ingest",
                "weather",
                "--mode",
                "live",
                "--stations",
                "{{ params.stations }}",
            )
        ],
        capture_to=FETCH_LOG,
        doc=(
            "Downloads the per-station `now` ZIPs, decodes ISO-8859-1, normalises `-999` to "
            "NULL and upserts `weather_observations` on "
            "`(weather_station_id, observed_at)`. Writes a `data_ingestion_runs` row."
        ),
    )

    @task(task_id="validate_ingestion", retries=0)
    def validate_ingestion(facts_path: str) -> dict[str, Any]:
        """Assert the run actually moved data, and that it moved *real* data.

        A fixture answer is tolerated here — unlike in the monthly BNetzA load — because
        the only consumers of an hourly weather row are the energy model's segment
        enrichment and the dashboard's weather tile, both of which already surface
        `X-AutoTwin-Data-Mode` to the user (BUILD_SPEC §7). It is logged as a warning so a
        persistently degraded source is visible in the task log, and the daily quality
        report is what turns "degraded for hours" into a failure.
        """
        facts = common.read_cli_facts(facts_path)
        mode = facts.get("provider_mode") or facts.get("mode")
        accepted = facts.get("rows_accepted")
        rejected = facts.get("rows_rejected")

        if facts.get("error_message"):
            raise AirflowFailException(f"DWD ingestion reported an error: {facts['error_message']}")

        if mode in {"cache", "fixture"}:
            common.LOGGER.warning(
                "DWD provider answered from '%s', not 'live'. Observations may be stale; "
                "`data_quality_report` will fail if this persists past the %s freshness "
                "threshold.",
                mode,
                common.FRESHNESS_THRESHOLDS[common.SOURCE_DWD],
            )

        if isinstance(accepted, int | float):
            if accepted < MIN_ACCEPTED_ROWS:
                raise AirflowFailException(
                    "DWD ingestion accepted no rows. The `now` directory is never empty, so "
                    "this is a parsing or connectivity failure, not an empty source."
                )
            if isinstance(rejected, int | float) and (accepted + rejected) > 0:
                ratio = float(rejected) / float(accepted + rejected)
                if ratio > MAX_REJECT_RATIO:
                    raise AirflowFailException(
                        f"DWD rejection ratio {ratio:.1%} exceeds {MAX_REJECT_RATIO:.0%}. "
                        "Check `MESS_DATUM` width and the ISO-8859-1 decode — the 10-minute "
                        "and hourly products do not share a timestamp format."
                    )
        else:
            common.LOGGER.warning(
                "No row counters in %s; the volume gate was skipped. The CLI exited 0, so "
                "the run is treated as successful.",
                facts_path,
            )

        summary = {
            "provider_mode": mode,
            "rows_accepted": accepted,
            "rows_rejected": rejected,
        }
        common.LOGGER.info("DWD ingestion accepted: %s", summary)
        return summary

    dbt_models = common.bash_step(
        task_id="run_dbt_models",
        commands=[common.dbt_command("run", "--select", *DBT_SELECTOR)],
        doc="Rebuilds `stg_weather_observations` and everything downstream of it.",
    )

    dbt_tests = common.bash_step(
        task_id="run_dbt_tests",
        commands=[common.dbt_command("test", "--select", *DBT_SELECTOR)],
        doc=(
            "`not_null` on the observation key, `accepted_values` on `condition` "
            "(BUILD_SPEC §2), and the Germany-bounding-box singular test."
        ),
    )

    validation = validate_ingestion(FETCH_LOG)
    fetch >> validation >> dbt_models >> dbt_tests


ingest_weather()
