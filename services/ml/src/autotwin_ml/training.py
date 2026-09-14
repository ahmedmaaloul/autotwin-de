"""Training the energy-consumption regressor, and scoring it honestly (BUILD_SPEC §10.2).

The whole module exists to answer one question without flattering anybody: **does a gradient-
boosted model predict EV consumption better than the transparent physical model, on rows
neither of them has seen?**

Three design decisions make that answer trustworthy.

1. **The same rows.** ML and baseline are evaluated on the identical held-out test fold, with
   the baseline reconstructed from the very columns the ML model was given. No separate
   sampling, no different filtering.
2. **A held-out fold that is really held out.** The split is grouped by ``trip_id``
   (:mod:`autotwin_ml.dataset`), so no window of a test trip ever appeared in training. Early
   stopping reads the *validation* fold; the test fold is touched exactly once, at the end.
3. **The baseline is not strawmanned.** It is the full road-load model of BUILD_SPEC §10.1,
   evaluated per row at that row's real speed, gradient, temperature, distance and duration.
   The one thing it cannot see is the within-window speed *variation* — and that is a genuine
   property of a steady-state model, not a handicap imposed for the comparison.

If the model loses to the baseline, the metrics file says so and ``notes`` records it. A
portfolio project that reports a model beating physics only when it does is worth more than one
that always reports a win.

LightGBM, with a real fallback
------------------------------

LightGBM needs ``libomp`` and is the most common "works on my machine" failure in this stack.
:func:`train_model` therefore *tries* to import it and falls back to scikit-learn's
``HistGradientBoostingRegressor`` — the same histogram-based gradient boosting algorithm, pure
Python/Cython, no system library — logging which one it used and recording it in the artefact.
Both paths early-stop on our own validation fold, the sklearn one through an explicit
warm-start loop because ``HistGradientBoostingRegressor`` can only early-stop on a *random*
internal split, which would silently break the grouping.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import numpy as np
import polars as pl
from numpy import typing as npt
from numpy.random import default_rng

from autotwin_contracts.enums import DataOrigin, RoadClass, TrafficSeverity
from autotwin_contracts.temporal import utc_now
from autotwin_contracts.vehicles import VehicleProfile, get_vehicle_profile
from autotwin_core.config import Settings, get_settings
from autotwin_core.errors import ValidationError
from autotwin_core.logging import get_logger, log_duration
from autotwin_ml.baseline import PhysicalEnergyModel
from autotwin_ml.dataset import (
    DEFAULT_SPLIT_RATIOS,
    DatasetSplit,
    describe_training_source,
    load_dataset,
)
from autotwin_ml.explain import GlobalImportance, GlobalShapReport, global_shap_importance
from autotwin_ml.features import FEATURE_NAMES, TARGET_COLUMN, feature_matrix
from autotwin_ml.registry import (
    ModelRecord,
    artifact_paths,
    load_model,
    next_version,
    save_artifact,
    set_active_version,
)
from autotwin_ml.types import SegmentConditions

__all__ = [
    "ALGORITHM_LIGHTGBM",
    "ALGORITHM_SKLEARN",
    "MetricSet",
    "TrainingConfig",
    "TrainingResult",
    "baseline_predictions",
    "evaluate_regression",
    "train_model",
]

_LOGGER = get_logger(__name__)

ALGORITHM_LIGHTGBM: Final[str] = "lightgbm"
ALGORITHM_SKLEARN: Final[str] = "sklearn_hist_gradient_boosting"

_LIGHTGBM_PARAMS: Final[MappingProxyType[str, Any]] = MappingProxyType(
    {
        "objective": "regression",
        "n_estimators": 4000,
        "learning_rate": 0.08,
        "num_leaves": 31,
        "min_child_samples": 40,
        "subsample": 0.85,
        "subsample_freq": 1,
        "colsample_bytree": 0.9,
        "reg_lambda": 1.0,
        "max_bin": 255,
        "verbose": -1,
    }
)
"""Capacity chosen on the **validation fold** under a stated artefact-size budget.

