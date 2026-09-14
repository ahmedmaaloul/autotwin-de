"""Serving the energy model — ``POST /api/v1/ml/predict`` and the route analysis.

:class:`EnergyPredictor` answers with **both numbers, always**: the learned prediction and the
physical baseline for the same segment. That is not a debugging convenience, it is the
project's honesty rule made operational. A single number from a gradient-boosted model trained
on simulated labels is unfalsifiable; the same number next to the transparent road-load model's
answer tells the reader immediately whether the ML model is adding a correction or inventing
one, and ``RouteAnalysis`` (BUILD_SPEC §7.3) has fields for both for exactly that reason.

Two entry shapes
----------------

* :meth:`EnergyPredictor.predict` takes a :class:`~autotwin_contracts.vehicles.VehicleProfile`
  and a :class:`~autotwin_ml.types.SegmentConditions` — what the route analyser holds. The
  baseline is then the real physical model on the real vehicle, with no reconstruction.
* :meth:`EnergyPredictor.predict_features` takes a raw feature mapping — what an API client
  posts. The 20 features do **not** include the rolling-resistance coefficient, so the baseline
  has to be computed against a reconstructed profile and is flagged
  ``baseline_is_approximate``. Flagging it rather than hiding it is the point: a comparison
  whose two sides were computed under different assumptions must say so.

Nothing here caches anything of its own: the estimator is cached by
:mod:`autotwin_ml.registry` and the SHAP explainer by :mod:`autotwin_ml.explain`, both keyed on
the artefact's modification time, so a retrained model is picked up without a restart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
from numpy import typing as npt
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_contracts.vehicles import GENERIC_VEHICLE_PROFILES, VehicleProfile
from autotwin_core.config import Settings
from autotwin_core.db.models import MLPrediction
from autotwin_core.errors import ValidationError
from autotwin_core.logging import get_logger
from autotwin_ml.baseline import PhysicalEnergyModel
from autotwin_ml.explain import PredictionExplanation, explain_prediction
from autotwin_ml.features import (
    DEFAULT_SOC_PERCENT,
    FEATURE_NAMES,
    feature_batch,
    features_from_conditions,
)
from autotwin_ml.registry import LoadedModel, get_active_model
from autotwin_ml.types import SegmentConditions

__all__ = [
    "EnergyPredictor",
    "Prediction",
    "record_prediction",
]

_LOGGER = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Prediction:
    """One served prediction: the ML answer, the physical answer, and who produced them."""

    prediction_kwh_100km: float
    """The model's consumption estimate for this segment."""

    baseline_kwh_100km: float
    """The physical road-load model's estimate for the *same* segment."""

    model_name: str
    model_version: str
    algorithm: str

    features: dict[str, float]
    """The exact vector the model saw, for the ``ml_predictions`` audit row."""

    baseline_is_approximate: bool
    """True when the baseline used a reconstructed vehicle (see the module docstring)."""

    explanation: PredictionExplanation | None = None
    """Per-feature SHAP contributions, present only when the caller asked for them."""

    @property
    def delta_kwh_100km(self) -> float:
        """ML minus baseline. Positive means the model expects more than the physics does."""
        return self.prediction_kwh_100km - self.baseline_kwh_100km

    @property
    def delta_percent(self) -> float:
        """:attr:`delta_kwh_100km` as a percentage of the baseline; NaN if the baseline is 0."""
        if self.baseline_kwh_100km == 0.0:
            return float("nan")
        return self.delta_kwh_100km / self.baseline_kwh_100km * 100.0

    def energy_kwh(self, distance_km: float) -> float:
        """Predicted energy for ``distance_km`` — what the route analyser sums per segment."""
        return self.prediction_kwh_100km * distance_km / 100.0

    def baseline_energy_kwh(self, distance_km: float) -> float:
        """Baseline energy for ``distance_km``, so a route can report both totals."""
        return self.baseline_kwh_100km * distance_km / 100.0

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form — the body of ``POST /api/v1/ml/predict``."""
        return {
            "prediction_kwh_100km": self.prediction_kwh_100km,
            "baseline_kwh_100km": self.baseline_kwh_100km,
            "delta_kwh_100km": self.delta_kwh_100km,
            "delta_percent": self.delta_percent,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "algorithm": self.algorithm,
            "baseline_is_approximate": self.baseline_is_approximate,
            "features": dict(self.features),
            "explanation": self.explanation.as_dict() if self.explanation else None,
        }


class EnergyPredictor:
    """Serves consumption predictions from the active model, always beside the baseline."""

    __slots__ = ("_model", "_physics")

    def __init__(
        self,
        model: LoadedModel,
        *,
        physics: PhysicalEnergyModel | None = None,
    ) -> None:
        """Bind a loaded artefact and the physical model the comparison is made against."""
        self._model = model
        self._physics = physics or PhysicalEnergyModel()

    @classmethod
    def load(
        cls,
        version: str | None = None,
        *,
        name: str | None = None,
        model_dir: Path | None = None,
        settings: Settings | None = None,
        physics: PhysicalEnergyModel | None = None,
    ) -> EnergyPredictor:
        """Build a predictor on the active (or named) model version.

        Raises:
            ModelNotAvailable: When nothing is trained yet — a 503 with a message naming the
                command that fixes it, rather than an exception the API has to guess about.
        """
        return cls(
            get_active_model(version, name=name, model_dir=model_dir, settings=settings),
            physics=physics,
        )

    @property
    def model(self) -> LoadedModel:
        """The loaded artefact, for callers that need its metrics or record."""
        return self._model

    @property
    def name(self) -> str:
        """Model name — ``RouteAnalysis.model_name``."""
        return self._model.name

    @property
    def version(self) -> str:
        """Model version — ``RouteAnalysis.model_version``."""
        return self._model.version

    @property
    def algorithm(self) -> str:
        """Which backend produced the artefact."""
        return self._model.algorithm

    # -- prediction from domain objects --------------------------------------------------

    def predict(
        self,
        profile: VehicleProfile,
        conditions: SegmentConditions,
        *,
        soc_percent: float = DEFAULT_SOC_PERCENT,
        explain: bool = False,
        avg_speed_kmh: float | None = None,
        speed_std_kmh: float = 0.0,
        acceleration_abs_mean_ms2: float | None = None,
        accel_events_per_km: float = 0.0,
        segment_distance_km: float | None = None,
    ) -> Prediction:
        """Predict one segment for one vehicle, with the exact physical baseline beside it.

        The optional dispersion arguments are the within-window statistics of
        :func:`~autotwin_ml.features.features_from_conditions`, for a caller that aggregates
        real telemetry rather than analysing a homogeneous route segment. They are exposed on
        the single-segment call only: per-segment dispersion over a whole route is a *column*,
        not one value, and such a caller should build its own feature rows and go through
        :meth:`predict_features`.
        """
        features = features_from_conditions(
            profile,
            conditions,
            soc_percent=soc_percent,
            avg_speed_kmh=avg_speed_kmh,
            speed_std_kmh=speed_std_kmh,
            acceleration_abs_mean_ms2=acceleration_abs_mean_ms2,
            accel_events_per_km=accel_events_per_km,
            segment_distance_km=segment_distance_km,
        )
        matrix = feature_batch((features,))
        explanations = self._explanations(matrix) if explain else None
        return Prediction(
            prediction_kwh_100km=float(self._model.predict(matrix)[0]),
            baseline_kwh_100km=self._physics.consumption_kwh_per_100km(profile, conditions),
            model_name=self.name,
            model_version=self.version,
            algorithm=self.algorithm,
            features=features,
            baseline_is_approximate=False,
            explanation=None if explanations is None else explanations[0],
        )

    def predict_segments(
        self,
        profile: VehicleProfile,
        segments: Sequence[SegmentConditions],
        *,
        soc_percent: float = DEFAULT_SOC_PERCENT,
        explain: bool = False,
    ) -> list[Prediction]:
        """Predict a whole route in one model call — the route-analysis path.

        Batching matters on the flagship endpoint: a 400 km corridor is 80 segments, and 80
        separate ``predict`` calls into a boosted ensemble cost roughly an order of magnitude
        more than one call with an 80-row matrix.
        """
        if not segments:
            msg = "predict_segments needs at least one segment"
            raise ValidationError(msg, details={"segments": 0})
        rows = [
            features_from_conditions(profile, conditions, soc_percent=soc_percent)
            for conditions in segments
        ]
        matrix = feature_batch(rows)
        predictions = self._model.predict(matrix)
        explanations = self._explanations(matrix) if explain else None
        baselines = [
            self._physics.consumption_kwh_per_100km(profile, conditions) for conditions in segments
        ]
        return [
            Prediction(
                prediction_kwh_100km=float(predictions[index]),
                baseline_kwh_100km=baselines[index],
                model_name=self.name,
                model_version=self.version,
                algorithm=self.algorithm,
                features=rows[index],
                baseline_is_approximate=False,
                explanation=None if explanations is None else explanations[index],
            )
            for index in range(len(segments))
        ]

    # -- prediction from raw feature vectors ---------------------------------------------

    def predict_features(
        self,
        rows: Sequence[Mapping[str, float]],
        *,
        profile: VehicleProfile | None = None,
        explain: bool = False,
    ) -> list[Prediction]:
        """Predict from raw feature mappings — the shape an API client posts.

        When ``profile`` is given the baseline is exact. Otherwise a vehicle is reconstructed
        from ``mass_kg``, ``drag_area`` and ``nominal_consumption_kwh_100km``, borrowing the
        rolling-resistance coefficient of the closest generic profile because ``c_rr`` is not
        one of the 20 features. The resulting baseline is marked approximate.
        """
        matrix = feature_batch(rows)
        predictions = self._model.predict(matrix)
        explanations = self._explanations(matrix) if explain else None
        results: list[Prediction] = []
        for index, row in enumerate(rows):
            values = {name: float(row[name]) for name in FEATURE_NAMES}
            vehicle = profile or _reconstruct_profile(values)
            conditions = _conditions_from_features(values)
            baseline = self._physics.consumption_kwh_per_100km(vehicle, conditions)
            results.append(
                Prediction(
                    prediction_kwh_100km=float(predictions[index]),
                    baseline_kwh_100km=baseline,
                    model_name=self.name,
                    model_version=self.version,
                    algorithm=self.algorithm,
                    features=values,
                    baseline_is_approximate=profile is None,
                    explanation=None if explanations is None else explanations[index],
                )
            )
        return results

    def predict_matrix(self, matrix: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Raw model output for a prepared design matrix, with no baseline and no explanation.

        For bulk offline work — backfilling ``route_segments.predicted_kwh_per_100km`` over
        thousands of rows — where the per-row baseline would dominate the runtime and the
        caller computes it separately if it needs it.
        """
        return self._model.predict(matrix)

    def _explanations(self, matrix: npt.NDArray[np.float64]) -> list[PredictionExplanation] | None:
        """SHAP contributions for a batch, degrading to ``None`` rather than failing a request.

        A prediction is useful without its explanation; a 500 instead of a prediction is not.
        The failure is logged with the model label so it cannot pass unnoticed.
        """
        try:
            return explain_prediction(self._model, matrix)
        except Exception as exc:  # a SHAP failure must never fail the prediction itself
            _LOGGER.warning(
                "ml.explanation_failed",
                model=self._model.record.label,
                error=str(exc),
            )
            return None


