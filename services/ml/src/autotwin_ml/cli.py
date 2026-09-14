"""``python -m autotwin_ml.cli`` — the ML service's command-line contract.

BUILD_SPEC §15 fixes three commands:

.. code-block:: text

    python -m autotwin_ml.cli train    [--data PATH] [--version V] [--algorithm lightgbm|sklearn]
    python -m autotwin_ml.cli evaluate [--version V]
    python -m autotwin_ml.cli explain  [--version V] [--out PATH]

A fourth, ``generate-data``, is added rather than substituted: the three above keep their exact
spelling and behaviour, and the new one exists because training needs a dataset and the
simulator that produces the real one may not have run yet. It says loudly, in its help text and
in the file it writes, that its output is a physics sweep and not simulated telemetry — see
``docs/ml/methodology.md``.

``argparse`` per §15. Every command accepts ``--log-level`` and ``--json-logs``, exits ``0`` on
success, ``1`` on a handled failure and ``2`` on bad arguments, and ends with a human-readable
summary on **stdout** while the structured log goes to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final
from uuid import UUID

import numpy as np

from autotwin_core.config import LogFormat, get_settings
from autotwin_core.db.session import dispose_engine, session_scope
from autotwin_core.errors import AutoTwinError
from autotwin_core.logging import configure_logging, get_logger
from autotwin_ml.dataset import (
    DEFAULT_TRAINING_PATH,
    describe_training_source,
    generate_physics_sweep,
    load_dataset,
)
from autotwin_ml.explain import GlobalShapReport, get_explainer
from autotwin_ml.features import feature_matrix
from autotwin_ml.registry import ModelRecord, load_model, register_model, resolve_model
from autotwin_ml.training import (
    TrainingConfig,
    baseline_predictions,
    evaluate_regression,
    train_model,
)

__all__ = ["build_parser", "main"]

_LOGGER = get_logger(__name__)

EXIT_OK: Final[int] = 0
"""The command did what it was asked to do."""

EXIT_FAILURE: Final[int] = 1
"""A handled failure: no dataset, no trained model, the database refused the registry row."""

EXIT_USAGE: Final[int] = 2
"""Bad arguments. argparse exits with this on its own; the constant documents the contract."""

_DEFAULT_TOP_FEATURES: Final[int] = 10


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    A function rather than a module-level constant so tests can build a fresh parser and so
    importing this module has no side effect beyond defining names.
    """
    parser = argparse.ArgumentParser(
        prog="python -m autotwin_ml.cli",
        description=(
            "AutoTwin DE energy model: train the gradient-boosted regressor, score it against "
            "the physical baseline, and read its SHAP attribution."
        ),
    )
    _add_common_flags(parser)
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    train = subparsers.add_parser(
        "train",
        help="train a new model version and score it against the physical baseline",
        description=(
            "Load the training Parquet, split it grouped by trip_id (70/15/15), fit a LightGBM "
            "regressor with early stopping on the validation fold, and evaluate both the model "
            "and the BUILD_SPEC §10.1 physical baseline on the untouched test fold. Writes "
            "models/<name>_v<N>.joblib and <name>_v<N>.metrics.json, activates the version and "
            "inserts an ml_models row."
        ),
    )
    _add_common_flags(train)
    train.add_argument(
        "--data",
        type=Path,
        metavar="PATH",
        help=f"training Parquet (default: {DEFAULT_TRAINING_PATH})",
    )
    train.add_argument(
        "--version",
        metavar="V",
        help="version to write; default is the next unused integer",
    )
    train.add_argument(
        "--algorithm",
        choices=("lightgbm", "sklearn", "auto"),
        default="auto",
        help=(
            "estimator backend. 'auto' (default) prefers LightGBM and falls back to "
            "scikit-learn's HistGradientBoostingRegressor when LightGBM cannot be imported"
        ),
    )
    train.add_argument(
        "--seed",
        type=int,
        metavar="S",
        help="master seed for the split and the estimator (default: AUTOTWIN_SIM_SEED)",
    )
    train.add_argument(
        "--no-activate",
        action="store_true",
        help="write the artefact but leave the previously active version serving",
    )
    train.add_argument(
        "--no-register",
        action="store_true",
        help="skip the ml_models row; use when no database is reachable",
    )

    evaluate = subparsers.add_parser(
        "evaluate",
        help="re-score a stored model version on the test fold, beside the physical baseline",
        description=(
            "Reproduces the grouped split with the seed the version was trained with, then "
            "scores the stored artefact and the physical model on the same held-out rows. Use "
            "it to check a committed artefact against a regenerated dataset."
        ),
    )
    _add_common_flags(evaluate)
    evaluate.add_argument("--version", metavar="V", help="model version (default: the active one)")
    evaluate.add_argument("--data", type=Path, metavar="PATH", help="training Parquet to score on")
    evaluate.add_argument("--seed", type=int, metavar="S", help="split seed to reproduce")

    explain = subparsers.add_parser(
        "explain",
        help="print the global SHAP feature importance of a model version",
        description=(
            "Reads the mean |SHAP| ranking stored in the version's metrics file, or recomputes "
            "it from the test fold with --recompute. Optionally writes the full report as JSON."
        ),
    )
    _add_common_flags(explain)
    explain.add_argument("--version", metavar="V", help="model version (default: the active one)")
    explain.add_argument("--out", type=Path, metavar="PATH", help="write the report as JSON here")
    explain.add_argument(
        "--top",
        type=_positive_int,
        default=_DEFAULT_TOP_FEATURES,
        metavar="N",
        help=f"features to print (default: {_DEFAULT_TOP_FEATURES})",
    )
    explain.add_argument(
        "--recompute",
        action="store_true",
        help="recompute the ranking from the dataset instead of reading the metrics file",
    )
    explain.add_argument("--data", type=Path, metavar="PATH", help="dataset used by --recompute")
    explain.add_argument("--seed", type=int, metavar="S", help="split seed used by --recompute")

    generate = subparsers.add_parser(
        "generate-data",
        help="write a physics-sweep training set (stand-in for simulator output)",
        description=(
            "Generate data/gold/training/energy_windows.parquet from a seeded sweep over the "
            "physical energy model. This is NOT simulated telemetry: it exists so the ML "
            "pipeline can be trained before the simulator has run. Prefer "
            "`python -m autotwin_simulator.cli generate-training-data` when it is available. "
            "A sidecar records the provenance and the trainer carries it into the model "
            "registry, so a model trained on a sweep is never reported as trained on "
            "simulator output."
        ),
    )
    _add_common_flags(generate)
    generate.add_argument(
        "--trips",
        type=_positive_int,
        default=400,
        metavar="N",
        help="synthetic journeys to generate (default: 400, roughly 22 000 windows)",
    )
    generate.add_argument("--seed", type=int, metavar="S", help="master seed for the sweep")
    generate.add_argument(
        "--out",
        type=Path,
        metavar="PATH",
        help=f"destination Parquet (default: {DEFAULT_TRAINING_PATH})",
    )

    return parser


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    """Attach ``--log-level`` and ``--json-logs`` (BUILD_SPEC §15).

    Added to the top-level parser *and* to each subparser so that both ``cli --json-logs train``
    and ``cli train --json-logs`` work: operators reach for the second spelling and shell
    history produces the first.
    """
    parser.add_argument(
        "--log-level",
        default=None,
        metavar="LEVEL",
        help="log level name, e.g. DEBUG or INFO (default: AUTOTWIN_LOG_LEVEL)",
    )
    parser.add_argument(
        "--json-logs",
        action="store_true",
        default=None,
        help="render logs as one JSON object per line instead of console output",
    )


