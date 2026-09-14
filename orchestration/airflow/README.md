# Airflow orchestration

Scheduled ingestion, retraining and data-quality checks for AutoTwin DE.

Five DAGs, all of which do the same thing a developer does by hand: shell out to the
command-line contract in [`docs/BUILD_SPEC.md` §15](../../docs/BUILD_SPEC.md) and to dbt
(§16). Nothing here imports AutoTwin code, and nothing in the application imports anything
here.

---

## 1. Quick start

```bash
make airflow-up      # docker compose --profile airflow up -d
```

| | |
|---|---|
| Web UI | <http://localhost:8082> |
| Username / password | `autotwin` / `autotwin` |
| Executor | `LocalExecutor` |
| Image | `apache/airflow:2.10.4-python3.12` |
| Metadata DB | the `airflow` database on the same Postgres container as the application |
| DAG folder | `orchestration/airflow/dags`, bind-mounted to `/opt/airflow/dags` |

```bash
make airflow-down    # stops the three Airflow containers, leaves Postgres running
```

> ### The credentials are local-only
>
> `autotwin` / `autotwin` is a fixed development login created by the `airflow-init`
> one-shot in `docker-compose.yml`, for a service bound to `localhost` on a laptop.
> It is written down in the compose file, in the Makefile banner and here, which is
> precisely the point: it is **not a secret and must never be treated as one**.
>
> The same profile deliberately leaves `AIRFLOW__CORE__FERNET_KEY` unset, so Airflow
> Connection passwords are not encrypted at rest. Do not put a real credential into this
> Airflow. If this were ever deployed anywhere reachable, the login, the Fernet key and the
> `EXPOSE_CONFIG: "true"` setting would all have to change first.

Every DAG is created **paused** (`AIRFLOW__CORE__DAGS_ARE_PAUSED_AT_CREATION: "true"`).
Unpause the ones you want, or trigger a single run from the UI — a fresh checkout will not
start hitting federal open-data servers on its own.

---

## 2. Why Airflow is an optional profile, not a service

`make demo` brings up Postgres, seeds the schema and runs the simulator. It does not need a
scheduler, and adding one to the critical path would mean a reviewer cloning this repository
waits for an Airflow metadata migration before seeing a map.

The division of responsibility is deliberate:

* **The CLI is the unit of work.** `python -m autotwin_ingestion.cli ingest charging` is a
  complete, testable, exit-code-returning program that writes its own `data_ingestion_runs`
  row (§15). It works identically from a Makefile target, from a cron entry, from a
  container `command:`, and from a `BashOperator`.
* **Airflow adds scheduling, retries, backfill boundaries, per-task logs and a change
  gate** — and nothing else. It is not where business logic lives.
* Consequently the application degrades to "nobody is calling the CLI" when Airflow is off,
  not to "the application is broken". `GET /api/v1/data/quality` still reports the true age
  of every source; it just reports that the ages are growing.

This is also why the DAG files contain no `import autotwin_core`. The scheduler re-parses
every file in the DAG folder every ~30 seconds; importing SQLAlchemy models, Pydantic
settings and a database URL into that loop is a well-known way to make an Airflow
deployment slow and brittle. The coupling between this directory and the rest of the
repository is a **process boundary**, not an import.

---

## 3. Wiring required before the tasks can run

**Read this before you conclude the DAGs are broken.**

The DAGs parse and schedule with the compose profile exactly as committed. They cannot
*execute* until the Airflow containers can see the repository and the workspace packages,
because every task is a shell command that runs `python -m autotwin_*.cli` or `dbt` with
`cwd` set to the repository root.

`common.py` expects the checkout at **`/opt/autotwin`** (override with
`AUTOTWIN_REPO_ROOT`). At the time this file was written, the `x-airflow` fragment in
`docker-compose.yml` mounts only `dags/`, `logs/` and `plugins/`, and installs nothing
extra — so the first task of the first run fails with

```
AirflowException: Can not find the cwd: /opt/autotwin
```

which is the intended failure: loud, immediate, and not a confusing `ModuleNotFoundError`
three steps later. `docker-compose.yml` is owned elsewhere in the repository; the three
lines it needs are:

```yaml
x-airflow: &airflow
  # ... existing keys ...
  environment:
    # ... existing keys ...
    AUTOTWIN_REPO_ROOT: /opt/autotwin
    _PIP_ADDITIONAL_REQUIREMENTS: "-r /opt/autotwin/orchestration/airflow/requirements.txt"
  volumes:
    - .:/opt/autotwin                     # <- the repository itself
    - ./orchestration/airflow/dags:/opt/airflow/dags
    - ./orchestration/airflow/logs:/opt/airflow/logs
    - ./orchestration/airflow/plugins:/opt/airflow/plugins
```

### The two supported install paths

**A. `_PIP_ADDITIONAL_REQUIREMENTS` (above).** Zero image build. pip re-runs on every
container start, which costs a minute or two each time and is unsuitable for anything but a
laptop — the Airflow docs say as much. Good enough for "show me the DAGs working".

**B. A custom image (recommended for anything repeated).**

```dockerfile
FROM apache/airflow:2.10.4-python3.12
COPY --chown=airflow:root . /opt/autotwin
RUN pip install --no-cache-dir -r /opt/autotwin/orchestration/airflow/requirements.txt
```

…then point the `x-airflow` fragment at `build:` instead of `image:`. Keep the bind mount
as well: [`requirements.txt`](requirements.txt) installs the workspace members **editable**
from `/opt/autotwin`, so a code change on the host is picked up by the next task run
without a rebuild.

### Directory layout

```
orchestration/airflow/
  dags/              bind-mounted to /opt/airflow/dags — the five DAGs and common.py
  dags/.airflowignore  keeps the DagBag from parsing common.py as a DAG
  plugins/           bind-mounted, intentionally empty (a .gitkeep holds the path in git,
                     so Docker does not create it root-owned on a fresh clone)
  logs/              bind-mounted task logs; gitignored, created on first start
  requirements.txt   what has to be installed into the image
```

There is no `config/airflow.cfg`. Every setting the profile needs is an
`AIRFLOW__SECTION__KEY` environment variable in `docker-compose.yml`, which is one place to
look instead of two and survives an image upgrade that reorganises the default config.

### Known integration risk

Airflow 2.10 and dbt-core 1.9 both constrain `Jinja2`, `MarkupSafe` and `protobuf`. They
resolve together today with the pins in `requirements.txt`, but this is the one place in
this directory where an upstream release can break the image. If pip cannot satisfy both,
the fallback is to stop installing dbt into the Airflow image and run it as its own
container (`docker run --network autotwin ghcr.io/dbt-labs/dbt-postgres:1.9.latest …`)
invoked from the same `BashOperator`. The DAGs would not change — only
`common.DBT_BIN`.

---

## 4. The DAGs

| DAG | Schedule (UTC) | Tags | What it does |
|---|---|---|---|
| `ingest_charging_infrastructure` | `0 4 3 * *` (monthly) | `ingestion` `bnetza` `official` | BNetzA Ladesäulenregister → PostGIS → dbt marts, with a SHA-256 change gate |
| `ingest_weather` | `15 * * * *` (hourly) | `ingestion` `dwd` `official` | DWD 10-minute station observations → PostGIS → dbt |
| `ingest_traffic` | `*/15 * * * *` | `ingestion` `autobahn` `official` | Autobahn roadworks/closures/warnings → PostGIS → dbt |
| `train_energy_model` | `0 3 * * 1` (weekly) | `ml` `simulated` | Generate **simulated** training data → train → evaluate → SHAP → promotion gate |
| `data_quality_report` | `0 6 * * *` (daily) | `quality` `dbt` `observability` | Source-freshness gate + the full dbt test suite |

Each DAG carries its full reasoning in its module docstring, which Airflow renders as
`doc_md` on the DAG's own page in the UI — schedule rationale, thresholds, and what it
deliberately does *not* do. The summaries below are the short version.

### `ingest_charging_infrastructure` — the flagship

```
fetch_bnetza_charging  →  validate_raw_dataset  →  source_changed_gate  ⇢ (skip)
                                                           ↓
                       transform_charging_data  →  load_postgis  →  run_dbt_models
                       →  run_data_quality_checks  →  record_source_fingerprint
```

* The BNetzA file rotates monthly and **the previous edition is deleted** — there is no
  `latest` URL (`docs/data/sources.md` §1). The DAG runs on the **3rd**, not the 1st,
  because the file dated the 1st appears during the first few days of the month.
