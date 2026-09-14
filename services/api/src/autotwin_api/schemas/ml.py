"""Model-registry, evaluation and prediction payloads.

Every response here reports the **physical baseline beside the learned number**. That is the
project's central honesty commitment applied to machine learning: a single figure from a
gradient-boosted model trained on simulated labels is unfalsifiable, while the same figure next
to the transparent road-load model's answer tells the reader immediately whether the model is
correcting the physics or inventing a correction (BUILD_SPEC §10).

``protected_namespaces=()`` appears on the classes that carry ``model_name`` /
``model_version``. Pydantic's current default only reserves ``model_validate`` and
``model_dump``, but the reservation has been broader before and these field names are the
frontend's contract rather than ours — stating the exemption costs one line and removes the
chance of a future release turning the whole ``/ml`` page into warnings.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from autotwin_api.schemas.common import ApiModel
from autotwin_contracts import DataOrigin, RoadClass, TrafficSeverity

__all__ = [
    "FeatureImportanceEntry",
    "GlobalShapEntry",
    "GlobalShapReportOut",
    "MLMetrics",
    "MLModelInfo",
    "MetricsBlock",
    "PredictRequest",
    "PredictResponse",
    "PredictedVsActual",
    "ResidualBucket",
    "ShapContribution",
]


class MLModelInfo(ApiModel):
    """One trained version, as the registry describes it."""

    id: UUID = Field(
        ...,
        description=(
            "Registry row id. Derived deterministically from name and version when the artefact "
            "exists on disk but has not been registered in the database yet."
        ),
    )
    name: str = Field(..., description="Model name, e.g. `energy_consumption`.")
    version: str = Field(..., description="Integer version as a string, e.g. `1`.")
    algorithm: str = Field(
        ...,
        description="`lightgbm` or `sklearn_hist_gradient_boosting`.",
    )
    trained_at: datetime = Field(..., description="When training finished (UTC).")
    training_rows: int = Field(..., ge=0, description="Windows the model was fitted on.")
    feature_names: list[str] = Field(
        default_factory=list,
        description="The 20 features in the exact order the model expects (BUILD_SPEC §10.2).",
    )
    is_active: bool = Field(..., description="Whether this is the version being served.")
    training_data_origin: DataOrigin = Field(
        ...,
        description=(
            "Origin of the labels. `simulated` throughout: the model learns this platform's "
            "physics, not measurements of a real fleet."
        ),
    )
    notes: str | None = Field(default=None, description="Training summary written by the CLI.")


class MetricsBlock(ApiModel):
    """The six headline numbers, model and baseline on the same held-out test set."""

    mae: float = Field(..., description="Mean absolute error of the model, kWh/100 km.")
    rmse: float = Field(..., description="Root mean squared error of the model, kWh/100 km.")
    r2: float = Field(..., description="Coefficient of determination of the model.")
    baseline_mae: float = Field(..., description="MAE of the physical road-load model.")
    baseline_rmse: float = Field(..., description="RMSE of the physical road-load model.")
    baseline_r2: float = Field(..., description="R² of the physical road-load model.")


class PredictedVsActual(ApiModel):
    """One test-set point: what happened, what the model said, what the physics said."""

    actual: float = Field(..., description="Observed consumption, kWh/100 km.")
    predicted: float = Field(..., description="Model prediction for the same window.")
    baseline: float = Field(..., description="Physical baseline for the same window.")


class ResidualBucket(ApiModel):
    """One bin of the residual histogram."""

    bucket: float = Field(
        ...,
        description="Centre of the bin, in kWh/100 km. Negative means the model overestimated.",
    )
    count: int = Field(..., ge=0, description="Test-set windows that fell into the bin.")


class FeatureImportanceEntry(ApiModel):
    """How much one feature moves the prediction."""

    feature: str = Field(..., description="Feature name from `FEATURE_NAMES`.")
    importance: float = Field(
        ...,
        description=(
            "Mean |SHAP| in kWh/100 km where SHAP is available — the units of the target — "
            "falling back to the tree's normalised split gain when it is not."
        ),
    )
    label_de: str | None = Field(default=None, description="German label for the UI.")
    label_en: str | None = Field(default=None, description="English label for the UI.")


class MLMetrics(ApiModel):
    """Everything the `/ml` page renders for one model version.

    Read from the metrics sidecar that training wrote, not recomputed: the numbers must be the
    ones the model was actually evaluated with, on the split it was actually evaluated on, and
    an endpoint that re-scored the model on request could quietly report a different test set.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, protected_namespaces=())

    model: MLModelInfo | None = Field(
        default=None,
        description="The version these metrics belong to; null when nothing is trained.",
    )
    metrics: MetricsBlock | None = Field(
        default=None,
        description="Model and baseline scores on the same held-out test set.",
    )
    predicted_vs_actual: list[PredictedVsActual] = Field(
        default_factory=list,
        description="A sample of test-set points for the scatter plot.",
    )
    residuals: list[ResidualBucket] = Field(
        default_factory=list,
        description="Histogram of prediction errors, ascending by bin centre.",
    )
    feature_importance: list[FeatureImportanceEntry] = Field(
        default_factory=list,
        description="Global importance, most important first.",
    )
    mae_improvement_percent: float | None = Field(
        default=None,
        description="How much the model improves on the physical baseline's MAE, in percent.",
    )
    training_data_note: str | None = Field(
        default=None,
        description=(
            "The caveat that governs every number above: the labels are simulated, so these "
            "metrics demonstrate that the pipeline works, not that the model predicts a real "
            "vehicle."
        ),
    )