There is no tuning search here and there should not be: a grid over 20 features and 20 000 rows
moves the third decimal of the MAE and consumes the attention that belongs on whether the
evaluation protocol is sound. What *was* measured — on validation only, never on test — is the
capacity/size trade-off, because BUILD_SPEC §10.2 requires the artefact to be committed:

===============  ======  ==========  =============
config           trees   val. MAE    artefact
===============  ======  ==========  =============
63 leaves, 0.05   2027       2.486       ~2.5 MB
31 leaves, 0.05   2036       2.457       ~2.5 MB
31 leaves, 0.08   1023       2.479       ~1.2 MB
15 leaves, 0.08    607       2.528       ~0.4 MB
===============  ======  ==========  =============

The chosen row costs 0.9 % of validation MAE against the best and halves the file. The two
extremes are both worse choices: 63 leaves is strictly dominated (more capacity, *worse*
validation error — it memorises), and 15 leaves gives up 2.9 % for a saving nobody needs. The
table is reproduced in ``docs/ml/methodology.md`` so the decision is auditable rather than
asserted.

``n_estimators`` is a **budget, not a target**: it is set high enough that early stopping is
always what ends training. A run that finishes at the cap has not converged, and its tree count
would be an artefact of this constant rather than a property of the data — so the cap is kept
well above where the validation curve actually turns (a few hundred trees on both the simulator
dataset and the physics sweep)."""

_SKLEARN_PARAMS: Final[MappingProxyType[str, Any]] = MappingProxyType(
    {
        "loss": "squared_error",
        "learning_rate": 0.08,
        "max_leaf_nodes": 31,
        "min_samples_leaf": 40,
        "l2_regularization": 1.0,
        "max_bins": 255,
    }
)
"""The sklearn fallback, matched to :data:`_LIGHTGBM_PARAMS` knob for knob wherever the two
libraries expose the same concept, so switching backend changes the implementation and not the
model's capacity. The two then landing within 1 % of each other is a check that neither is
doing something peculiar."""

_EARLY_STOPPING_ROUNDS: Final[int] = 100
"""Boosting rounds without validation improvement before training stops."""

_SKLEARN_STEP: Final[int] = 50
"""Trees added per warm-start round in the sklearn fallback's manual early stopping.

A compromise: smaller steps find the optimum more precisely, larger steps cost fewer refits.
50 resolves the stopping point to within 2 % of a typical 1 200-tree ensemble."""

_SKLEARN_MAX_ITER: Final[int] = 4000
"""Tree budget of the sklearn fallback, matching LightGBM's. Like ``n_estimators`` there it is
a budget rather than a target: a run that ends at the cap has not converged."""

_SKLEARN_PATIENCE_STEPS: Final[int] = 3
"""Blocks of :data:`_SKLEARN_STEP` trees without validation improvement before stopping."""

_MAX_SCATTER_POINTS: Final[int] = 400
"""Predicted-vs-actual pairs stored for the ``/ml`` page's scatter plot.

