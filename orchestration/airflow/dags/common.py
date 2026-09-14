"""Shared primitives for the AutoTwin DE Airflow DAGs.

Everything in this module exists so the DAG files stay thin and so the *one* place that
knows how to invoke AutoTwin is the *one* place that has to change when the CLI contract
(`docs/BUILD_SPEC.md` §15) moves.

Three rules govern this file, and they are deliberate:

1. **No AutoTwin imports, ever.** The scheduler re-parses every file in the DAG folder
   every ~30 s. Importing `autotwin_core` would drag SQLAlchemy models, Pydantic settings
   and a database URL into the parser loop — the classic way to make an Airflow deployment
   slow and fragile. The DAGs therefore shell out to the documented `python -m ...` CLI
   and read its output back as text. The coupling is a process boundary, not an import.
2. **No `Variable.get()` / DB access at parse time.** Variables are read *inside* task
   functions only. A `Variable.get()` at module level is one metadata-DB round trip per
   file per parse.
3. **`os.environ` is read here.** BUILD_SPEC §5 says never to read `os.environ` outside
   `autotwin_core.config` — that rule binds the workspace packages. These DAGs are outside
   the workspace by design (rule 1), cannot import `Settings`, and need three paths to
   build a shell command. They are read once, at import, into module constants.

Filesystem assumption: the executor is `LocalExecutor`, so every task of a DAG run
executes as a subprocess of the same scheduler container and shares its filesystem. The
CLI-output handoff (`capture_to` → `read_cli_facts`) depends on that. Moving to Celery or
Kubernetes executors would require shared storage or pushing the payload through XCom
instead; the README says so.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from airflow.models import Variable
from airflow.operators.bash import BashOperator

LOGGER = logging.getLogger("autotwin.airflow")

# --------------------------------------------------------------------------------------
# Identity & tags
# --------------------------------------------------------------------------------------

OWNER = "autotwin-data-platform"

TAG_INGESTION = "ingestion"
TAG_OFFICIAL = "official"
TAG_SIMULATED = "simulated"
TAG_QUALITY = "quality"
TAG_ML = "ml"
TAG_DBT = "dbt"

SOURCE_BNETZA = "bundesnetzagentur"
SOURCE_DWD = "dwd"
SOURCE_AUTOBAHN = "autobahn"

# --------------------------------------------------------------------------------------
# Paths — resolved once, at parse time
# --------------------------------------------------------------------------------------

#: Repository root *as mounted inside the Airflow containers*. The compose profile mounts
#: the checkout read-write here; see orchestration/airflow/README.md.
REPO_ROOT = Path(os.environ.get("AUTOTWIN_REPO_ROOT", "/opt/autotwin"))

#: Must match `AUTOTWIN_DATA_DIR` as the application sees it, otherwise the DAG would look
#: for the cached download in a different directory than the one the CLI wrote it to.
DATA_DIR = Path(os.environ.get("AUTOTWIN_DATA_DIR", str(REPO_ROOT / "data")))
RAW_DIR = DATA_DIR / "raw"
GOLD_DIR = DATA_DIR / "gold"

DBT_DIR = REPO_ROOT / "dbt"
MODEL_DIR = Path(os.environ.get("AUTOTWIN_ML_MODEL_DIR", str(REPO_ROOT / "models")))

AIRFLOW_HOME = Path(os.environ.get("AIRFLOW_HOME", "/opt/airflow"))

#: Scratch space for the captured stdout of the CLI steps. Deliberately *not* under the
#: repository: it is per-run throwaway state, and `data/` has its own gitignore rules.
SCRATCH_ROOT = Path(os.environ.get("AUTOTWIN_AIRFLOW_SCRATCH", str(AIRFLOW_HOME / "scratch")))

#: `python` resolves to the Airflow image's interpreter, which is where the workspace
#: packages are installed (see requirements.txt).
PYTHON_BIN = os.environ.get("AUTOTWIN_PYTHON", "python")

DBT_BIN = os.environ.get("AUTOTWIN_DBT", "dbt")

# --------------------------------------------------------------------------------------
# Freshness policy
# --------------------------------------------------------------------------------------

#: How stale a source may be before `data_quality_report` fails the run. Each threshold is
#: roughly three times the publication interval of the source, so a single missed run warns
#: through the report but does not page anybody; a broken pipeline does.
#:
#: * Bundesnetzagentur publishes one file per month and deletes the previous one
#:   (docs/data/sources.md §1) — 40 days means "we missed a whole edition".
#: * DWD 10-minute "now" products are refreshed continuously; the DAG runs hourly.
#: * Autobahn is polled every 15 minutes.
FRESHNESS_THRESHOLDS: dict[str, timedelta] = {
    SOURCE_BNETZA: timedelta(days=40),
    SOURCE_DWD: timedelta(hours=3),
    SOURCE_AUTOBAHN: timedelta(minutes=90),
}

# --------------------------------------------------------------------------------------
# Default args
# --------------------------------------------------------------------------------------


def log_task_failure(context: Mapping[str, Any]) -> None:
    """Emit one structured line per failed task.

    Not an alerting integration — there is no Slack workspace or SMTP server in this
    project and pretending otherwise would be decoration. What it does buy: a single
    grep-able line per failure in the scheduler log, in the same shape the application's
    structured logging uses.
    """
    task_instance = context.get("task_instance")
    exception = context.get("exception")
    LOGGER.error(
        "airflow_task_failed dag=%s task=%s run_id=%s try=%s exception=%r",
        getattr(task_instance, "dag_id", "?"),
        getattr(task_instance, "task_id", "?"),
        getattr(context.get("dag_run"), "run_id", "?"),
        getattr(task_instance, "try_number", "?"),
        exception,
    )


def default_args(**overrides: Any) -> dict[str, Any]:
    """Return a fresh `default_args` dict, optionally overridden per DAG.

    A function rather than a module-level dict on purpose: a shared mutable mapping handed
    to several DAGs is a trap — one DAG mutating it (or Airflow normalising a value in
    place) leaks into the others.

    Retries use exponential backoff because every failure mode these DAGs actually hit is
    transient and external: a federal open-data server that 503s under load, a DNS blip, a
    Postgres that is still replaying WAL. Retrying a broken CSV schema three times is
    useless, which is why schema failures are raised as `AirflowFailException` (no retry)
    in the validation tasks.
    """
    args: dict[str, Any] = {
        "owner": OWNER,
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=30),
        "execution_timeout": timedelta(minutes=30),
        "email_on_failure": False,
        "email_on_retry": False,
        "on_failure_callback": log_task_failure,
    }
    args.update(overrides)
    return args


# --------------------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------------------


def autotwin_command(
    module: str,
    *args: str,
    log_level: str = "INFO",
    json_logs: bool = True,
) -> str:
    """Build a `python -m <module> ...` invocation from the BUILD_SPEC §15 contract.

    `--json-logs` is on by default because the DAG-side gates parse the CLI's log stream;
    §15 guarantees every command accepts it.

    Arguments are shell-quoted. Jinja placeholders survive quoting (`'{{ params.x }}'`
    renders to `'A1,A2'`), so templated arguments are safe as long as the rendered value
    contains no single quote — which, for the parameters these DAGs expose, it cannot.
    """
    parts = [PYTHON_BIN, "-m", module, *args, "--log-level", log_level]
    if json_logs:
        parts.append("--json-logs")
    return " ".join(shlex.quote(part) for part in parts)


def dbt_command(subcommand: str, *args: str) -> str:
    """Build a dbt invocation against the committed project in `dbt/` (BUILD_SPEC §16).

    `--project-dir` and `--profiles-dir` both point at `dbt/` because `profiles.yml` is
    committed next to `dbt_project.yml` and reads `AUTOTWIN_DB_*` from the environment.
    No `--target` is passed: the committed profile decides, so the DAG cannot silently
    write to a different schema than a developer running dbt by hand.
    """
    parts = [
        DBT_BIN,
        "--no-use-colors",
        subcommand,
        "--project-dir",
        str(DBT_DIR),
        "--profiles-dir",
        str(DBT_DIR),
        *args,
    ]
    return " ".join(shlex.quote(part) for part in parts)


def dbt_deps_if_needed() -> str:
    """`dbt deps`, but only when the project actually declares packages."""
    guard = shlex.quote(str(DBT_DIR / "packages.yml"))
    return f"if [ -f {guard} ]; then {dbt_command('deps')}; fi"


def dbt_seed() -> str:
    """`dbt seed` — required, not decoration.

    `dbt/seeds/bundesland_reference.csv` is the target of a `relationships` test on
    `stg_charging_stations.bundesland`. If the seed relation has never been materialised,
    that test does not fail — it *errors* with "relation does not exist", which reads like
    a broken warehouse rather than a missing `dbt seed`. Sixteen rows cost nothing to
    reload, so every step that is about to run or test the charging lineage seeds first.
    """
    return dbt_command("seed")


def task_env() -> dict[str, str]:
    """Environment overlay for every shell step.

    These are the variables the DAG layer genuinely owns: where the CLI writes artefacts
    and how it logs. Merged onto the container environment (`append_env=True`).

    Connection settings are deliberately *not* here — see `CONNECTION_DEFAULTS`. An entry
    in this dict overrides whatever compose exported, which is the wrong precedence for a
    hostname or a database URL.
    """
    return {
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "AUTOTWIN_DATA_DIR": str(DATA_DIR),
        "AUTOTWIN_ML_MODEL_DIR": str(MODEL_DIR),
        # The CLI's own structured logging, matching `--json-logs` on the command line.
        "AUTOTWIN_LOG_FORMAT": "json",
    }


#: In-network connection settings, applied **only when the variable is not already set**.
#:
#: Why this exists: the `airflow` compose profile is separate from the application
#: profile, so the Airflow containers do not inherit `x-backend-env`. Without these, the
#: defaults win — `autotwin_core.config.Settings` defaults `DATABASE_URL` to
#: `localhost:5433` (BUILD_SPEC §5) and `dbt/profiles.yml` defaults to `localhost:5433`
#: too, because both are written for a developer on the host. Inside a container,
#: `localhost` is the container itself and every task fails with a connection refused that
#: looks like a database outage.
#:
#: Applied in the shell with `${VAR:=default}` rather than in `env=`, so the precedence is
#: right in both directions: anything compose, `.env` or an Airflow Connection already
#: exported wins, and the default only fills a hole. It is also evaluated in the process
#: that actually runs the task, not in the scheduler that parsed the file.
#:
#: The literal credential is the local development one — the same `autotwin/autotwin`
#: already in plain text in `docker-compose.yml`, `.env.example` and `dbt/profiles.yml`.
#: `AUTOTWIN_DB_USER` / `_PASSWORD` / `_NAME` are not repeated here: `profiles.yml`
#: already defaults them correctly, and one copy of a credential is better than four.
CONNECTION_DEFAULTS: dict[str, str] = {
    "AUTOTWIN_DATABASE_URL": "postgresql+psycopg://autotwin:autotwin@postgres:5432/autotwin",
    "AUTOTWIN_DB_HOST": "postgres",
    "AUTOTWIN_DB_PORT": "5432",
    "AUTOTWIN_KAFKA_BOOTSTRAP_SERVERS": "redpanda:9092",
}


def scratch_file(name: str) -> str:
    """A Jinja-templated scratch path, unique per DAG and logical date.

    Returned as a template string (not a resolved path) so the same constant can be handed
    to a `BashOperator`'s `bash_command` and to a TaskFlow function's keyword argument —
    `PythonOperator.template_fields` includes `op_kwargs`, so both are rendered.
    """
    return f"{SCRATCH_ROOT}/{{{{ dag.dag_id }}}}/{{{{ ts_nodash }}}}/{name}"


#: Characters that would change the meaning of a `"${VAR:=default}"` expansion. None of the
#: defaults above contains one; the check exists so that a future edit fails at parse time
#: instead of producing a subtly wrong shell command.
_UNSAFE_IN_DOUBLE_QUOTES = ('"', "$", "`", "\\", "\n")


def _preamble() -> list[str]:
    """Shell prologue shared by every step: strict mode plus connection fallbacks.

    The defaults are emitted as `: "${VAR:=default}"` — double quotes, not `shlex.quote`,
    because single quotes would suppress the parameter expansion that is the entire point.
    """
    lines = [
        "set -euo pipefail",
        "",
        "# Connection fallbacks — see CONNECTION_DEFAULTS in common.py. `:=` assigns only",
        "# when the variable is unset, so anything compose or .env exported still wins.",
    ]
    for name, value in CONNECTION_DEFAULTS.items():
        if any(char in value for char in _UNSAFE_IN_DOUBLE_QUOTES):
            raise ValueError(f"CONNECTION_DEFAULTS[{name!r}] is not safe to interpolate: {value!r}")
        lines.append(f': "${{{name}:={value}}}"')
    lines.append(f"export {' '.join(CONNECTION_DEFAULTS)}")
    lines.append("")
    return lines


def _script(commands: Sequence[str], capture_to: str | None) -> str:
    lines = _preamble()
    if capture_to is None:
        lines.extend(commands)
        return "\n".join(lines) + "\n"

    quoted = shlex.quote(capture_to)
    lines.append(f'mkdir -p "$(dirname {quoted})"')
    lines.append("{")
    lines.extend(f"  {command}" for command in commands)
    # `pipefail` is set, so the exit status of the group survives the pipe into tee.
    lines.append(f"}} 2>&1 | tee {quoted}")
    return "\n".join(lines) + "\n"


def bash_step(
    task_id: str,
    commands: Sequence[str],
    *,
    capture_to: str | None = None,
    doc: str | None = None,
    **kwargs: Any,
) -> BashOperator:
    """A `BashOperator` wired to the repository root with the AutoTwin environment.

    `cwd` is the mounted repository: dbt resolves relative paths from there, and if the
    mount is missing the task fails immediately with a comprehensible error instead of
    producing a confusing `ModuleNotFoundError` later.

    When `capture_to` is given the combined output is tee'd to that path *and* still shown
    in the Airflow task log, so a downstream Python gate can read the CLI's structured
    output without anybody losing the ability to read it in the UI.
    """
    return BashOperator(
        task_id=task_id,
        bash_command=_script(commands, capture_to),
        cwd=str(REPO_ROOT),
        env=task_env(),
        append_env=True,
        doc_md=doc,
        **kwargs,
    )


# --------------------------------------------------------------------------------------
# Reading the CLI back
# --------------------------------------------------------------------------------------

#: Keys the DAG gates look for in the CLI's JSON log stream. The names are not invented
#: here: `source_file_sha256`, `rows_received`, `rows_accepted`, `rows_rejected`,
#: `rows_duplicate` and `provider_mode` are columns of `data_ingestion_runs`
#: (BUILD_SPEC §3.2); `source`, `last_run_at`, `age_minutes`, `status` and `mode` are the
#: fields of the freshness block in §7.1. The CLI writes that row, so it is the natural
#: vocabulary for it to log in.
FACT_KEYS = frozenset(
    {
        "source",
        "pipeline",
        "provider_mode",
        "mode",
        "status",
        "source_url",
        "source_file_sha256",
        "rows_received",
        "rows_accepted",
        "rows_rejected",
        "rows_duplicate",
        "bytes_downloaded",
        "error_message",
        "last_run_at",
        "finished_at",
        "age_minutes",
        "version",
        "mae",
        "rmse",
        "r2",
        "baseline_mae",
    }
)


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict nested anywhere inside a decoded JSON value."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def iter_log_objects(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield every JSON object found in a captured CLI log, nested ones included.

    Tolerant by design. §15 allows a command to end with a human-readable summary, and
    structured loggers differ in whether they nest their payload under `event`, `extra` or
    the top level. Lines that are not JSON are skipped rather than treated as an error.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return
    with file_path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield from _walk(decoded)


def read_cli_facts(path: str | Path) -> dict[str, Any]:
    """Flatten a captured CLI log into the last value seen for each interesting key.

    Last-wins: a pipeline logs `rows_received` when it starts parsing and again in its
    closing summary, and the closing summary is the authoritative one.
    """
    facts: dict[str, Any] = {}
    for obj in iter_log_objects(path):
        for key, value in obj.items():
            if key in FACT_KEYS and value is not None:
                facts[key] = value
    return facts


def collect_source_records(path: str | Path) -> list[dict[str, Any]]:
    """Extract per-source freshness records from a captured `quality report` log.

    A record is any JSON object that names a `source` *and* carries something that can be
    turned into an age. Duplicates are collapsed on `source`, last one winning.
    """
    records: dict[str, dict[str, Any]] = {}
    age_keys = ("age_minutes", "last_run_at", "finished_at", "last_success_at")
    for obj in iter_log_objects(path):
        source = obj.get("source")
        if not isinstance(source, str):
            continue
        if not any(obj.get(key) is not None for key in age_keys):
            continue
        records[source] = {key: value for key, value in obj.items() if not isinstance(value, dict)}
    return list(records.values())


def age_of(record: Mapping[str, Any], *, now: datetime) -> timedelta | None:
    """Best-effort age of a freshness record.

    Prefers an explicit `age_minutes` (that is what §7.1 puts on the wire); falls back to
    the newest timestamp the record carries. Returns `None` when neither is usable, and
    the caller decides what an unknown age means.
    """
    minutes = record.get("age_minutes")
    if isinstance(minutes, int | float):
        return timedelta(minutes=float(minutes))

    for key in ("last_run_at", "last_success_at", "finished_at"):
        value = record.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            continue
        return now - parsed
    return None


# --------------------------------------------------------------------------------------
# Raw artefacts & fingerprints
# --------------------------------------------------------------------------------------


def sha256_of_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Streaming SHA-256 — the BNetzA export is tens of megabytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def newest_raw_artefact(patterns: Iterable[str], *, root: Path | None = None) -> Path | None:
    """Newest file under `data/raw/` whose *name* matches any of `patterns` (regex).

    The ingestion layer owns the cache layout (`data/raw/` + TTL, BUILD_SPEC §4), so the
    DAG matches on the source's own vocabulary instead of hard-coding a filename it does
    not control. Returns `None` when nothing matches — every caller treats that as
    "unknown", never as "empty".
    """
    directory = RAW_DIR if root is None else root
    if not directory.is_dir():
        return None
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    candidates = [
        path
        for path in directory.rglob("*")
        if path.is_file() and any(rx.search(path.name) for rx in compiled)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


# --------------------------------------------------------------------------------------
# ML artefacts
# --------------------------------------------------------------------------------------

#: Metrics keys the promotion gate needs. BUILD_SPEC §3.2 defines `ml_models.metrics` as
#: `{mae, rmse, r2, baseline_mae, baseline_rmse, baseline_r2}`, and §10.2 says the same
#: object is written next to the artefact as `energy_consumption_v{N}.metrics.json`.
METRIC_KEYS = ("mae", "rmse", "r2", "baseline_mae", "baseline_rmse", "baseline_r2")


def find_metrics_file(version: str, *, model_name: str) -> Path | None:
    """Locate the metrics JSON for one trained version.

    Matches on *containment* rather than an exact filename: §10.2 documents
    `models/energy_consumption_v{N}.metrics.json`, and the version the DAG passes on the
    command line (`--version 20260315`) is the `{N}` in that name. Being tolerant here
    means a change in how the ML CLI decorates the version does not silently break the
    promotion gate — it will still find the file or say plainly that it did not.
    """
    if not MODEL_DIR.is_dir():
        return None
    candidates = [
        path for path in MODEL_DIR.glob(f"{model_name}*.metrics.json") if version in path.name
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def iter_metrics_files(*, model_name: str) -> list[Path]:
    """Every metrics JSON belonging to `model_name`, newest last."""
    if not MODEL_DIR.is_dir():
        return []
    return sorted(MODEL_DIR.glob(f"{model_name}*.metrics.json"), key=lambda p: p.stat().st_mtime)


def extract_metrics(path: Path) -> dict[str, float]:
    """Pull the numeric metrics out of a metrics JSON, wherever they are nested.

    Accepts both a flat `{"mae": ...}` document and one that wraps the numbers under a
    `metrics` key, because §3.2 describes the column and §10.2 describes the file without
    fixing the file's outer shape.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read metrics file %s: %s", path, exc)
        return {}

    for candidate in _walk(payload):
        if "mae" in candidate:
            return {
                key: float(candidate[key])
                for key in METRIC_KEYS
                if isinstance(candidate.get(key), int | float)
            }
    return {}


