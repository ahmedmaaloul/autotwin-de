"""Weekly retraining of the energy-consumption model — on **simulated** labels.

> **The labels in this pipeline are simulated.** The training set is aggregated windows of
> telemetry produced by `autotwin_simulator`, whose vehicles are integrated from a
> longitudinal road-load model (BUILD_SPEC §10.1), not measured from real cars. Every row
> carries `data_origin = simulated`, the registry row records
> `training_data_origin = simulated`, and the UI labels anything derived from it
> `SIMULIERT`. What this model demonstrates is a complete, honest ML lifecycle — feature
> contract, grouped split, a physical baseline to beat, SHAP attribution, a promotion gate
> — evaluated on data whose generating process is known. It is **not** evidence about the
> consumption of any real vehicle, and no claim in this repository says otherwise.

**Why weekly, `0 3 * * 1`.** The simulator is the only thing that produces training rows,
and its output changes when the *code* changes, not when the clock moves. A weekly cadence
keeps the registry, the metrics artefacts and the SHAP explanations in step with the
repository without spending an hour of CPU every night to re-learn the same function.
03:00 on Monday keeps it away from the hourly and quarter-hourly ingestion DAGs.

## Pipeline

```
generate_training_data   autotwin_simulator.cli generate-training-data --trips N --out P
train_energy_model       autotwin_ml.cli train    --data P --version V --algorithm A
evaluate_energy_model    autotwin_ml.cli evaluate --version V
explain_energy_model     autotwin_ml.cli explain  --version V --out PATH
evaluate_promotion       DAG-side gate: MAE vs the active version *and* vs the physical baseline
promote_energy_model     python -m autotwin_ml.cli promote  --version V
```

`V` is `{{ ds_nodash }}` — the logical date of the run, e.g. `20260316`. Deriving the
version from the schedule rather than an auto-incrementing counter makes the whole chain
deterministic: `train`, `evaluate`, `explain` and `promote` all address the same artefact
without the DAG having to scrape a version number out of a log, and re-running a week
overwrites that week's artefacts instead of accumulating phantom versions.

## The promotion gate

`evaluate_promotion` reads `models/energy_consumption_v{V}.metrics.json` (§10.2) and
refuses to promote unless **both** hold:

1. **It beats the physical baseline.** `mae < baseline_mae`. A gradient-boosted model that
   cannot beat a road-load equation on the same test split has learned nothing worth
   serving, and §10.2 exists precisely so that comparison is always on the table.
2. **It improves on the currently active version.** `mae < active_mae * (1 - 0.001)`. The
   0.1 % margin is a no-churn band, not a significance test: it stops a numerically
   identical retrain from cycling the active model.

If either fails the task raises `AirflowFailException` — **the run goes red, and the
previously active model keeps serving**. That is the point of the gate. A pipeline that
silently promotes a worse model is worse than no pipeline, and a pipeline that silently
*keeps* a worse model while reporting success is worse still.

"Currently active" is resolved from the Airflow Variable
`autotwin_active_model_version__energy_consumption`, which this DAG writes after a
successful promotion; on a cold start it falls back to the best MAE among the metrics files
already in `models/`, and if there are none at all the first model is promoted unopposed
(logged as such).

## One dependency outside BUILD_SPEC §15

`promote_energy_model` calls `python -m autotwin_ml.cli promote --version V`. **§15 lists
`train`, `evaluate` and `explain` — it does not list `promote`.** Flipping
`ml_models.is_active` is a database write, and these DAG files deliberately do not import
`autotwin_core` to make one (see `common.py`), so the promotion has to cross the same
process boundary as everything else. If `autotwin_ml.cli` has not implemented `promote`,
argparse exits 2 and this task fails loudly with the model unpromoted — the safe
direction, and a visible reminder rather than a silent gap. It is the one place where this
orchestration layer asks for CLI surface the spec has not yet written down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import common
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.models.param import Param

DAG_ID = "train_energy_model"

#: BUILD_SPEC §5: `ML_ACTIVE_MODEL=energy_consumption`. Artefacts are
#: `models/energy_consumption_v{N}.joblib` / `.metrics.json` (§10.2).
MODEL_NAME = "energy_consumption"

#: Relative MAE improvement required to promote. Not a significance test — a no-churn band
#: that keeps a numerically identical retrain from cycling the active model.
MIN_RELATIVE_IMPROVEMENT = 0.001

TRAINING_DATA = f"{common.GOLD_DIR}/energy_training_{{{{ ds_nodash }}}}.parquet"
SHAP_OUTPUT = f"{common.GOLD_DIR}/shap_{MODEL_NAME}_v{{{{ ds_nodash }}}}.json"
VERSION = "{{ ds_nodash }}"

TRAIN_LOG = common.scratch_file("train.jsonl")
EVALUATE_LOG = common.scratch_file("evaluate.jsonl")


@dag(
    dag_id=DAG_ID,
    description="Weekly retrain of the energy model on simulated telemetry, with a promotion gate",
    schedule="0 3 * * 1",
    start_date=datetime(2026, 1, 5, tzinfo=UTC),
    catchup=False,
    max_active_runs=1,
    dagrun_timeout=timedelta(hours=6),
    default_args=common.default_args(
        # Training is expensive and deterministic: if it failed, it will fail again the same
        # way. One retry covers a transient database or filesystem problem; two would just
        # burn CPU.
        retries=1,
        retry_delay=timedelta(minutes=10),
        execution_timeout=timedelta(hours=1),
    ),
    tags=[common.TAG_ML, common.TAG_SIMULATED],
    doc_md=__doc__,
    params={
        "trips": Param(
            600,
            type="integer",
            minimum=50,
            title="Simulated trips",
            description="Trips to synthesise for the training set. Grouped-split by trip_id.",
        ),
        "algorithm": Param(
            "lightgbm",
            type="string",
            enum=["lightgbm", "sklearn"],
            title="Algorithm",
            description="`sklearn` falls back to HistGradientBoostingRegressor (BUILD_SPEC §10.2).",
        ),
    },
)
def train_energy_model() -> None:
    """Assemble the weekly training pipeline."""

    generate = common.bash_step(
        task_id="generate_training_data",
        commands=[
            f'mkdir -p "{common.GOLD_DIR}"',
            common.autotwin_command(
                "autotwin_simulator.cli",
                "generate-training-data",
                "--trips",
                "{{ params.trips }}",
                "--out",
                TRAINING_DATA,
            ),
        ],
        execution_timeout=timedelta(minutes=45),
        doc=(
            "Runs the physics simulator headless and aggregates its telemetry into feature "
            "windows (BUILD_SPEC §10.2 `FEATURE_NAMES`). **Simulated labels** — the target "
            "`energy_consumption_kwh_100km` comes from the road-load model, not a vehicle."
        ),
    )

    train = common.bash_step(
        task_id="train_energy_model",
        commands=[
            common.autotwin_command(
                "autotwin_ml.cli",
                "train",
                "--data",
                TRAINING_DATA,
                "--version",
                VERSION,
                "--algorithm",
                "{{ params.algorithm }}",
            )
        ],
        capture_to=TRAIN_LOG,
        execution_timeout=timedelta(hours=1),
        doc=(
            "Grouped 70/15/15 split on `trip_id` with a fixed seed, writing "
            "`models/energy_consumption_v{{ ds_nodash }}.joblib` and its metrics JSON, and "
            "registering the version in `ml_models` with "
            "`training_data_origin = simulated`."
        ),
    )

    evaluate = common.bash_step(
        task_id="evaluate_energy_model",
        commands=[
            common.autotwin_command("autotwin_ml.cli", "evaluate", "--version", VERSION),
        ],
        capture_to=EVALUATE_LOG,
        execution_timeout=timedelta(minutes=30),
        doc=(
            "Scores the held-out test split for both the trained model and the physical "
            "baseline (§10.1) and writes MAE / RMSE / R² for each into the metrics artefact. "
            "The baseline numbers are what the promotion gate compares against."
        ),
    )

    explain = common.bash_step(
        task_id="explain_energy_model",
        commands=[
            f'mkdir -p "{common.GOLD_DIR}"',
            common.autotwin_command(
                "autotwin_ml.cli",
                "explain",
                "--version",
                VERSION,
                "--out",
                SHAP_OUTPUT,
            ),
        ],
        execution_timeout=timedelta(minutes=30),
        doc=(
            "SHAP `TreeExplainer` global attribution, written to `data/gold/` rather than "
            "`models/` so the committed artefact directory stays small. Feeds "
            "`GET /api/v1/ml/explain/global` and the deterministic insight generator (§11)."
        ),
    )

    @task(task_id="evaluate_promotion", retries=0)
    def evaluate_promotion(version: str) -> dict[str, Any]:
        """Decide whether this week's model may replace the active one.

        Fails loudly rather than skipping: a model that did not improve is a result worth
        seeing in red, and an Airflow "skipped" would make a regression look like a
        no-op.
        """
        metrics_path = common.find_metrics_file(version, model_name=MODEL_NAME)
        if metrics_path is None:
            raise AirflowFailException(
                f"No metrics artefact for version {version} under {common.MODEL_DIR}. "
                f"Expected {MODEL_NAME}_v{version}.metrics.json (BUILD_SPEC §10.2) — "
                "training reported success but produced nothing to evaluate."
            )

        candidate = common.extract_metrics(metrics_path)
        candidate_mae = candidate.get("mae")
        baseline_mae = candidate.get("baseline_mae")
        if candidate_mae is None:
            raise AirflowFailException(
                f"{metrics_path} carries no `mae`. The promotion gate cannot compare models "
                "without it; see BUILD_SPEC §3.2 for the expected metrics object."
            )

        if baseline_mae is None:
            common.LOGGER.warning(
                "%s carries no `baseline_mae`; the physical-baseline gate is skipped. "
                "BUILD_SPEC §10.2 requires both models to be scored on the same test split.",
                metrics_path,
            )
        elif candidate_mae >= baseline_mae:
            raise AirflowFailException(
                f"Model v{version} MAE {candidate_mae:.4f} does not beat the physical "
                f"baseline MAE {baseline_mae:.4f}. Refusing to promote: a learned model that "
                "loses to the road-load equation has nothing to add to the API."
            )

        active_version = common.get_active_model_version(MODEL_NAME)
        active_mae: float | None = None

        if active_version is not None and active_version != version:
            active_path = common.find_metrics_file(active_version, model_name=MODEL_NAME)
            if active_path is not None:
                active_mae = common.extract_metrics(active_path).get("mae")

        if active_mae is None:
            # Cold start, or the recorded active version has no readable metrics file.
            # Fall back to the best MAE among every other artefact in models/.
            others = [
                (path, common.extract_metrics(path).get("mae"))
                for path in common.iter_metrics_files(model_name=MODEL_NAME)
                if version not in path.name
            ]
            scored = [(path, mae) for path, mae in others if mae is not None]
            if scored:
                best_path, best_mae = min(scored, key=lambda item: item[1])
                active_version, active_mae = best_path.name, best_mae
                common.LOGGER.info(
                    "No promoted version on record; comparing against the best existing "
                    "artefact %s (MAE %.4f).",
                    best_path.name,
                    best_mae,
                )

        if active_mae is None:
            common.LOGGER.info(
                "No previous model to compare against — promoting v%s unopposed "
                "(MAE %.4f, baseline %s).",
                version,
                candidate_mae,
                f"{baseline_mae:.4f}" if baseline_mae is not None else "n/a",
            )
        else:
            required = active_mae * (1.0 - MIN_RELATIVE_IMPROVEMENT)
            if candidate_mae >= required:
                raise AirflowFailException(
                    f"Model v{version} MAE {candidate_mae:.4f} does not improve on the active "
                    f"model ({active_version}, MAE {active_mae:.4f}); at least "
                    f"{required:.4f} was required. Not promoting — the active model keeps "
                    "serving and this run is red on purpose."
                )
            common.LOGGER.info(
                "Promoting v%s: MAE %.4f vs active %s MAE %.4f (%.2f %% better).",
                version,
                candidate_mae,
                active_version,
                active_mae,
                100.0 * (active_mae - candidate_mae) / active_mae,
            )

        return {
            "version": version,
            "metrics_path": str(metrics_path),
            "mae": candidate_mae,
            "baseline_mae": baseline_mae,
            "previous_version": active_version,
            "previous_mae": active_mae,
        }

    promote = common.bash_step(
        task_id="promote_energy_model",
        commands=[
            common.autotwin_command("autotwin_ml.cli", "promote", "--version", VERSION),
        ],
        execution_timeout=timedelta(minutes=10),
        doc=(
            "Flips `ml_models.is_active` to this version. **Not in BUILD_SPEC §15** — see "
            "the DAG documentation. If the subcommand does not exist, argparse exits 2 and "
            "the previously active model keeps serving."
        ),
    )

    @task(task_id="record_active_version", retries=0)
    def record_active_version(decision: dict[str, Any]) -> None:
        """Remember what was promoted, so next week's gate has something to beat."""
        version = str(decision["version"])
        common.set_active_model_version(MODEL_NAME, version)
        common.LOGGER.info(
            "Active %s version is now v%s (MAE %s, previous %s). Labels: SIMULATED.",
            MODEL_NAME,
            version,
            decision.get("mae"),
            decision.get("previous_version") or "<none>",
        )

    decision = evaluate_promotion(VERSION)
    recorded = record_active_version(decision)

    generate >> train >> evaluate >> explain >> decision >> promote >> recorded


train_energy_model()