class GlobalShapEntry(ApiModel):
    """One feature's global SHAP importance."""

    feature: str = Field(..., description="Feature name.")
    mean_abs_shap: float = Field(
        ...,
        description="Mean |contribution| over the sample, in kWh/100 km.",
    )
    share: float = Field(..., description="Share of the total mean |SHAP| across features, 0-1.")
    rank: int = Field(..., ge=1, description="1-based rank, 1 being the most important.")
    label_de: str = Field(..., description="German label.")
    label_en: str = Field(..., description="English label.")
    insight_factor: str | None = Field(
        default=None,
        description="Matching driver id of the deterministic insight generator (BUILD_SPEC §11).",
    )


class GlobalShapReportOut(ApiModel):
    """Global feature attribution for one model version."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, protected_namespaces=())

    model_name: str = Field(..., description="Model the report belongs to.")
    model_version: str = Field(..., description="Version the report belongs to.")
    method: str = Field(..., description="`tree_explainer`, or the fallback that was used.")
    base_value: float = Field(
        ...,
        description="The explainer's expected value: the model's mean prediction, kWh/100 km.",
    )
    sample_rows: int = Field(..., ge=0, description="Rows the attribution was averaged over.")
    background_rows: int = Field(..., ge=0, description="Background rows, 0 for exact tree SHAP.")
    degraded_reason: str | None = Field(
        default=None,
        description="Why an approximation was used instead of exact tree SHAP, if it was.",
    )
    entries: list[GlobalShapEntry] = Field(
        default_factory=list,
        description="Features ordered by descending importance.",
    )


class ShapContribution(ApiModel):
    """One feature's signed contribution to one prediction."""

    feature: str = Field(..., description="Feature name.")
    value: float = Field(..., description="The feature's value for this prediction.")
    contribution: float = Field(
        ...,
        description="Signed SHAP value in kWh/100 km. Positive raised the prediction.",
    )
    direction: str = Field(..., description="`increase` or `decrease` — a rendering hint.")
    label_de: str = Field(..., description="German label.")
    label_en: str = Field(..., description="English label.")


