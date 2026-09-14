# `data/` — the AutoTwin DE data zone

Everything AutoTwin reads from disk lives here: the provider cache, the bundled fixture
overrides, the archived source downloads and the Parquet analytics zones. The directory
structure is committed; **the payload never is** (see [Why it is git-ignored](#why-it-is-git-ignored)).

```
data/
├── cache/     provider responses, keyed by URL, expiring after AUTOTWIN_CACHE_TTL_SECONDS
├── fixtures/  operator overrides for the bundled fixtures (usually empty)
├── raw/       source documents exactly as they were served, each with a JSON sidecar
├── bronze/    every parsed record — accepted *and* rejected — as Parquet
├── silver/    accepted records only, typed and analysis-ready, as Parquet
└── gold/      business marts (dbt writes these into PostgreSQL; exports land here)
```

The zones are managed by `autotwin_ingestion.lake.DataLake`. Nothing else writes to them.

---

## What each zone holds, and why

### `cache/` — the middle rung of the fallback chain

Every adapter walks `live → on-disk cache → bundled fixture` (BUILD_SPEC §4) and this is the
middle rung. Entries are named by the SHA-256 of `namespace\0key`, grouped per source
(`cache/dwd/…`, `cache/autobahn/…`), and expire after `AUTOTWIN_CACHE_TTL_SECONDS` (6 h by
default, which matches the DWD publishing cadence). A provider serving from here reports
`ProviderMode.cache`, and the age shown in the UI is the age of the *cached copy*, never the
age of the request.

Two entries are deliberately long-lived: OSRM routes and Nominatim results have a TTL of `0`
(never expire). A road does not move, and re-asking a free community server the same question
every six hours would be rude.

### `raw/` — the audit trail

Source documents, byte for byte, plus a `<file>.meta.json` sidecar:

```json
{
  "kind": "source_download",
  "file": "Ladesaeulenregister_BNetzA_2026-09-01_cdd8e38f1fa7.csv",
  "source_url": "https://data.bundesnetzagentur.de/.../Ladesaeulenregister_BNetzA_2026-09-01.csv",
  "fetched_at": "2026-09-14T15:42:19.259861+00:00",
  "sha256": "cdd8e38f1fa7e851…",
  "bytes": 55559083,
  "content_type": "text/csv; charset=utf-8"
}
```

`sha256` is the same value written to `data_ingestion_runs.source_file_sha256`, which is what
makes "this ingestion run read *that* file" checkable months later rather than merely asserted.

`kind` distinguishes two things that must never be confused:

| `kind` | meaning | written by |
|---|---|---|
| `source_download` | the upstream bytes, unmodified | the Ladesäulenregister ingestion |
| `normalised_snapshot` | the parsed record set of one run, as JSON | the weather and traffic ingestions |

The weather and traffic adapters fan a single call out over dozens of HTTP requests and return
parsed records, so the pipeline never sees one document to archive. It writes the normalised
record set instead — enough to replay a run offline, and labelled so nobody mistakes it for the
source. Those runs therefore leave `bytes_downloaded` **null** on their ingestion row: the
pipeline genuinely does not know how many bytes crossed the wire, and a plausible-looking
number would be a lie.

### `bronze/` — the rows PostgreSQL never sees

One row per *parsed source record*, whether it was written or not, with three extra columns:

| column | meaning |
|---|---|
| `quality_status` | `accepted` · `rejected` · `duplicate` |
| `quality_rule` | the first rule that fired, e.g. `within_germany_bbox` |
| `quality_message` | the human-readable violation |

This is the zone's reason to exist. BUILD_SPEC §6 requires pipelines to **reject** bad rows
rather than write them, so a station with a swapped latitude never reaches the database — and
without bronze, the rejected population would be invisible to analysis. `data_ingestion_runs`
says *how many* rows were rejected; bronze says *which ones*.

### `silver/` — analysis-ready

Accepted records only, flattened to scalar columns (`latitude`/`longitude` instead of a nested
coordinate, enum values instead of enum objects), typed and safe to join. This is what the
research notebook reads.

### `gold/` — marts

The gold layer of this project is produced by **dbt into the `analytics` schema of PostgreSQL**
(BUILD_SPEC §16), not by the lake. This zone is where an *export* of a mart lands when someone
wants one as a file. It is empty until something exports into it — deliberately, not by
oversight.

---

## Partitioning and idempotency

Parquet datasets are Hive-partitioned by ingestion date:

```
bronze/charging_stations/dt=2026-09-14/part-0000.parquet
                                       part-0001.parquet
silver/charging_stations/dt=2026-09-14/part-0000.parquet
```

Writing a partition **replaces** the parts already in it, so running a pipeline twice on the
same day leaves one copy of that day's data rather than two — the same idempotency the database
upserts give. Parts hold 50 000 rows each, which turns the 117 000-row Ladesäulenregister into
three files instead of sixty.

---

## How provenance is recorded

Provenance is recorded in four places, and they agree:

1. **The sidecar** in `raw/` — URL, timestamp, SHA-256, byte count.
2. **`data_ingestion_runs`** — one row per pipeline execution, carrying `provider_mode`
   (`live` · `cache` · `fixture`), the four row counters, `source_file_sha256`,
   `bytes_downloaded` and the full `quality_report` JSON. A run that fails still writes this
   row, with `status = failed` and the error message; that is what `/data-quality` reads.
3. **The provenance block on every ingested row** (BUILD_SPEC §3.1) — `source`,
   `source_identifier`, `source_url`, `source_timestamp`, `data_origin`, `ingestion_run_id`,
   `ingested_at`. `ingestion_run_id` is the join back to (2).
4. **The Parquet zones** — the same provenance columns, flattened.

`data_origin` is never inferred: `official` for the German open-data sources, `derived` for the
routed corridors (OSRM computed them from OpenStreetMap; the path is AutoTwin's derivation) and
`simulated` for anything the simulator invents.

---

## How to reproduce

```bash
# One command: profiles, corridors, charging, weather, traffic, simulated vehicles.
uv run python -m autotwin_ingestion.cli seed demo

# Or a source at a time. --mode fixture never touches the network.
uv run python -m autotwin_ingestion.cli ingest charging --mode cached --limit 5000
uv run python -m autotwin_ingestion.cli ingest weather  --mode cached --stations 8
uv run python -m autotwin_ingestion.cli ingest traffic  --mode cached --roads A5,A8
uv run python -m autotwin_ingestion.cli seed routes     --mode cached

# What happened, per source, including the rules that fired.
uv run python -m autotwin_ingestion.cli quality report
```

`--dry-run` fetches and validates without writing anything — not even a `data_ingestion_runs`
row, because a dry run is a question about the source rather than an event in the ingestion
history.

Re-running is safe by construction. Every pipeline upserts on a natural key
(`charging_stations.external_id`, `weather_observations.(station, observed_at)`,
`traffic_events.external_id`, `routes.slug`), and the charging upsert additionally compares
every payload column, so re-ingesting an unchanged edition of the register updates **zero**
rows.

### Querying the Parquet zones

```python
from autotwin_ingestion.lake import DataLake

lake = DataLake()
lake.views()          # ['bronze_charging_stations', 'silver_charging_stations', ...]

lake.query("""
    SELECT bundesland,
           count(*)                       AS sites,
           count(*) FILTER (WHERE is_fast_charger) AS fast_sites,
           round(median(max_power_kw), 1) AS median_kw
    FROM silver_charging_stations
    GROUP BY 1
    ORDER BY sites DESC
""")

# Which rows were thrown away, and by which rule?
lake.query("""
    SELECT quality_rule, count(*)
    FROM bronze_charging_stations
    WHERE quality_status <> 'accepted'
    GROUP BY 1 ORDER BY 2 DESC
""")
```

DuckDB reads the Parquet files directly; there is no database file to keep in sync.

---

## Why it is git-ignored

`.gitignore` excludes `data/raw/**`, `data/bronze/**`, `data/silver/**`, `data/gold/**` and
every `*.parquet`, while keeping `.gitkeep` and `README.md`. Three reasons:

1. **Size.** One edition of the Ladesäulenregister is 55 MB. Twelve monthly editions would make
   the repository larger than every line of code in it by two orders of magnitude, permanently,
   because git never forgets a blob.
2. **Licensing.** The Bundesnetzagentur and DWD data is CC BY 4.0 and redistributable *with
   attribution*, but the Autobahn GmbH API publishes **no licence statement** at all
   (see `DATA_LICENSES.md`). AutoTwin treats it as available for development, licence
   unconfirmed: cached locally, never redistributed. Committing a cache of it would be
   redistribution.
3. **Freshness.** Committed data rots. The register rotates monthly and the previous month is
   deleted upstream; traffic events are minutes old. A checkout should fetch current data, not
   inherit a snapshot of whenever the last commit happened.

**Fixtures are the deliberate exception.** A trimmed, attributed sample of each source is
committed inside the wheel at `services/ingestion/src/autotwin_ingestion/fixtures/`, so
`--mode fixture` works offline, in CI and on a train. Those samples keep the original shape —
the ten-line BNetzA preamble, the ISO-8859-1 DWD encoding, the lat-first Autobahn `point`
strings — so the parser exercised offline is the parser that runs in production. Drop a file
into `data/fixtures/` to override a bundled one without rebuilding the package.

---

## Licences and attribution

| source | licence | attribution |
|---|---|---|
| Bundesnetzagentur Ladesäulenregister | CC BY 4.0 | © Bundesnetzagentur |
| DWD Open Data | CC BY 4.0 | „Quelle: Deutscher Wetterdienst" — required on anything derived |
| Autobahn GmbH API | **undeclared** | treated as development-only, never redistributed |
| OSRM / OpenStreetMap | ODbL 1.0 | © OpenStreetMap-Mitwirkende · Routing: OSRM |

Full terms in [`DATA_LICENSES.md`](../DATA_LICENSES.md); the source-by-source parsing brief is
in [`docs/data/sources.md`](../docs/data/sources.md).