def active_model_variable(model_name: str) -> str:
    """Name of the Airflow Variable holding the version this pipeline last promoted."""
    return f"autotwin_active_model_version__{model_name}"


def get_active_model_version(model_name: str) -> str | None:
    """Version last promoted by the training DAG, or `None` on a cold start."""
    value = Variable.get(active_model_variable(model_name), default_var=None)
    return value if isinstance(value, str) and value else None


def set_active_model_version(model_name: str, version: str) -> None:
    """Record the version this pipeline promoted."""
    Variable.set(active_model_variable(model_name), version)


# --------------------------------------------------------------------------------------
# Ingestion fingerprints
# --------------------------------------------------------------------------------------


def fingerprint_variable(dag_id: str) -> str:
    """Name of the Airflow Variable holding the last successfully loaded fingerprint."""
    return f"autotwin_last_loaded_sha256__{dag_id}"


def get_last_fingerprint(dag_id: str) -> str | None:
    """Read the fingerprint stored by the last fully successful run of `dag_id`.

    An Airflow Variable rather than an XCom: XComs are scoped to a DAG run, and the
    question here is explicitly cross-run ("has the file changed since the last run that
    *completed*"). `include_prior_dates=True` on an XCom pull would answer a subtly
    different question — it would find the value of the last run that reached the task,
    including runs that failed further downstream.
    """
    value = Variable.get(fingerprint_variable(dag_id), default_var=None)
    return value if isinstance(value, str) and value else None


def set_last_fingerprint(dag_id: str, fingerprint: str) -> None:
    """Record the fingerprint of the payload that is now in the database."""
    Variable.set(fingerprint_variable(dag_id), fingerprint)