def _positive_int(raw: str) -> int:
    """Parse a strictly positive integer, or raise the error argparse turns into exit code 2."""
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{raw!r} is not an integer"
        raise argparse.ArgumentTypeError(msg) from exc
    if value <= 0:
        msg = f"must be greater than zero, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _configure(namespace: argparse.Namespace) -> None:
    """Apply the logging flags on top of the configured settings.

    ``Settings`` is copied rather than mutated so that ``get_settings()`` keeps describing the
    environment for every other module in the process and only the log rendering follows the
    flag.
    """
    settings = get_settings()
    overrides: dict[str, object] = {}
    if namespace.log_level:
        overrides["log_level"] = str(namespace.log_level).upper()
    if namespace.json_logs:
        overrides["log_format"] = LogFormat.json
    configure_logging(settings.model_copy(update=overrides) if overrides else settings)


def _seed(namespace: argparse.Namespace) -> int:
    """Seed from the flag, else the project-wide simulation seed so runs line up."""
    explicit = getattr(namespace, "seed", None)
    if explicit is not None:
        return int(explicit)
    return get_settings().sim_seed


# ======================================================================================
# Commands
# ======================================================================================


def _run_train(namespace: argparse.Namespace) -> int:
    """Train, print the comparison table, then register the version in the database."""
    config = TrainingConfig(
        data_path=namespace.data,
        version=namespace.version,
        algorithm=namespace.algorithm,
        seed=_seed(namespace),
        activate=not namespace.no_activate,
    )
    result = train_model(config)
    print(result.summary())

    if namespace.no_register:
        print("  ml_models row skipped (--no-register)")
        return EXIT_OK

    try:
        model_id = asyncio.run(_register(result.record, activate=not namespace.no_activate))
    except Exception as exc:  # any database failure is reported, never swallowed
        _LOGGER.error("ml.register_failed", model=result.record.label, error=str(exc))
        print(
            f"error: the artefact was written to {result.record.artifact_path} but the "
            f"ml_models row could not be inserted: {exc}",
            file=sys.stderr,
        )
        return EXIT_FAILURE
    print(f"  ml_models row {model_id}")
    return EXIT_OK