class PredictRequest(ApiModel):
    """A consumption prediction request.

    Two ways to ask, and the difference matters for the baseline:

    * **By vehicle and conditions** (``vehicle_code`` plus the segment fields) — the physical
      baseline is then computed on the real vehicle profile and is exact.
    * **By raw feature vector** (``features``) — the 20 features do not include the rolling
      resistance coefficient, so the baseline has to be computed against a reconstructed
      vehicle and the response marks it ``baseline_is_approximate``.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, protected_namespaces=())

    vehicle_code: str | None = Field(
        default=None,
        description="Vehicle profile code, e.g. `compact_ev`. See `GET /api/v1/vehicles/profiles`.",
        examples=["compact_ev"],
    )
    speed_kmh: float | None = Field(
        default=None,
        gt=0.0,
        le=300.0,
        description="Average speed over the segment. Required unless `features` is given.",
        examples=[120.0],
    )
    distance_km: float = Field(
        default=1.0,
        gt=0.0,
        le=2000.0,
        description="Segment length, used to turn kWh/100 km into kWh for this segment.",
    )
    gradient_percent: float = Field(
        default=0.0,
        ge=-25.0,
        le=25.0,
        description="Average gradient in percent; positive is uphill.",
    )
    outside_temperature_c: float = Field(
        default=20.0,
        ge=-40.0,
        le=55.0,
        description="Ambient temperature in °C — the single largest driver in winter.",
    )
    battery_temperature_c: float | None = Field(
        default=None,
        ge=-40.0,
        le=80.0,
        description="Battery temperature in °C; defaults to a value derived from the ambient.",
    )
    soc_percent: float = Field(
        default=60.0,
        ge=0.0,
        le=100.0,
        description="State of charge at the start of the segment.",
    )
    acceleration_ms2: float = Field(
        default=0.0,
        ge=-8.0,
        le=8.0,
        description="Mean longitudinal acceleration in m/s².",
    )
    traffic_severity: TrafficSeverity = Field(
        default=TrafficSeverity.low,
        description="Congestion on the segment.",
    )
    precipitation_mm: float = Field(default=0.0, ge=0.0, description="Precipitation in mm.")
    wind_speed_ms: float = Field(default=0.0, ge=0.0, description="Wind speed in m/s.")
    headwind_ms: float = Field(
        default=0.0,
        description="Component of the wind against the direction of travel, m/s.",
    )
    road_class: RoadClass = Field(
        default=RoadClass.motorway,
        description="Road class; motorway is the case the corridor analysis cares about.",
    )
    speed_limit_kmh: float | None = Field(
        default=None,
        gt=0.0,
        description="Posted limit; defaults to the class's typical limit.",
    )
    features: dict[str, float] | None = Field(
        default=None,
        description=(
            "Complete feature vector, as an alternative to the fields above. Must contain every "
            "name in `FEATURE_NAMES`."
        ),
    )
    version: str | None = Field(
        default=None,
        description="Model version to use; defaults to the active one.",
    )
    explain: bool = Field(
        default=True,
        description="Whether to compute per-feature SHAP contributions for this prediction.",
    )

    @model_validator(mode="after")
    def _needs_one_input_shape(self) -> PredictRequest:
        """Require either a feature vector or enough of a segment to build one."""
        if self.features is None and self.speed_kmh is None:
            msg = "provide either `features` or `speed_kmh` (with the other segment fields)"
            raise ValueError(msg)
        return self


class PredictResponse(ApiModel):
    """A served prediction, its baseline and why the model said what it said."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True, protected_namespaces=())

    prediction_kwh_100km: float = Field(..., description="The model's estimate, kWh/100 km.")
    baseline_kwh_100km: float = Field(
        ...,
        description="The physical road-load model's estimate for the same segment.",
    )
    delta_kwh_100km: float = Field(
        ...,
        description="Model minus baseline. Positive means the model expects more than physics.",
    )
    delta_percent: float | None = Field(
        default=None,
        description="That delta as a percentage of the baseline; null if the baseline is zero.",
    )
    energy_kwh: float = Field(
        ...,
        description="Predicted energy for `distance_km` of this segment, in kWh.",
    )
    baseline_energy_kwh: float = Field(
        ...,
        description="Baseline energy for the same distance, in kWh.",
    )
    model_name: str = Field(..., description="Model that answered.")
    model_version: str = Field(..., description="Version that answered.")
    algorithm: str = Field(..., description="Backend that produced the artefact.")
    baseline_is_approximate: bool = Field(
        ...,
        description=(
            "True when the baseline used a vehicle reconstructed from the feature vector, "
            "because the rolling-resistance coefficient is not one of the 20 features."
        ),
    )
    features: dict[str, float] = Field(
        default_factory=dict,
        description="The exact vector the model saw — the audit trail of this answer.",
    )
    base_value: float | None = Field(
        default=None,
        description="SHAP expected value; `base_value + Σ contributions == prediction`.",
    )
    method: str | None = Field(default=None, description="How the contributions were computed.")
    contributions: list[ShapContribution] = Field(
        default_factory=list,
        description="Per-feature SHAP contributions, largest absolute first.",
    )
    prediction_id: UUID | None = Field(
        default=None,
        description="Id of the `ml_predictions` audit row written for this answer.",
    )
    generated_at: datetime = Field(..., description="When the prediction was served (UTC).")