* `transform_charging_data` and `load_postgis` are the same command with and without
  `--dry-run`: a plan and an apply. The plan parses the file the fetch step cached and
  writes a `data_ingestion_runs` row with its `quality_report` **without touching the
  domain tables**, so a source schema change fails before any transaction opens against
  PostGIS. Both use `--mode cached` — §5's "try live, fall back to cache/fixture" with a
  6-hour TTL, which within one run means the bytes the fetch step just wrote. It prefers
  the cache; it does not promise the network is out of the path.
* **The change gate.** `source_changed_gate` is a `@task.short_circuit`. It compares the
  fingerprint of the file just fetched against the one stored by the last *fully
  successful* run, and skips the entire load when they match. The fingerprint is resolved
  in three tiers, and the DAG logs which one answered:
  1. `source_file_sha256` parsed out of the CLI's `--json-logs` stream — authoritative,
     because it is the value the CLI itself persists on `data_ingestion_runs` (§3.2);
  2. a streaming SHA-256 the DAG computes over the newest matching artefact in `data/raw/`;
  3. nothing — in which case the gate **fails open and the load proceeds**. Re-loading
     identical data wastes minutes; skipping a load because a fingerprint was unreadable
     would silently lose a month of data.

  Cross-run state is the Airflow Variable
  `autotwin_last_loaded_sha256__ingest_charging_infrastructure`, written by the *last*
  task — so a half-finished run cannot suppress next month's load. Trigger with
  `{"force_reload": true}` (or delete the Variable) to force a reload.
* A `fixture` provider answer is a hard failure here, unlike in the hourly DAGs: writing
  demo bytes into the warehouse under `data_origin = official` is exactly the dishonesty
  BUILD_SPEC §0.2 forbids. A `cache` answer is accepted — those are still the publisher's
  bytes.

### `ingest_weather`

Hourly at quarter past, against the DWD "now" products. No plan/apply split and no change
gate — every hour genuinely brings new observations, and a change gate would hide a frozen
upstream feed behind a green "skipped" run. Staleness is caught where it belongs, in
`data_quality_report`. Three retries rather than two, because a public open-data server
returns the occasional 5xx and the next scheduled run is only an hour away.

### `ingest_traffic`

Every 15 minutes, `max_active_runs=1`, `catchup=False`.

**The Autobahn API has no published licence.** It is keyless, anonymous and specified in
the federal `bundesAPI` organisation, but nothing states terms of use. `DATA_LICENSES.md`
records the position this project takes — *available for development, licence unconfirmed*:
cached locally, never redistributed, and **deliberately polled at a low rate**. That is a
property of this DAG, not a footnote:

* The API lists **108 roads**; this DAG polls **8** — the corridors the demo routes
  traverse. 8 roads × 3 endpoints = 24 requests per run, ≈ **96 requests/hour**. Polling
  everything at this cadence would be ~1 300 requests/hour against an unmetered public
  service for data nobody here reads.
* `max_active_runs=1` means a slow run delays the next one instead of stacking a second
  fan-out of requests on top of it; `catchup=False` means a restart never replays a backlog
  of 15-minute windows against a current-state feed.

dbt *tests* are not run here — they run once a day. A schema invariant that holds at 04:00
does not stop holding at 04:15 because six roadworks were added.

### `train_energy_model`

> **The labels are simulated.** The training set is aggregated telemetry from
> `autotwin_simulator`, integrated from the longitudinal road-load model in BUILD_SPEC
> §10.1 — not measured from real vehicles. Every row carries `data_origin = simulated`, the
> registry row records `training_data_origin = simulated`, and the UI labels anything
> derived from it `SIMULIERT`. What this pipeline demonstrates is a complete and honest ML
> lifecycle evaluated on data whose generating process is known. It is not evidence about
> the consumption of any real car.

```
generate_training_data → train_energy_model → evaluate_energy_model → explain_energy_model
                       → evaluate_promotion → promote_energy_model → record_active_version
```

The version is `{{ ds_nodash }}`, so `train`, `evaluate`, `explain` and `promote` all
address the same artefact without scraping a version number out of a log.

`evaluate_promotion` reads `models/energy_consumption_v{V}.metrics.json` (§10.2) and
promotes only if **both** hold:

1. `mae < baseline_mae` — it beats the physical road-load baseline on the same test split.
   A learned model that loses to the equation has nothing to add to the API.
2. `mae < active_mae * (1 - 0.001)` — it improves on the currently active version. The
   0.1 % band is a no-churn guard, not a significance test.

Otherwise the task raises `AirflowFailException`: **the run goes red and the previously
active model keeps serving**. A pipeline that silently promotes a worse model is bad; one
that silently keeps a worse model while reporting success is worse.

### `data_quality_report`

The DAG that is allowed to be red. `run_quality_report` → `check_source_freshness` →
`run_dbt_tests` (`trigger_rule=ALL_DONE`, so a stale source and a broken invariant are both
discovered in the same run) → `summarise` (always runs, one consolidated line).

Freshness thresholds live once, in `common.FRESHNESS_THRESHOLDS`, at roughly three
publication intervals each:

| source | threshold | why |
|---|---|---|
| `bundesnetzagentur` | 40 days | one edition per month, previous one deleted |
| `dwd` | 3 hours | hourly DAG against a continuously refreshed feed |
| `autobahn` | 90 minutes | 15-minute polling |

Sources with no publication schedule (`osm`, `osrm`, `simulator`, `derived`) are reported
but can never fail the run. If **no** per-source record can be parsed at all, the gate
fails — the opposite of the charging DAG's fail-open choice, and deliberately so: there, an
unreadable fingerprint costs a redundant load; here, an unreadable report *is* the failure
being reported.

---

## 5. Relationship to the Makefile

The DAGs run the same programs as the Makefile targets a developer uses by hand. Neither
wraps the other; both call the CLI.

| By hand | Scheduled equivalent |
|---|---|
| `make ingest-charging` | `ingest_charging_infrastructure` → `load_postgis` |
| `make ingest-weather` | `ingest_weather` → `fetch_dwd_observations` |
| `make ingest-traffic` | `ingest_traffic` → `fetch_autobahn_events` |
| `make dbt-run` | `run_dbt_models` in the charging / weather / traffic DAGs (with `--select`) |
| `make dbt-test` | `run_dbt_tests` in `data_quality_report` (whole suite, daily) |
| `make ml-train` / `make ml-evaluate` | `train_energy_model` → `train` / `evaluate` |
| *(no target)* | `check_source_freshness`, `source_changed_gate`, `evaluate_promotion` |

The last row is the honest summary of what Airflow adds: **gates**. Everything else is a
schedule around a command that already worked.

Two differences worth knowing:

* **dbt.** `make dbt-run` invokes dbt through `uvx --from "dbt-postgres~=1.9" dbt` so the
  host never installs it into the workspace. Inside the Airflow image dbt is a plain
  installed CLI on `PATH` (see `requirements.txt`). Same project, same `profiles.yml`, same
  `--project-dir dbt --profiles-dir dbt`, same target — a `dbt run` from the Makefile and
  one from a DAG produce the same relations.
* **`dbt seed`.** The DAGs run it before building or testing the charging lineage.
  `stg_charging_stations.bundesland` has a `relationships` test against the
  `bundesland_reference` seed; if that seed was never materialised the test does not fail,
  it *errors* with "relation does not exist", which reads like a broken warehouse rather
  than a missing command. Sixteen rows cost nothing to reload.

---

## 6. Implementation notes

**`common.py` is a library, not a DAG.** It is listed in `.airflowignore` so the DagBag
does not open it looking for a `DAG` object on every scheduler loop. It stays importable:
`.airflowignore` governs DAG *discovery*, and Airflow puts `DAGS_FOLDER` on `sys.path`, so
`import common` resolves.

**No `Variable.get()` at parse time.** Variables are read inside task functions only —
a module-level `Variable.get()` is one metadata-database round trip per file per parse.

**Schedules are UTC.** The compose profile sets `TZ: Europe/Berlin`, which affects the
timestamps in container logs; it does not move the schedules, because every DAG's
`start_date` is `tzinfo=UTC` and the cron expression is interpreted in the DAG's timezone.
`0 4 3 * *` means 04:00 UTC, i.e. 05:00 or 06:00 local depending on the season.