async def _register(record: ModelRecord, *, activate: bool) -> UUID:
    """Insert the registry row in its own transaction and close the engine afterwards.

    The engine is disposed explicitly because this is a short-lived command: leaving a pool of
    open connections behind makes the process hang on exit under some drivers, and a CLI that
    does not return to the shell is a worse bug than a missing row.
    """
    try:
        async with session_scope() as session:
            return await register_model(session, record, activate=activate)
    finally:
        await dispose_engine()


def _run_evaluate(namespace: argparse.Namespace) -> int:
    """Score a stored artefact and the physical baseline on the same held-out rows."""
    record = resolve_model(namespace.version)
    model = load_model(record)
    seed = _seed(namespace)
    split = load_dataset(namespace.data, seed=seed)

    matrix = feature_matrix(split.test, context="test fold")
    actual = np.asarray(
        split.test.get_column("energy_consumption_kwh_100km").to_numpy(), dtype=np.float64
    ).reshape(-1)
    model_metrics = evaluate_regression(actual, model.predict(matrix))
    baseline_metrics = evaluate_regression(actual, baseline_predictions(split.test))
    stored = record.headline_metrics

    print(f"{record.label}  ({record.algorithm})")
    print(f"  dataset {split.source_path}  seed {seed}  test rows {split.test.height}")
    print("  test fold (kWh/100km)      MAE     RMSE       R2")
    print(
        f"    ML model             {model_metrics.mae:7.3f}  {model_metrics.rmse:7.3f}  "
        f"{model_metrics.r2:7.4f}"
    )
    print(
        f"    physical baseline    {baseline_metrics.mae:7.3f}  {baseline_metrics.rmse:7.3f}  "
        f"{baseline_metrics.r2:7.4f}"
    )
    print(
        f"    as trained           {stored['mae']:7.3f}  {stored['rmse']:7.3f}  {stored['r2']:7.4f}"
    )
    if baseline_metrics.mae > 0.0:
        delta = (baseline_metrics.mae - model_metrics.mae) / baseline_metrics.mae * 100.0
        verdict = "beats" if delta > 0 else "LOSES to"
        print(f"  ML {verdict} the physical baseline by {abs(delta):.1f} % MAE")
    return EXIT_OK