Enough to show the shape of the error, few enough that the metrics JSON stays a file one can
open in an editor and commit without apology."""

_SHAP_BACKGROUND_ROWS: Final[int] = 400
_SHAP_SAMPLE_ROWS: Final[int] = 2000
"""Rows drawn for the global SHAP computation. Exact Shapley values on a tree ensemble are
O(rows · trees · depth²); 2 000 rows put the mean |SHAP| ranking well inside its own sampling
noise while keeping training under a few seconds."""


# ======================================================================================
# Metrics
# ======================================================================================


@dataclass(frozen=True, slots=True)
class MetricSet:
    """MAE, RMSE and R² of one predictor on one fold, in kWh/100 km."""

    mae: float
    rmse: float
    r2: float

    def as_dict(self) -> dict[str, float]:
        """JSON-ready form."""
        return {"mae": self.mae, "rmse": self.rmse, "r2": self.r2}


def evaluate_regression(
    actual: npt.NDArray[np.float64],
    predicted: npt.NDArray[np.float64],
) -> MetricSet:
    """Score ``predicted`` against ``actual``.

    R² is computed explicitly rather than through ``sklearn.metrics`` so that the definition is
    visible: ``1 - SS_res / SS_tot`` against the mean of the *actual* values of this fold. That
    matters for the baseline, whose R² can legitimately be negative — a physical model is not
    fitted to this data and may do worse than predicting the fold's mean, and a negative number
    is the honest way to say so.
    """
    if actual.shape != predicted.shape:
        msg = f"actual and predicted must align; got {actual.shape} and {predicted.shape}"
        raise ValidationError(msg, details={"actual": list(actual.shape)})
    if actual.size == 0:
        msg = "cannot evaluate on an empty fold"
        raise ValidationError(msg, details={"rows": 0})
    residual = predicted - actual
    mae = float(np.mean(np.abs(residual)))
    rmse = float(np.sqrt(np.mean(np.square(residual))))
    ss_res = float(np.sum(np.square(residual)))
    ss_tot = float(np.sum(np.square(actual - float(np.mean(actual)))))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else float("nan")
    return MetricSet(mae=mae, rmse=rmse, r2=r2)


# ======================================================================================
# Physical baseline on the same rows
# ======================================================================================


def baseline_predictions(
    frame: pl.DataFrame,
    *,
    model: PhysicalEnergyModel | None = None,
) -> npt.NDArray[np.float64]:
    """Physical-model consumption in kWh/100 km for every row of ``frame``.

    Each window is replayed as a single :class:`~autotwin_ml.types.SegmentConditions` at the
    window's own mean speed, real distance and real duration. ``acceleration_ms2`` is zero,
    which is not a simplification but the definition of a steady-state baseline: the *net*
    acceleration across a 60 s window is essentially zero however the speed moved inside it, and
    the road-load model has no term for the cyclic loss. Recovering that loss from the
    dispersion features is the ML model's job, and this is the reference it has to beat.
    """
    physics = model or PhysicalEnergyModel()
    required = (
        "vehicle_code",
        "speed_kmh",
        "distance_km",
        "duration_s",
        "gradient_percent",
        "outside_temperature_c",
        "battery_temperature_c",
        "traffic_severity",
        "precipitation_mm",
        "wind_speed_ms",
        "road_class",
        "speed_limit_kmh",
    )
    missing = [name for name in required if name not in frame.columns]
    if missing:
        msg = f"cannot compute the physical baseline; missing column(s): {', '.join(missing)}"
        raise ValidationError(msg, details={"missing": missing})

    profiles: dict[str, VehicleProfile] = {}
    predictions = np.empty(frame.height, dtype=np.float64)
    for index, row in enumerate(frame.select(required).iter_rows(named=True)):
        code = str(row["vehicle_code"])
        profile = profiles.get(code)
        if profile is None:
            try:
                profile = get_vehicle_profile(code)
            except KeyError as exc:
                msg = f"training row {index} names unknown vehicle_code {code!r}"
                raise ValidationError(msg, details={"vehicle_code": code}) from exc
            profiles[code] = profile

        conditions = _conditions_from_row(row)
        result = physics.segment_energy(profile, conditions)
        predictions[index] = result.kwh_per_100km
    return predictions


def _conditions_from_row(row: dict[str, Any]) -> SegmentConditions:
    """Rebuild the steady-state segment a training row describes."""
    return SegmentConditions(
        speed_kmh=float(row["speed_kmh"]),
        distance_km=float(row["distance_km"]),
        gradient_percent=float(row["gradient_percent"]),
        outside_temperature_c=float(row["outside_temperature_c"]),
        battery_temperature_c=float(row["battery_temperature_c"]),
        acceleration_ms2=0.0,
        traffic_severity=TrafficSeverity(str(row["traffic_severity"])),
        precipitation_mm=float(row["precipitation_mm"]),
        wind_speed_ms=float(row["wind_speed_ms"]),
        road_class=RoadClass(str(row["road_class"])),
        speed_limit_kmh=float(row["speed_limit_kmh"]),
        duration_s=float(row["duration_s"]),
    )


# ======================================================================================
# Estimators
# ======================================================================================


def _lightgbm_available() -> bool:
    """Whether LightGBM imports on this platform (``libomp`` is the usual blocker)."""
    try:
        import lightgbm  # noqa: F401  - import is the probe
    except Exception as exc:  # a broken OpenMP raises OSError, not ImportError
        _LOGGER.warning("ml.lightgbm_unavailable", error=str(exc))
        return False
    return True


def _fit_lightgbm(
    x_train: npt.NDArray[np.float64],
    y_train: npt.NDArray[np.float64],
    x_validation: npt.NDArray[np.float64],
    y_validation: npt.NDArray[np.float64],
    *,
    seed: int,
) -> tuple[Any, int]:
    """Fit a LightGBM regressor, early-stopping on the validation fold."""
    import lightgbm as lgb

    # No ``feature_name=`` is passed: the model is fitted and served on positional NumPy
    # matrices throughout, and naming the columns at fit time only makes every later
    # ``predict`` on an unnamed array emit a "X does not have valid feature names" warning.
    # The name-to-position contract lives in ``FEATURE_NAMES`` and is enforced at load time.
    estimator = lgb.LGBMRegressor(random_state=seed, n_jobs=-1, **dict(_LIGHTGBM_PARAMS))
    # LightGBM 4.7 deprecated the ``eval_set=[(X, y)]`` spelling in favour of ``eval_X``/
    # ``eval_y`` but still accepts it; 4.5 and 4.6 accept only the old one. The service
    # declares ``lightgbm>=4.5``, so the call is shaped from the installed signature rather
    # than pinned to one spelling — a version bump then changes nothing here and emits no
    # deprecation warning into the CLI's output.
    validation_kwargs: dict[str, Any] = (
        {"eval_X": x_validation, "eval_y": y_validation}
        if "eval_X" in inspect.signature(estimator.fit).parameters
        else {"eval_set": [(x_validation, y_validation)]}
    )
    estimator.fit(
        x_train,
        y_train,
        eval_metric="l1",
        callbacks=[
            lgb.early_stopping(_EARLY_STOPPING_ROUNDS, verbose=False),
            lgb.log_evaluation(0),
        ],
        **validation_kwargs,
    )
    best = int(estimator.best_iteration_ or estimator.n_estimators)
    return estimator, best


def _fit_sklearn(
    x_train: npt.NDArray[np.float64],
    y_train: npt.NDArray[np.float64],
    x_validation: npt.NDArray[np.float64],
    y_validation: npt.NDArray[np.float64],
    *,
    seed: int,
) -> tuple[Any, int]:
    """Fit ``HistGradientBoostingRegressor`` with early stopping on **our** validation fold.

    scikit-learn's built-in ``early_stopping=True`` carves a random slice out of the training
    data, which would put windows of a training trip into the stopping set and reintroduce
    exactly the leakage the grouped split removes. The warm-start loop below adds trees in
    blocks, scores each block on the real validation fold, and then refits once at the best
    size so the saved model contains no trees past the optimum.
    """
    from sklearn.ensemble import HistGradientBoostingRegressor

    probe = HistGradientBoostingRegressor(
        random_state=seed,
        early_stopping=False,
        warm_start=True,
        max_iter=_SKLEARN_STEP,
        **dict(_SKLEARN_PARAMS),
    )
    best_mae = float("inf")
    best_iterations = _SKLEARN_STEP
    steps_without_improvement = 0
    for total in range(_SKLEARN_STEP, _SKLEARN_MAX_ITER + 1, _SKLEARN_STEP):
        probe.set_params(max_iter=total)
        probe.fit(x_train, y_train)
        mae = float(np.mean(np.abs(np.asarray(probe.predict(x_validation)) - y_validation)))
        if mae < best_mae - 1e-6:
            best_mae = mae
            best_iterations = total
            steps_without_improvement = 0
        else:
            steps_without_improvement += 1
            if steps_without_improvement >= _SKLEARN_PATIENCE_STEPS:
                break

    estimator = HistGradientBoostingRegressor(
        random_state=seed,
        early_stopping=False,
        warm_start=False,
        max_iter=best_iterations,
        **dict(_SKLEARN_PARAMS),
    )
    estimator.fit(x_train, y_train)
    return estimator, best_iterations


def _feature_importance(
    estimator: Any,
    algorithm: str,
    *,
    x_validation: npt.NDArray[np.float64],
    y_validation: npt.NDArray[np.float64],
    seed: int,
) -> list[dict[str, Any]]:
    """Feature importance, by the best measure the backend offers.

    LightGBM reports both *gain* (total loss reduction attributable to splits on the feature)
    and *split* (how often it was used); gain is the one that answers "how much does this
    feature matter". ``HistGradientBoostingRegressor`` exposes neither, so the fallback is
    permutation importance measured on the validation fold — a model-agnostic measure with a
    different meaning (how much worse the model gets when the feature is shuffled), which is
    why the ``method`` field is reported alongside and never silently conflated.
    """
    if algorithm == ALGORITHM_LIGHTGBM:
        booster = estimator.booster_
        gains = np.asarray(booster.feature_importance(importance_type="gain"), dtype=np.float64)
        splits = np.asarray(booster.feature_importance(importance_type="split"), dtype=np.float64)
        total = float(gains.sum()) or 1.0
        gain_rows: list[dict[str, Any]] = [
            {
                "feature": name,
                "importance": float(gains[index]),
                "importance_normalised": float(gains[index] / total),
                "splits": int(splits[index]),
                "method": "gain",
            }
            for index, name in enumerate(FEATURE_NAMES)
        ]
        gain_rows.sort(key=lambda item: float(item["importance"]), reverse=True)
        return gain_rows

    from sklearn.inspection import permutation_importance

    result = permutation_importance(
        estimator,
        x_validation,
        y_validation,
        n_repeats=5,
        random_state=seed,
        scoring="neg_mean_absolute_error",
    )
    means = np.asarray(result.importances_mean, dtype=np.float64)
    deviations = np.asarray(result.importances_std, dtype=np.float64)
    total = float(np.abs(means).sum()) or 1.0
    permutation_rows: list[dict[str, Any]] = [
        {
            "feature": name,
            "importance": float(means[index]),
            "importance_normalised": float(abs(means[index]) / total),
            "std": float(deviations[index]),
            "method": "permutation",
        }
        for index, name in enumerate(FEATURE_NAMES)
    ]
    permutation_rows.sort(key=lambda item: float(item["importance"]), reverse=True)
    return permutation_rows


# ======================================================================================
# Public API
# ======================================================================================


@dataclass(frozen=True, slots=True)
class TrainingConfig:
    """Everything one training run needs to be reproducible."""

    data_path: Path | None = None
    """Training Parquet; defaults to ``data/gold/training/energy_windows.parquet``."""

    version: str | None = None
    """Version to write; defaults to the next unused integer."""

    algorithm: str | None = None
    """``lightgbm``, ``sklearn``, or ``None`` to prefer LightGBM and fall back."""

    seed: int = 20_260_214
    """Seeds the split, the boosting subsampling and every sampling in the report."""

    name: str | None = None
    """Model name; defaults to ``AUTOTWIN_ML_ACTIVE_MODEL`` (``energy_consumption``)."""

    model_dir: Path | None = None
    """Artefact directory; defaults to ``AUTOTWIN_ML_MODEL_DIR``."""

    ratios: tuple[float, float, float] = DEFAULT_SPLIT_RATIOS
    """Train/validation/test row proportions."""

    activate: bool = True
    """Point ``<name>.active`` at the new version once it is written."""

    settings: Settings | None = None
    """Injected settings, for tests that point the model directory at a tmp path."""


@dataclass(frozen=True, slots=True)
class TrainingResult:
    """Outcome of one run: the artefact, the numbers, and the comparison that justifies it."""

    record: ModelRecord
    test: MetricSet
    baseline: MetricSet
    validation: MetricSet
    train: MetricSet
    split: DatasetSplit
    algorithm: str
    iterations: int
    lightgbm_available: bool
    global_importance: tuple[GlobalImportance, ...]
    payload: dict[str, Any] = field(repr=False)
    """The full metrics document, exactly as written to ``*.metrics.json``."""

    @property
    def beats_baseline(self) -> bool:
        """True when the ML model's test MAE is lower than the physical model's."""
        return self.test.mae < self.baseline.mae

    @property
    def mae_improvement_percent(self) -> float:
        """How much lower the ML MAE is than the baseline MAE, in percent. Negative if worse."""
        if self.baseline.mae <= 0.0:
            return float("nan")
        return (self.baseline.mae - self.test.mae) / self.baseline.mae * 100.0

    def summary(self) -> str:
        """The human-readable table the CLI prints — ML against baseline, same rows."""
        verdict = (
            f"ML beats the physical baseline by {self.mae_improvement_percent:.1f} % MAE"
            if self.beats_baseline
            else f"ML LOSES to the physical baseline ({self.mae_improvement_percent:.1f} % MAE)"
        )
        return "\n".join(
            (
                f"{self.record.label}  ({self.algorithm}, {self.iterations} trees)",
                f"  training rows {self.split.train.height}  "
                f"validation {self.split.validation.height}  test {self.split.test.height}",
                "  test fold (kWh/100km)      MAE     RMSE       R2",
                f"    ML model             {self.test.mae:7.3f}  {self.test.rmse:7.3f}  "
                f"{self.test.r2:7.4f}",
                f"    physical baseline    {self.baseline.mae:7.3f}  {self.baseline.rmse:7.3f}  "
                f"{self.baseline.r2:7.4f}",
                f"  {verdict}",
                f"  artefact  {self.record.artifact_path}",
                f"  metrics   {self.record.metrics_path}",
            )
        )


def train_model(config: TrainingConfig | None = None) -> TrainingResult:
    """Train, evaluate against the physical baseline, and persist one model version.

    Writes ``<name>_v<version>.joblib`` and ``<name>_v<version>.metrics.json`` into the model
    directory and (unless ``activate=False``) points ``<name>.active`` at the new version. The
    ``ml_models`` row is **not** written here — that needs a database session, and keeping this
    function synchronous and database-free is what lets it run in CI, in a notebook, and inside
    the CLI without an event loop.
    """
    settings = (config.settings if config else None) or get_settings()
    cfg = config or TrainingConfig()
    name = cfg.name or settings.ml_active_model

    split = load_dataset(cfg.data_path, seed=cfg.seed, ratios=cfg.ratios)
    source = describe_training_source(split.source_path)

    x_train = feature_matrix(split.train, context="train fold")
    x_validation = feature_matrix(split.validation, context="validation fold")
    x_test = feature_matrix(split.test, context="test fold")
    y_train = _target(split.train)
    y_validation = _target(split.validation)
    y_test = _target(split.test)

    lightgbm_available = _lightgbm_available()
    algorithm = _select_algorithm(cfg.algorithm, lightgbm_available=lightgbm_available)

    with log_duration(_LOGGER, "ml.training", algorithm=algorithm, rows=int(x_train.shape[0])):
        if algorithm == ALGORITHM_LIGHTGBM:
            estimator, iterations = _fit_lightgbm(
                x_train, y_train, x_validation, y_validation, seed=cfg.seed
            )
        else:
            estimator, iterations = _fit_sklearn(
                x_train, y_train, x_validation, y_validation, seed=cfg.seed
            )

    predictions_test = np.asarray(estimator.predict(x_test), dtype=np.float64)
    test_metrics = evaluate_regression(y_test, predictions_test)
    validation_metrics = evaluate_regression(
        y_validation, np.asarray(estimator.predict(x_validation), dtype=np.float64)
    )
    train_metrics = evaluate_regression(
        y_train, np.asarray(estimator.predict(x_train), dtype=np.float64)
    )
    baseline_test = baseline_predictions(split.test)
    baseline_metrics = evaluate_regression(y_test, baseline_test)

    version = cfg.version or next_version(name=name, model_dir=cfg.model_dir, settings=settings)
    paths = artifact_paths(name, version, model_dir=cfg.model_dir, settings=settings)
    trained_at = utc_now()
    save_artifact(
        estimator,
        name=name,
        version=version,
        algorithm=algorithm,
        trained_at=trained_at,
        path=paths.artifact,
    )

    # Round-trip through the registry before reporting success: it re-reads the artefact and
    # re-checks the feature contract, so a run can never end by claiming to have written a
    # model that cannot be loaded back.
    loaded = load_model(
        ModelRecord(
            name=name,
            version=version,
            algorithm=algorithm,
            trained_at=trained_at,
            training_rows=int(x_train.shape[0]),
            feature_names=FEATURE_NAMES,
            metrics={},
            artifact_path=paths.artifact,
            metrics_path=paths.metrics,
            training_data_origin=DataOrigin.simulated,
        )
    )
    importance = _feature_importance(
        estimator,
        algorithm,
        x_validation=x_validation,
        y_validation=y_validation,
        seed=cfg.seed,
    )
    shap_report = global_shap_importance(
        loaded,
        sample=_subsample(x_test, _SHAP_SAMPLE_ROWS, seed=cfg.seed),
        background=_subsample(x_train, _SHAP_BACKGROUND_ROWS, seed=cfg.seed),
    )

    payload = _build_payload(
        name=name,
        version=version,
        algorithm=algorithm,
        trained_at=trained_at,
        iterations=iterations,
        seed=cfg.seed,
        split=split,
        source=source.as_dict(),
        test=test_metrics,
        baseline=baseline_metrics,
        validation=validation_metrics,
        train=train_metrics,
        importance=importance,
        shap_report=shap_report,
        y_test=y_test,
        predictions_test=predictions_test,
        baseline_test=baseline_test,
        lightgbm_available=lightgbm_available,
    )
    paths.metrics.parent.mkdir(parents=True, exist_ok=True)
    paths.metrics.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if cfg.activate:
        set_active_version(version, name=name, model_dir=cfg.model_dir, settings=settings)

    record = ModelRecord.from_metrics_file(paths.metrics, is_active=cfg.activate)
    result = TrainingResult(
        record=record,
        test=test_metrics,
        baseline=baseline_metrics,
        validation=validation_metrics,
        train=train_metrics,
        split=split,
        algorithm=algorithm,
        iterations=iterations,
        lightgbm_available=lightgbm_available,
        global_importance=shap_report.entries,
        payload=payload,
    )
    _LOGGER.info(
        "ml.trained",
        model=record.label,
        algorithm=algorithm,
        trees=iterations,
        test_mae=test_metrics.mae,
        baseline_mae=baseline_metrics.mae,
        beats_baseline=result.beats_baseline,
    )
    return result


def _select_algorithm(requested: str | None, *, lightgbm_available: bool) -> str:
    """Resolve the ``--algorithm`` flag against what is importable, refusing to lie.

    An explicit ``--algorithm lightgbm`` on a machine without LightGBM raises rather than
    silently training something else: the user asked for a specific model family, and the
    artefact would be labelled with the algorithm it actually is, making the mismatch a puzzle
    later. Automatic selection (the default) falls back and logs.
    """
    if requested in (None, "auto"):
        return ALGORITHM_LIGHTGBM if lightgbm_available else ALGORITHM_SKLEARN
    if requested == ALGORITHM_LIGHTGBM:
        if not lightgbm_available:
            msg = (
                "--algorithm lightgbm was requested but LightGBM cannot be imported on this "
                "platform (usually a missing libomp). Use --algorithm sklearn."
            )
            raise ValidationError(msg, details={"algorithm": requested})
        return ALGORITHM_LIGHTGBM
    if requested in ("sklearn", ALGORITHM_SKLEARN):
        return ALGORITHM_SKLEARN
    msg = f"unknown algorithm {requested!r}; expected 'lightgbm' or 'sklearn'"
    raise ValidationError(msg, details={"algorithm": requested})


def _target(frame: pl.DataFrame) -> npt.NDArray[np.float64]:
    """The target column as a 1-D ``float64`` array."""
    return np.asarray(frame.get_column(TARGET_COLUMN).to_numpy(), dtype=np.float64).reshape(-1)


def _subsample(
    matrix: npt.NDArray[np.float64],
    rows: int,
    *,
    seed: int,
) -> npt.NDArray[np.float64]:
    """A seeded random subset of ``matrix``'s rows — the whole matrix when it is small enough."""
    if matrix.shape[0] <= rows:
        return matrix
    indices = default_rng(seed).choice(matrix.shape[0], size=rows, replace=False)
    return np.asarray(matrix[np.sort(indices)], dtype=np.float64)


