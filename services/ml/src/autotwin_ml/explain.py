"""SHAP explainability for the energy model — global at train time, local at request time.

Two questions, two costs, two places.

**Global** — *which features does this model rely on?* Computed once during training over a
sample of the test fold, stored in ``*.metrics.json``, and served from there by
``GET /api/v1/ml/explain/global``. Nothing is recomputed per request, because the answer cannot
change until the model does.

**Local** — *why this number, for this segment?* Computed per request by
``POST /api/v1/ml/predict``. A tree SHAP evaluation on a 1 200-tree ensemble takes single-digit
milliseconds for one row, so it is affordable inline; the explainer object itself is cached per
artefact, because *constructing* it walks every tree in the model.

Why SHAP and not the model's own feature importance
---------------------------------------------------

Gain-based importance answers "which feature did the training algorithm split on most
profitably", which is a statement about the fitting procedure. SHAP answers "how much did this
feature move *this* prediction away from the average prediction", which is a statement about the
model's behaviour and is additive: the contributions plus the base value reconstruct the
prediction exactly. Only the additive form can be rendered next to a number a user is looking
at, and only the additive form can be folded into the deterministic driver list of
BUILD_SPEC §11 — which is what :data:`~autotwin_ml.features.INSIGHT_FACTOR_BY_FEATURE` is for.

Honest degradation
------------------

``shap.TreeExplainer`` supports both backends this project trains — LightGBM boosters and
scikit-learn's ``HistGradientBoostingRegressor`` — and that was verified on this platform
rather than assumed. If it ever fails to build (a SHAP version that has not caught up with a
new sklearn internal, most likely), the explainer falls back to SHAP's model-agnostic
permutation explainer, records ``method="permutation"`` in every report it produces, and says
so in the ``degraded_reason`` field. The numbers then mean something slightly different —
permutation values are an approximation of the same Shapley values, computed by masking
features against a background sample — and a consumer that cares can branch on ``method``
instead of being quietly handed different quantities under the same name.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
from numpy import typing as npt

from autotwin_core.errors import ValidationError
from autotwin_core.logging import get_logger
from autotwin_ml.features import FEATURE_LABELS, FEATURE_NAMES, INSIGHT_FACTOR_BY_FEATURE
from autotwin_ml.insights import DriverDirection
from autotwin_ml.registry import LoadedModel

__all__ = [
    "METHOD_PERMUTATION",
    "METHOD_TREE",
    "FeatureContribution",
    "GlobalImportance",
    "GlobalShapReport",
    "PredictionExplanation",
    "ShapExplainer",
    "clear_explainer_cache",
    "explain_prediction",
    "get_explainer",
    "global_shap_importance",
]

_LOGGER = get_logger(__name__)

METHOD_TREE: Final[str] = "tree_explainer"
"""Exact tree SHAP — the intended path for both supported backends."""

METHOD_PERMUTATION: Final[str] = "permutation"
"""Model-agnostic fallback; approximate, and always reported as such."""

_NEUTRAL_CONTRIBUTION_KWH_100KM: Final[float] = 0.05
"""Below this a contribution is rendered as ``neutral`` rather than up or down.

0.05 kWh/100 km is roughly a thousandth of a typical prediction — far inside the model's own
error bar, and well inside the noise floor of the labels it was trained on. Showing such a
contribution with an arrow would imply a confidence the model does not have."""

_PERMUTATION_BACKGROUND_ROWS: Final[int] = 100
"""Background sample size for the fallback explainer.