def _run_explain(namespace: argparse.Namespace) -> int:
    """Print (and optionally write) the global SHAP ranking of one model version."""
    record = resolve_model(namespace.version)
    report = (
        _recompute_global(record, namespace)
        if namespace.recompute
        else _stored_global(record, namespace)
    )

    print(f"{record.label}  global SHAP importance ({report.method})")
    if report.degraded_reason:
        print(f"  degraded: {report.degraded_reason}")
    print(f"  base value {report.base_value:.3f} kWh/100km over {report.sample_rows} sample row(s)")
    print("  rank  mean|SHAP|   share  feature")
    for entry in report.top(namespace.top):
        print(
            f"  {entry.rank:>4}  {entry.mean_abs_shap:9.4f}  {entry.share * 100:5.1f}%  "
            f"{entry.feature}  ({entry.label_en})"
        )

    if namespace.out is not None:
        namespace.out.parent.mkdir(parents=True, exist_ok=True)
        namespace.out.write_text(
            json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"  written to {namespace.out}")
    return EXIT_OK


def _stored_global(record: ModelRecord, namespace: argparse.Namespace) -> GlobalShapReport:
    """Read the ranking the training run stored, recomputing only if the file predates it."""
    stored = record.metrics.get("shap")
    if not isinstance(stored, dict) or not stored.get("global_importance"):
        _LOGGER.info("ml.shap_not_stored", model=record.label)
        return _recompute_global(record, namespace)
    from autotwin_ml.explain import GlobalImportance

    entries = tuple(
        GlobalImportance(
            feature=str(item["feature"]),
            mean_abs_shap=float(item["mean_abs_shap"]),
            share=float(item.get("share", 0.0)),
            rank=int(item.get("rank", index + 1)),
            label_de=str(item.get("label_de", item["feature"])),
            label_en=str(item.get("label_en", item["feature"])),
            insight_factor=item.get("insight_factor"),
        )
        for index, item in enumerate(stored["global_importance"])
    )
    return GlobalShapReport(
        method=str(stored.get("method", "tree_explainer")),
        entries=entries,
        base_value=float(stored.get("base_value", 0.0)),
        sample_rows=int(stored.get("sample_rows", 0)),
        background_rows=int(stored.get("background_rows", 0)),
        degraded_reason=stored.get("degraded_reason"),
    )


def _recompute_global(record: ModelRecord, namespace: argparse.Namespace) -> GlobalShapReport:
    """Recompute the ranking from the dataset's test fold."""
    model = load_model(record)
    split = load_dataset(namespace.data, seed=_seed(namespace))
    matrix = feature_matrix(split.test, context="test fold")
    explainer = get_explainer(model, background=feature_matrix(split.train, context="train fold"))
    return explainer.global_importance(matrix)


def _run_generate_data(namespace: argparse.Namespace) -> int:
    """Write the physics-sweep training set and say plainly what it is."""
    path = generate_physics_sweep(
        trips=namespace.trips,
        seed=_seed(namespace),
        out_path=namespace.out,
    )
    rows = describe_training_source(path).detail.get("rows", "?")
    print(f"wrote {rows} window(s) from {namespace.trips} synthetic trip(s) to {path}")
    print(
        "  NOTE: this is a physics sweep, not simulator telemetry. Prefer "
        "`python -m autotwin_simulator.cli generate-training-data` once the simulator runs."
    )
    return EXIT_OK


def _dispatch(namespace: argparse.Namespace) -> int:
    """Run the selected subcommand, mapping a handled failure onto exit code 1."""
    handlers = {
        "train": _run_train,
        "evaluate": _run_evaluate,
        "explain": _run_explain,
        "generate-data": _run_generate_data,
    }
    handler = handlers[namespace.command]
    try:
        return handler(namespace)
    except AutoTwinError as exc:
        # Everything AutoTwin raises deliberately is a handled failure: report it and exit 1.
        # Anything else is a bug and is allowed to propagate with its traceback intact.
        _LOGGER.error(
            "cli.command_failed",
            command=namespace.command,
            code=exc.code,
            message=exc.message,
            **exc.details,
        )
        print(f"error: {exc.message}", file=sys.stderr)
        return EXIT_FAILURE


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: parse, configure logging, dispatch, and return the process exit code."""
    parser = build_parser()
    namespace = parser.parse_args(argv)
    _configure(namespace)
    try:
        return _dispatch(namespace)
    except KeyboardInterrupt:
        _LOGGER.info("cli.interrupted", command=namespace.command)
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