# ======================================================================================
# Reconstructing a vehicle from a feature vector
# ======================================================================================


def _reconstruct_profile(values: Mapping[str, float]) -> VehicleProfile:
    """Build a physically usable vehicle from the three vehicle features in the vector.

    ``mass_kg``, ``drag_area`` and ``nominal_consumption_kwh_100km`` are reproduced exactly;
    the rolling-resistance coefficient and the frontal area are borrowed from the nearest
    generic profile, because neither is a feature and the road-load model needs both. The
    frontal area is chosen first and the drag coefficient then derived as
    ``drag_area / frontal_area``, so the product the physics actually uses is exact whatever
    the split between its factors.

    Nearest is measured in units of "how much would a reviewer call this a different car":
    500 kg of mass, 0.2 m² of drag area, 5 kWh/100 km of nominal consumption.
    """
    mass_kg = values["mass_kg"]
    drag_area = values["drag_area"]
    nominal = values["nominal_consumption_kwh_100km"]
    if mass_kg <= 0.0 or drag_area <= 0.0 or nominal <= 0.0:
        msg = (
            "mass_kg, drag_area and nominal_consumption_kwh_100km must all be positive to "
            "reconstruct a vehicle for the physical baseline"
        )
        raise ValidationError(
            msg,
            details={"mass_kg": mass_kg, "drag_area": drag_area, "nominal": nominal},
        )

    def distance(candidate: VehicleProfile) -> float:
        return (
            abs(candidate.mass_kg - mass_kg) / 500.0
            + abs(candidate.drag_area - drag_area) / 0.2
            + abs(candidate.nominal_consumption_kwh_100km - nominal) / 5.0
        )

    nearest = min(GENERIC_VEHICLE_PROFILES, key=distance)
    return VehicleProfile(
        code=f"reconstructed_{nearest.code}",
        display_name=f"Rekonstruiert ({nearest.display_name})",
        vehicle_class=nearest.vehicle_class,
        battery_capacity_kwh=nearest.battery_capacity_kwh,
        usable_capacity_kwh=nearest.usable_capacity_kwh,
        nominal_consumption_kwh_100km=nominal,
        max_dc_power_kw=nearest.max_dc_power_kw,
        max_ac_power_kw=nearest.max_ac_power_kw,
        mass_kg=mass_kg,
        drag_coefficient=drag_area / nearest.frontal_area_m2,
        frontal_area_m2=nearest.frontal_area_m2,
        rolling_resistance=nearest.rolling_resistance,
        is_generic=False,
    )