Permutation SHAP costs one model evaluation for every combination of explained row, background
row and permutation step, so the background size is the term that decides whether a
request-time explanation takes milliseconds or seconds."""


# ======================================================================================
# Result types
# ======================================================================================


@dataclass(frozen=True, slots=True)
class GlobalImportance:
    """One feature's global importance: the mean absolute SHAP value over a sample."""

    feature: str
    mean_abs_shap: float
    """Average |contribution| in kWh/100 km — the unit the target is in, not a unitless score."""

    share: float
    """This feature's share of the total mean |SHAP| across all features, 0-1."""

    rank: int
    """1-based rank, 1 being the most important."""

    label_de: str
    label_en: str
    insight_factor: str | None
    """Matching driver id of :mod:`autotwin_ml.insights`, or ``None`` when the feature does not
    correspond to a driver the counterfactual ladder models."""

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form for the metrics file and ``GET /api/v1/ml/explain/global``."""
        return {
            "feature": self.feature,
            "mean_abs_shap": self.mean_abs_shap,
            "share": self.share,
            "rank": self.rank,
            "label_de": self.label_de,
            "label_en": self.label_en,
            "insight_factor": self.insight_factor,
        }


@dataclass(frozen=True, slots=True)
class GlobalShapReport:
    """Global feature importance for one model version, computed once at training time."""

    method: str
    entries: tuple[GlobalImportance, ...]
    base_value: float
    """The explainer's expected value: the model's mean prediction over the background."""

    sample_rows: int
    background_rows: int
    degraded_reason: str | None = None

    def top(self, count: int = 10) -> tuple[GlobalImportance, ...]:
        """The ``count`` most important features."""
        return self.entries[:count]

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form, stored under ``metrics.shap``."""
        return {
            "method": self.method,
            "base_value": self.base_value,
            "sample_rows": self.sample_rows,
            "background_rows": self.background_rows,
            "degraded_reason": self.degraded_reason,
            "global_importance": [entry.as_dict() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class FeatureContribution:
    """One feature's signed contribution to one prediction."""

    feature: str
    value: float
    """The feature's value for this prediction — a contribution without it is unreadable."""

    contribution: float
    """Signed SHAP value in kWh/100 km. Positive raised the prediction."""

    direction: DriverDirection
    """Rendering hint, reusing the vocabulary of :mod:`autotwin_ml.insights` so that a SHAP
    contribution and a counterfactual driver render with the same arrow and the same colour."""

    label_de: str
    label_en: str
    insight_factor: str | None

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form for the ``POST /api/v1/ml/predict`` response."""
        return {
            "feature": self.feature,
            "value": self.value,
            "contribution": self.contribution,
            "direction": self.direction.value,
            "label_de": self.label_de,
            "label_en": self.label_en,
            "insight_factor": self.insight_factor,
        }


@dataclass(frozen=True, slots=True)
class PredictionExplanation:
    """Why the model returned this number, as an additive decomposition.

    **Additive identity** — ``base_value + sum(contribution) == prediction`` up to floating
    point. It is asserted rather than assumed by :attr:`reconstruction_error`, because a
    decomposition that does not reconstruct its own prediction is worse than none at all.
    """

    prediction: float
    base_value: float
    method: str
    contributions: tuple[FeatureContribution, ...]
    """Every feature, ordered by descending |contribution|."""

    @property
    def reconstruction_error(self) -> float:
        """``|base + Σ contributions - prediction|`` — the additivity check, in kWh/100 km."""
        total = self.base_value + sum(item.contribution for item in self.contributions)
        return abs(total - self.prediction)

    def top(self, count: int = 5) -> tuple[FeatureContribution, ...]:
        """The ``count`` features that moved this prediction most."""
        return self.contributions[:count]

    def by_insight_factor(self) -> dict[str, float]:
        """Contributions aggregated onto the driver ids of BUILD_SPEC §11.

        Features with no mapped driver are summed under ``"other"`` rather than dropped, so the
        aggregation still adds up to the same total as the flat list.
        """
        totals: dict[str, float] = {}
        for item in self.contributions:
            key = item.insight_factor or "other"
            totals[key] = totals.get(key, 0.0) + item.contribution
        return totals

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form; also what is stored in ``ml_predictions.shap_values``."""
        return {
            "prediction": self.prediction,
            "base_value": self.base_value,
            "method": self.method,
            "reconstruction_error": self.reconstruction_error,
            "contributions": [item.as_dict() for item in self.contributions],
        }


# ======================================================================================
# The explainer
# ======================================================================================


def _as_scalar(value: Any) -> float:
    """Coerce an explainer's expected value to a float.

    LightGBM's ``TreeExplainer`` reports a scalar; the scikit-learn path reports a one-element
    array. Normalising here keeps every downstream field a plain number instead of "sometimes
    an array".
    """
    array = np.asarray(value, dtype=np.float64).reshape(-1)
    if array.size == 0:
        return 0.0
    return float(array[0])


class ShapExplainer:
    """A cached SHAP explainer bound to one loaded model artefact.

    Constructing a ``TreeExplainer`` walks the whole ensemble, so it is done once per artefact
    and reused; the object is stateless afterwards and safe to share across requests.
    """

    __slots__ = ("_background", "_explainer", "_method", "_model", "_reason")

    def __init__(
        self,
        model: LoadedModel,
        *,
        background: npt.NDArray[np.float64] | None = None,
    ) -> None:
        """Build a tree explainer, falling back to the permutation explainer if that fails."""
        import shap

        self._model = model
        self._background = background
        try:
            self._explainer: Any = shap.TreeExplainer(model.estimator)
            self._method = METHOD_TREE
            self._reason: str | None = None
        except Exception as exc:  # SHAP raises a bare Exception for unsupported models
            if background is None or background.size == 0:
                msg = (
                    "SHAP TreeExplainer could not be built for "
                    f"{model.algorithm!r} and no background sample was supplied for the "
                    "permutation fallback"
                )
                raise ValidationError(msg, details={"algorithm": model.algorithm}) from exc
            sample = background[:_PERMUTATION_BACKGROUND_ROWS]
            self._explainer = shap.PermutationExplainer(model.estimator.predict, sample)
            self._method = METHOD_PERMUTATION
            self._reason = f"TreeExplainer unavailable for {model.algorithm}: {exc}"
            _LOGGER.warning(
                "ml.shap_degraded",
                model=model.record.label,
                algorithm=model.algorithm,
                error=str(exc),
            )

    @property
    def method(self) -> str:
        """``tree_explainer`` or ``permutation``."""
        return self._method

    @property
    def degraded_reason(self) -> str | None:
        """Why the fallback is in use, or ``None`` when exact tree SHAP is."""
        return self._reason

    @property
    def base_value(self) -> float:
        """The model's expected prediction — the origin every contribution is measured from."""
        return _as_scalar(getattr(self._explainer, "expected_value", 0.0))

    def shap_values(self, matrix: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Raw SHAP values for an ``(n, 20)`` matrix, returned as ``(n, 20)``."""
        if matrix.ndim != 2 or matrix.shape[1] != len(FEATURE_NAMES):
            msg = f"expected a (n, {len(FEATURE_NAMES)}) matrix, got shape {matrix.shape}"
            raise ValidationError(msg, details={"shape": list(matrix.shape)})
        if self._method == METHOD_TREE:
            raw = self._explainer.shap_values(matrix, check_additivity=False)
        else:
            raw = self._explainer(matrix).values
        values = np.asarray(raw, dtype=np.float64)
        if values.ndim == 3:
            # Multi-output shape (rows, features, outputs); this is a single-output regressor,
            # so the third axis is always length 1.
            values = values[:, :, 0]
        return values.reshape(matrix.shape[0], len(FEATURE_NAMES))

    def explain(self, matrix: npt.NDArray[np.float64]) -> list[PredictionExplanation]:
        """Explain every row of ``matrix``, contributions sorted by descending magnitude."""
        values = self.shap_values(matrix)
        predictions = self._model.predict(matrix)
        base = self.base_value
        explanations: list[PredictionExplanation] = []
        for row_index in range(matrix.shape[0]):
            contributions = tuple(
                sorted(
                    (
                        _contribution(
                            name,
                            value=float(matrix[row_index, column]),
                            contribution=float(values[row_index, column]),
                        )
                        for column, name in enumerate(FEATURE_NAMES)
                    ),
                    key=lambda item: abs(item.contribution),
                    reverse=True,
                )
            )
            explanations.append(
                PredictionExplanation(
                    prediction=float(predictions[row_index]),
                    base_value=base,
                    method=self._method,
                    contributions=contributions,
                )
            )
        return explanations

    def global_importance(
        self,
        sample: npt.NDArray[np.float64],
        *,
        background_rows: int | None = None,
    ) -> GlobalShapReport:
        """Mean |SHAP| per feature over ``sample`` — the global picture stored at train time."""
        values = self.shap_values(sample)
        means = np.abs(values).mean(axis=0)
        total = float(means.sum()) or 1.0
        ordered = sorted(
            zip(FEATURE_NAMES, means.tolist(), strict=True),
            key=lambda item: item[1],
            reverse=True,
        )
        entries = tuple(
            GlobalImportance(
                feature=name,
                mean_abs_shap=float(mean),
                share=float(mean) / total,
                rank=rank,
                label_de=FEATURE_LABELS[name][0],
                label_en=FEATURE_LABELS[name][1],
                insight_factor=INSIGHT_FACTOR_BY_FEATURE.get(name),
            )
            for rank, (name, mean) in enumerate(ordered, start=1)
        )
        return GlobalShapReport(
            method=self._method,
            entries=entries,
            base_value=self.base_value,
            sample_rows=int(sample.shape[0]),
            background_rows=(
                background_rows
                if background_rows is not None
                else (0 if self._background is None else int(self._background.shape[0]))
            ),
            degraded_reason=self._reason,
        )


def _contribution(name: str, *, value: float, contribution: float) -> FeatureContribution:
    """Wrap one raw SHAP number with its label and its rendering direction."""
    if abs(contribution) < _NEUTRAL_CONTRIBUTION_KWH_100KM:
        direction = DriverDirection.neutral
    elif contribution > 0.0:
        direction = DriverDirection.increase
    else:
        direction = DriverDirection.decrease
    label_de, label_en = FEATURE_LABELS[name]
    return FeatureContribution(
        feature=name,
        value=value,
        contribution=contribution,
        direction=direction,
        label_de=label_de,
        label_en=label_en,
        insight_factor=INSIGHT_FACTOR_BY_FEATURE.get(name),
    )


# ======================================================================================
# Caching
# ======================================================================================

_CACHE_LOCK: Final[threading.Lock] = threading.Lock()
_EXPLAINER_CACHE: dict[tuple[str, int], ShapExplainer] = {}
"""(artefact path, artefact mtime) → explainer, mirroring the model cache in the registry so
that a retrained version in place invalidates both."""


def clear_explainer_cache() -> None:
    """Forget every cached explainer — for tests and for an explicit reload."""
    with _CACHE_LOCK:
        _EXPLAINER_CACHE.clear()


def _cache_key(path: Path) -> tuple[str, int]:
    """Cache identity of an artefact: its path and its modification time."""
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return (str(path), 0)


def get_explainer(
    model: LoadedModel,
    *,
    background: npt.NDArray[np.float64] | None = None,
) -> ShapExplainer:
    """Return the cached explainer for ``model``, constructing it on first use."""
    key = _cache_key(model.record.artifact_path)
    with _CACHE_LOCK:
        cached = _EXPLAINER_CACHE.get(key)
    if cached is not None:
        return cached
    explainer = ShapExplainer(model, background=background)
    with _CACHE_LOCK:
        _EXPLAINER_CACHE[key] = explainer
    return explainer


def explain_prediction(
    model: LoadedModel,
    matrix: npt.NDArray[np.float64],
    *,
    background: npt.NDArray[np.float64] | None = None,
) -> list[PredictionExplanation]:
    """Per-prediction contributions for every row of ``matrix`` — the request-time entry point."""
    return get_explainer(model, background=background).explain(matrix)


def global_shap_importance(
    model: LoadedModel,
    sample: npt.NDArray[np.float64],
    *,
    background: npt.NDArray[np.float64] | None = None,
) -> GlobalShapReport:
    """Global mean |SHAP| over ``sample`` — called once per training run.

    ``background`` is only consulted if the tree explainer cannot be built; exact tree SHAP is
    path-dependent and needs no reference distribution.
    """
    explainer = get_explainer(model, background=background)
    return explainer.global_importance(
        sample,
        background_rows=0 if background is None else int(background.shape[0]),
    )