**Connection defaults.** The `airflow` compose profile is separate from the application
profile, so the Airflow containers do not inherit `x-backend-env`. Without help, both
`autotwin_core.config.Settings` and `dbt/profiles.yml` would default to `localhost:5433`
— correct for a developer on the host, meaningless inside a container. Every generated
script therefore begins with `: "${AUTOTWIN_DB_HOST:=postgres}"`-style fallbacks
(`common.CONNECTION_DEFAULTS`). `:=` assigns only when the variable is unset, so anything
compose, `.env` or a custom image already exported still wins, and the fallback is
evaluated in the process that runs the task rather than in the scheduler that parsed the
file.

**`LocalExecutor` is assumed.** The gate tasks read the CLI's captured stdout from a file
under `/opt/airflow/scratch/<dag_id>/<ts_nodash>/`, which works because every task of a run
is a subprocess of the same container and shares its filesystem. Moving to Celery or
Kubernetes executors would need shared storage — or the payload pushed through XCom, which
is why the parsing lives in `common.read_cli_facts()` behind a single call site.

**No Cosmos, no `dbt-airflow` adapter.** A `BashOperator` and a `--select` selector is the
whole dbt integration. Model-level task generation would buy per-model retries at the cost
of a dependency that re-implements dbt's own DAG, and it would stop the same command from
being runnable by hand.

**Failure handling is a log line, not an alerting integration.** There is no SMTP server
and no Slack workspace in this project, and pretending otherwise would be decoration.
`common.log_task_failure` emits one grep-able `airflow_task_failed …` line per failure, in
the same shape as the application's structured logging. `email_on_failure` is off.

**Retries are exponential and modest.** Every failure mode these DAGs actually hit is
transient and external — a federal open-data server under load, a DNS blip, a Postgres
still replaying WAL. Failures that a retry cannot fix (a changed CSV schema, a fixture
answer, a model that did not improve) are raised as `AirflowFailException` from tasks with
`retries=0`, so they fail once and stay failed.

### Airflow Variables this directory owns

| Variable | Written by | Meaning |
|---|---|---|
| `autotwin_last_loaded_sha256__ingest_charging_infrastructure` | `record_source_fingerprint` | SHA-256 of the BNetzA file currently loaded, modelled and tested |
| `autotwin_active_model_version__energy_consumption` | `record_active_version` | Version the training DAG last promoted |

Both are recreated by the next successful run if deleted. Deleting the first forces a full
BNetzA reload; deleting the second makes the next training run compare against the best
metrics artefact on disk instead.

### Environment variables this directory reads

| Variable | Default | Purpose |
|---|---|---|
| `AUTOTWIN_REPO_ROOT` | `/opt/autotwin` | Where the checkout is mounted; `cwd` for every task |
| `AUTOTWIN_DATA_DIR` | `$AUTOTWIN_REPO_ROOT/data` | Must match what the CLI sees, or the DAG looks for the cached download in the wrong place |
| `AUTOTWIN_ML_MODEL_DIR` | `$AUTOTWIN_REPO_ROOT/models` | Where the promotion gate reads `*.metrics.json` |
| `AUTOTWIN_AIRFLOW_SCRATCH` | `$AIRFLOW_HOME/scratch` | Captured CLI output, per DAG run |
| `AUTOTWIN_PYTHON` / `AUTOTWIN_DBT` | `python` / `dbt` | Interpreter and dbt binary on `PATH` |

BUILD_SPEC §5 forbids reading `os.environ` outside `autotwin_core.config`. That rule binds
the workspace packages; these DAG files are outside the workspace by design, cannot import
`Settings` without dragging it into the parser loop, and need three paths to build a shell
command. They are read once, at import, into module constants in `common.py`.

---

## 7. CLI surface this layer depends on

From BUILD_SPEC §15, used verbatim:

```
python -m autotwin_ingestion.cli ingest charging [--mode …] [--dry-run] [--limit N]
python -m autotwin_ingestion.cli ingest weather  [--mode …] [--stations N]
python -m autotwin_ingestion.cli ingest traffic  [--mode …] [--roads A5,A8]
python -m autotwin_ingestion.cli quality report
python -m autotwin_simulator.cli generate-training-data [--trips N] [--out PATH]
python -m autotwin_ml.cli train    [--data PATH] [--version V] [--algorithm …]
python -m autotwin_ml.cli evaluate [--version V]
python -m autotwin_ml.cli explain  [--version V] [--out PATH]
```

Three things the service layer has to hold up its end of:

1. **`--log-level` and `--json-logs` are passed after the subcommand**, matching how §15
   writes every other flag. If argparse defines them only on the top-level parser they will
   be rejected; they need to be on the subparsers (or on both).
2. **The gates parse the JSON log stream** for `source_file_sha256`, `rows_received`,
   `rows_accepted`, `rows_rejected`, `provider_mode`, `error_message` and, for the quality
   report, `source` + `age_minutes` / `last_run_at`. Those names are not invented here —
   they are columns of `data_ingestion_runs` (§3.2) and fields of the freshness block
   (§7.1). Every gate degrades to a logged warning when a key is missing; none of them
   silently passes a check it could not perform.
3. **`python -m autotwin_ml.cli promote --version V` is not in §15.** Flipping
   `ml_models.is_active` is a database write, and these DAGs deliberately do not import
   `autotwin_core` to make one, so the promotion has to cross the same process boundary as
   everything else. Until `autotwin_ml.cli` implements `promote`, argparse exits 2 and the
   task fails loudly with the model unpromoted — the safe direction, and a visible reminder
   rather than a silent gap. It is the one place where this layer asks for CLI surface the
   specification has not yet written down.

One divergence worth flagging: the `Makefile` currently spells these targets
`ingest-charging` / `seed --demo`, while BUILD_SPEC §15 specifies `ingest charging` /
`seed demo`. **The DAGs follow the specification**, because §15 says these invocations are
the contract. If the CLI ends up implementing the hyphenated form, one edit in each of the
three ingestion DAGs fixes it — the arguments are passed positionally to
`common.autotwin_command()`.

---

## 8. Troubleshooting

| Symptom | Cause |
|---|---|
| `Can not find the cwd: /opt/autotwin` | The repository is not mounted — see §3 |
| `ModuleNotFoundError: autotwin_ingestion` | `requirements.txt` was not installed into the image |
| `dbt: command not found` | Same |
| dbt: `connection refused … localhost:5433` | Something exported `AUTOTWIN_DB_HOST` as `localhost`; inside the container it must be `postgres` |
| DAGs do not appear in the UI | Check the scheduler's import errors: `docker compose logs autotwin-airflow-scheduler` |
| Every run is `skipped` after `source_changed_gate` | Working as designed — the BNetzA file has not changed. Trigger with `{"force_reload": true}` |
| `train_energy_model` red at `evaluate_promotion` | Also working as designed — the new model did not improve. The active model keeps serving |
| Permission errors on `logs/` (Linux) | Set `AIRFLOW_UID=$(id -u)` in `.env` before `make airflow-up` |

---

## 9. What has been verified, and what has not

Verified against a real `apache-airflow==2.10.4` install (throwaway virtualenv, Python
3.12, official constraints file):

* `airflow dags list` finds all five DAGs; `airflow dags list-import-errors` reports none;
* a `DagBag` fill with `-W error::DeprecationWarning` produces an empty `import_errors`,
  so nothing here uses a construct Airflow 2.10 has deprecated;
* `airflow tasks list --tree` renders the expected graph for every DAG, including the
  short-circuit branch;
* `airflow tasks render` resolves the templated fields end to end — `params` defaults
  (`--limit '1'`, `--roads 'A1,A3,…'`), the `{{ ds_nodash }}` model version and training
  path, and the per-run scratch path handed to the TaskFlow gates through `op_args`;
* `ruff check` and `ruff format --check` pass under the repository's own configuration
  (line length 100, `ANN`/`S`/`PTH`/`SIM` rule sets);
* the generated shell scripts were rendered and syntax-checked with `bash -n`;
* the dbt invocations match the committed `dbt/dbt_project.yml` and `dbt/profiles.yml`
  (project name, profile name, env-var names, `analytics` target schema);
* the CLI invocations match BUILD_SPEC §15 verbatim.

**Not verified**, because it needs the full stack running:

* an end-to-end DAG run against a live PostGIS instance — no task has ever executed;
* that the ingestion CLI emits the JSON log keys the gates read. The gates degrade to a
  logged warning when a key is missing, but that path has not been exercised against a real
  implementation;
* that Airflow 2.10.4 and dbt-core 1.9 install into one image without a pip resolution
  conflict (see "Known integration risk" in §3). The verification above installed Airflow
  alone.