def _conditions_from_features(values: Mapping[str, float]) -> SegmentConditions:
    """Rebuild the segment a feature vector describes, for the physical baseline.

    The distance is normalised to 1 km and the duration left implicit, because consumption per
    100 km is invariant under that scaling and ``segment_distance_km`` describes the *road
    segment* the window sits in rather than the distance the window covered — using it here
    would silently change the aggregate.
    """
    speed_kmh = values["speed_kmh"]
    if speed_kmh <= 0.0:
        msg = (
            "a feature vector with speed_kmh <= 0 has no consumption per 100 km; "
            "a stationary window is not a predictable segment"
        )
        raise ValidationError(msg, details={"speed_kmh": speed_kmh})
    return SegmentConditions(
        speed_kmh=speed_kmh,
        distance_km=1.0,
        gradient_percent=values["gradient_percent"],
        outside_temperature_c=values["outside_temperature_c"],
        battery_temperature_c=values["battery_temperature_c"],
        acceleration_ms2=0.0,
        precipitation_mm=values["precipitation_mm"],
        wind_speed_ms=values["wind_speed_ms"],
        speed_limit_kmh=values["speed_limit_kmh"],
    )


# ======================================================================================
# Audit trail
# ======================================================================================


async def record_prediction(
    session: AsyncSession,
    prediction: Prediction,
    *,
    ml_model_id: UUID | None = None,
    route_id: UUID | None = None,
    trip_id: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> UUID:
    """Append one served prediction to ``ml_predictions`` and return its id.

    Append-only by design (BUILD_SPEC §3.2): the row is evidence of what the model answered at
    a point in time, and it stays valid after the route is re-planned or the model version is
    superseded. The SHAP contributions are stored when the caller computed them, so a
    prediction a user questions later can be re-explained without re-running the model.
    """
    row = MLPrediction(
        ml_model_id=ml_model_id,
        route_id=route_id,
        trip_id=trip_id,
        features=dict(prediction.features),
        prediction_kwh_100km=prediction.prediction_kwh_100km,
        baseline_kwh_100km=prediction.baseline_kwh_100km,
        shap_values=(
            prediction.explanation.as_dict() if prediction.explanation is not None else None
        ),
        context=dict(context) if context else None,
    )
    session.add(row)
    await session.flush()
    return row.id