def _build_payload(
    *,
    name: str,
    version: str,
    algorithm: str,
    trained_at: datetime,
    iterations: int,
    seed: int,
    split: DatasetSplit,
    source: dict[str, Any],
    test: MetricSet,
    baseline: MetricSet,
    validation: MetricSet,
    train: MetricSet,
    importance: list[dict[str, Any]],
    shap_report: GlobalShapReport,
    y_test: npt.NDArray[np.float64],
    predictions_test: npt.NDArray[np.float64],
    baseline_test: npt.NDArray[np.float64],
    lightgbm_available: bool,
) -> dict[str, Any]:
    """Assemble the metrics document written next to the artefact.

    ``metrics`` is *exactly* what goes into ``ml_models.metrics`` (BUILD_SPEC §3.2): the six
    headline numbers at the top level so a consumer can read ``metrics["mae"]``, with the
    richer report — scatter points, residuals, importances, SHAP, split description — nested
    beside them for ``GET /api/v1/ml/metrics``.
    """
    residuals = predictions_test - y_test
    sample_count = min(_MAX_SCATTER_POINTS, y_test.size)
    indices = np.sort(default_rng(seed).choice(y_test.size, size=sample_count, replace=False))
    scatter = [
        {
            "actual": float(y_test[index]),
            "predicted": float(predictions_test[index]),
            "baseline": float(baseline_test[index]),
        }
        for index in indices
    ]
    improvement = (
        (baseline.mae - test.mae) / baseline.mae * 100.0 if baseline.mae > 0.0 else float("nan")
    )
    notes = (
        f"Trained on {split.train.height} windows of {source.get('kind', 'unknown')} data. "
        f"Test MAE {test.mae:.3f} kWh/100km versus physical baseline {baseline.mae:.3f} "
        f"({improvement:+.1f} %). Labels are simulated, not measured — see "
        "docs/ml/methodology.md."
    )
    return {
        "name": name,
        "version": version,
        "algorithm": algorithm,
        "trained_at": trained_at.isoformat(),
        "training_rows": split.train.height,
        "feature_names": list(FEATURE_NAMES),
        "target": TARGET_COLUMN,
        "training_data_origin": DataOrigin.simulated.value,
        "seed": seed,
        "notes": notes,
        "hyperparameters": {
            **(
                dict(_LIGHTGBM_PARAMS) if algorithm == ALGORITHM_LIGHTGBM else dict(_SKLEARN_PARAMS)
            ),
            "n_estimators_used": iterations,
            "early_stopping_rounds": _EARLY_STOPPING_ROUNDS,
            "lightgbm_available": lightgbm_available,
        },
        "metrics": {
            "mae": test.mae,
            "rmse": test.rmse,
            "r2": test.r2,
            "baseline_mae": baseline.mae,
            "baseline_rmse": baseline.rmse,
            "baseline_r2": baseline.r2,
            "mae_improvement_percent": improvement,
            "test_rows": int(y_test.size),
            "train": train.as_dict(),
            "validation": validation.as_dict(),
            "split": split.as_dict(),
            "training_data_source": source,
            "feature_importance": importance,
            "shap": shap_report.as_dict(),
            "predicted_vs_actual": scatter,
            "residuals": [float(residuals[index]) for index in indices],
            "residual_summary": {
                "mean": float(np.mean(residuals)),
                "std": float(np.std(residuals)),
                "p05": float(np.quantile(residuals, 0.05)),
                "p50": float(np.quantile(residuals, 0.50)),
                "p95": float(np.quantile(residuals, 0.95)),
            },
        },
    }
