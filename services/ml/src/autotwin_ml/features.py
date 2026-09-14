"""Feature contract of the energy-consumption model (BUILD_SPEC §10.2).

:data:`FEATURE_NAMES` is the **single source of truth** for what the regressor sees and in
which order. Four independent places depend on that order agreeing:

* the training-set builder, which materialises the matrix from Parquet;
* the persisted artefact, whose booster has learned column *positions*, not names;
* the API's ``POST /api/v1/ml/predict``, which builds one vector per request;
* the SHAP explainer, which reports contributions per column index.

A silent reordering between any two of them produces a model that predicts confidently and
wrongly — the failure mode that is hardest to notice, because nothing raises. Hence a single
frozen tuple here, a hard validation pass over every frame before it reaches the model
(:func:`validate_feature_frame`), and a feature-name list stored *inside* the artefact so a
mismatch is caught at load time rather than at prediction time.

Two builders, one contract
--------------------------

:func:`build_feature_frame` goes from the training Parquet to the 20 columns.
:func:`features_from_conditions` goes from a :class:`~autotwin_ml.types.SegmentConditions`
(what the route analyser has in hand) to the same 20 values, without touching Parquet or a
dataframe. Both end in the same order and the same units, which is what lets a route-analysis
prediction be compared with a training row at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

import numpy as np
import polars as pl
from numpy import typing as npt

from autotwin_contracts.enums import RoadClass
from autotwin_contracts.vehicles import VehicleProfile
from autotwin_core.errors import ValidationError
from autotwin_ml.constants import DEFAULT_PHYSICS, PhysicsConstants
from autotwin_ml.types import SegmentConditions

__all__ = [
    "DEFAULT_SOC_PERCENT",
    "FEATURE_LABELS",
    "FEATURE_NAMES",
    "GROUP_COLUMN",
    "INSIGHT_FACTOR_BY_FEATURE",
    "TARGET_COLUMN",
    "build_feature_frame",
    "default_speed_limit_kmh",
    "feature_array",
    "feature_batch",
    "feature_matrix",
    "features_from_conditions",
    "validate_feature_frame",
]

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "speed_kmh",
    "avg_speed_kmh",
    "speed_std_kmh",
    "acceleration_abs_mean_ms2",
    "accel_events_per_km",
    "outside_temperature_c",
    "battery_temperature_c",
    "soc_percent",
    "road_class_ordinal",
    "speed_limit_kmh",
    "traffic_severity_ordinal",
    "precipitation_mm",
    "wind_speed_ms",
    "gradient_percent",
    "segment_distance_km",
    "mass_kg",
    "drag_area",
    "nominal_consumption_kwh_100km",
    "hvac_load_kw",
    "is_motorway",
)
"""The 20 model inputs, in the exact order of BUILD_SPEC §10.2. Never reorder this tuple.

Adding a feature is a **new model version**, not an edit: an artefact trained on 20 columns
cannot consume 21, and :func:`validate_feature_frame` plus the feature list embedded in the
artefact make that mismatch an error instead of a silent misprediction.
"""

TARGET_COLUMN: Final[str] = "energy_consumption_kwh_100km"
"""Regression target: window consumption in kWh/100 km."""

GROUP_COLUMN: Final[str] = "trip_id"
"""Grouping key of the train/validation/test split — see :mod:`autotwin_ml.dataset`."""

DEFAULT_SOC_PERCENT: Final[float] = 60.0
"""State of charge assumed when a caller predicts without knowing one.

Mid-pack rather than full or empty: the learned SOC effect in this model is weak (the physical
simulation that produced the labels has no SOC-dependent internal resistance), so the choice
moves the prediction very little — but it has to be *a* number, because the feature vector may
not contain a NaN. Callers that know the SOC pass it; the route analyser does.
"""

_DEFAULT_SPEED_LIMIT_KMH: Final[MappingProxyType[RoadClass, float]] = MappingProxyType(
    {
        RoadClass.motorway: 130.0,
        RoadClass.trunk: 120.0,
        RoadClass.primary: 100.0,
        RoadClass.secondary: 100.0,
        RoadClass.tertiary: 80.0,
        RoadClass.residential: 50.0,
        RoadClass.service: 30.0,
        RoadClass.unknown: 100.0,
    }
)
"""Fallback speed limit per road class, in km/h, for segments where OSM carries no ``maxspeed``.

German defaults (StVO §3 and the Autobahn *Richtgeschwindigkeit*): 50 inside built-up areas,
100 outside them on ordinary roads, and 130 on the Autobahn — which is an advisory speed, not
a limit, and is used here precisely because a feature vector cannot hold "unlimited". A model
served a NaN would be worse; a model told 130 on an unrestricted stretch is told the number
that describes the *typical* traffic there, and the actual speed driven enters separately
through ``speed_kmh``.
"""

FEATURE_LABELS: Final[MappingProxyType[str, tuple[str, str]]] = MappingProxyType(
    {
        "speed_kmh": ("Durchschnittsgeschwindigkeit", "Mean speed"),
        "avg_speed_kmh": ("Fahrgeschwindigkeit ohne Stillstand", "Running speed excluding stops"),
        "speed_std_kmh": ("Geschwindigkeitsschwankung", "Speed variability"),
        "acceleration_abs_mean_ms2": ("Mittlere Beschleunigung", "Mean absolute acceleration"),
        "accel_events_per_km": ("Beschleunigungsvorgänge je km", "Acceleration events per km"),
        "outside_temperature_c": ("Außentemperatur", "Outside temperature"),
        "battery_temperature_c": ("Batterietemperatur", "Battery temperature"),
        "soc_percent": ("Ladezustand", "State of charge"),
        "road_class_ordinal": ("Straßenklasse", "Road class"),
        "speed_limit_kmh": ("Tempolimit", "Speed limit"),
        "traffic_severity_ordinal": ("Verkehrslage", "Traffic conditions"),
        "precipitation_mm": ("Niederschlag", "Precipitation"),
        "wind_speed_ms": ("Windgeschwindigkeit", "Wind speed"),
        "gradient_percent": ("Steigung", "Gradient"),
        "segment_distance_km": ("Segmentlänge", "Segment length"),
        "mass_kg": ("Fahrzeugmasse", "Vehicle mass"),
        "drag_area": ("Luftwiderstandsfläche c_d·A", "Drag area c_d·A"),
        "nominal_consumption_kwh_100km": ("Normverbrauch", "Nominal consumption"),
        "hvac_load_kw": ("Klimatisierungsleistung", "Climate control load"),
        "is_motorway": ("Autobahnfahrt", "Motorway driving"),
    }
)
"""Bilingual labels for the 20 features, for SHAP contributions rendered in the UI.

Where a feature corresponds to a driver of the deterministic insight generator, the German
wording is kept identical to :mod:`autotwin_ml.insights` (``Verkehrslage``, ``Steigung``,
``Tempolimit``) so that a SHAP contribution and a counterfactual driver naming the same cause
read as the same cause on the page. :data:`INSIGHT_FACTOR_BY_FEATURE` is the machine-readable
side of that correspondence.
"""

INSIGHT_FACTOR_BY_FEATURE: Final[MappingProxyType[str, str]] = MappingProxyType(
    {
        "speed_kmh": "speed_profile",
        "avg_speed_kmh": "speed_profile",
        "speed_std_kmh": "speed_profile",
        "acceleration_abs_mean_ms2": "speed_profile",
        "accel_events_per_km": "speed_profile",
        "road_class_ordinal": "speed_profile",
        "speed_limit_kmh": "speed_profile",
        "is_motorway": "speed_profile",
        "outside_temperature_c": "temperature",
        "battery_temperature_c": "temperature",
        "hvac_load_kw": "temperature",
        "traffic_severity_ordinal": "traffic",
        "gradient_percent": "gradient",
        "wind_speed_ms": "wind",
    }
)
"""Feature → ``insights`` factor id, for folding SHAP contributions into the driver list.

Six features are deliberately absent. ``soc_percent``, ``segment_distance_km``,
``precipitation_mm``, ``mass_kg``, ``drag_area`` and ``nominal_consumption_kwh_100km`` describe
the vehicle or the observation window rather than a *driver the trip could have avoided*, and
the counterfactual ladder of BUILD_SPEC §11 has no rung for them. A consumer aggregating SHAP
by factor must therefore treat a missing key as "not attributable to a named driver" rather
than assuming the mapping is total.
"""

_ACCELERATION_EVENT_THRESHOLD_MS2: Final[float] = 0.5
"""|a| above which a telemetry sample counts as an acceleration event; see the dataset builder.

Documented here rather than in the builder because the *feature* is only comparable across
datasets if every producer of ``accel_events_per_km`` uses the same threshold. 0.5 m/s² is the
usual cut in drive-cycle analysis: below it a passenger cannot feel the change, above it the
traction inverter is doing real work.
"""


def default_speed_limit_kmh(road_class: RoadClass) -> float:
    """Assumed speed limit in km/h for a segment whose limit is unknown.

    See :data:`_DEFAULT_SPEED_LIMIT_KMH` for the German defaults this encodes and why an
    unrestricted Autobahn stretch is reported as 130 rather than as a missing value.
    """
    return _DEFAULT_SPEED_LIMIT_KMH[road_class]


def _require_columns(frame: pl.DataFrame, context: str) -> None:
    """Every feature of :data:`FEATURE_NAMES` must be present, or name the ones that are not."""
    missing = [name for name in FEATURE_NAMES if name not in frame.columns]
    if missing:
        msg = f"{context} is missing required feature column(s): {', '.join(missing)}"
        raise ValidationError(msg, details={"missing_features": missing, "context": context})


def _require_rows(frame: pl.DataFrame, context: str) -> None:
    """An empty frame is caught before the dtype check, because a frame with no rows has no
    meaningful dtypes and "column 'speed_kmh' has dtype Null" is a confusing way to say it."""
    if frame.height == 0:
        msg = f"{context} has no rows; there is nothing to predict on"
        raise ValidationError(msg, details={"context": context})


def _require_numeric(frame: pl.DataFrame, context: str) -> None:
    """Every feature column must already be numeric; strings are a caller error, not a cast."""
    schema = frame.schema
    for name in FEATURE_NAMES:
        dtype = schema[name]
        if not dtype.is_numeric():
            msg = (
                f"{context} column {name!r} has non-numeric dtype {dtype}; "
                "features must be numeric — map enums through their .ordinal first"
            )
            raise ValidationError(msg, details={"feature": name, "dtype": str(dtype)})


def validate_feature_frame(frame: pl.DataFrame, *, context: str = "feature frame") -> None:
    """Fail loudly if ``frame`` cannot be fed to the model, naming the offending column.

    Checks, in order: every name of :data:`FEATURE_NAMES` is present; each is numeric; none
    contains a null, a NaN or an infinity. The order matters — reporting "column
    ``wind_speed_ms`` holds 3 null value(s)" is actionable, while letting LightGBM impute the
    nulls away and return a number is exactly the "garbage in, confident out" behaviour
    BUILD_SPEC §0.4 forbids.

    Raises:
        ValidationError: With ``details`` carrying the column name, so the API's exception
            handler surfaces it in the error envelope instead of a 500.
    """
    _require_columns(frame, context)
    _require_rows(frame, context)
    _require_numeric(frame, context)

    for name in FEATURE_NAMES:
        column = frame.get_column(name)
        null_count = column.null_count()
        if null_count:
            msg = f"{context} column {name!r} holds {null_count} null value(s)"
            raise ValidationError(msg, details={"feature": name, "null_count": null_count})

        values = column.cast(pl.Float64)
        nan_count = int(values.is_nan().sum())
        if nan_count:
            msg = f"{context} column {name!r} holds {nan_count} NaN value(s)"
            raise ValidationError(msg, details={"feature": name, "nan_count": nan_count})
        infinite_count = int(values.is_infinite().sum())
        if infinite_count:
            msg = f"{context} column {name!r} holds {infinite_count} infinite value(s)"
            raise ValidationError(msg, details={"feature": name, "infinite_count": infinite_count})


def build_feature_frame(frame: pl.DataFrame, *, context: str = "training frame") -> pl.DataFrame:
    """Project ``frame`` onto the 20 feature columns, in order, as ``Float64``.

    The cast to a single dtype is not cosmetic: ``road_class_ordinal`` and ``is_motorway``
    arrive as integers from Parquet, and a mixed-dtype frame converted to NumPy yields an
    ``object`` array that LightGBM rejects with a message about the wrong shape rather than the
    wrong dtype.

    Validation runs on the projected frame, so an error names a *feature*, which is what the
    caller can act on.
    """
    _require_columns(frame, context)
    _require_rows(frame, context)
    # Dtypes are checked *before* the cast: a strict cast of a string column raises polars'
    # own InvalidOperationError, which names the column but reaches the API as an unhandled
    # 500 instead of the 422 this contract promises.
    _require_numeric(frame, context)
    projected = frame.select(
        [pl.col(name).cast(pl.Float64, strict=True).alias(name) for name in FEATURE_NAMES]
    )
    validate_feature_frame(projected, context=context)
    return projected


def feature_matrix(
    frame: pl.DataFrame, *, context: str = "training frame"
) -> npt.NDArray[np.float64]:
    """Build the validated ``(n_rows, 20)`` ``float64`` design matrix from ``frame``."""
    return np.asarray(build_feature_frame(frame, context=context).to_numpy(), dtype=np.float64)


def features_from_conditions(
    profile: VehicleProfile,
    conditions: SegmentConditions,
    *,
    soc_percent: float = DEFAULT_SOC_PERCENT,
    avg_speed_kmh: float | None = None,
    speed_std_kmh: float = 0.0,
    acceleration_abs_mean_ms2: float | None = None,
    accel_events_per_km: float = 0.0,
    segment_distance_km: float | None = None,
    constants: PhysicsConstants = DEFAULT_PHYSICS,
) -> dict[str, float]:
    """Build one feature vector from a vehicle profile and one segment's conditions.

    This is the path the API takes: a route analysis has segments, not Parquet rows, and it
    must be able to ask the model for a prediction without a dataframe anywhere in the request.

    **Where the dispersion features come from.** ``SegmentConditions`` describes a *homogeneous*
    stretch — one mean speed, one gradient — so it carries no information about how much the
    speed varied inside it. The four dispersion features therefore default to the
    steady-state answer (no variability, no acceleration events, running speed equal to mean
    speed) and are overridable by a caller that does know better, such as the simulator
    aggregating a real 60 s telemetry window. Defaulting them to zero is the honest choice: it
    tells the model "this is a steady cruise", which is precisely the assumption the route
    analyser made when it derived ``assumed_speed_kmh``.

    Args:
        profile: Vehicle, supplying mass, drag area and nominal consumption.
        conditions: The segment. Its ``road_class``, ``traffic_severity`` and temperatures are
            mapped onto the ordinals and loads the model was trained on.
        soc_percent: State of charge in percent; see :data:`DEFAULT_SOC_PERCENT`.
        avg_speed_kmh: Running speed excluding standstill. Defaults to the mean speed.
        speed_std_kmh: Standard deviation of the instantaneous speeds in the window.
        acceleration_abs_mean_ms2: Mean |a|. Defaults to ``abs(conditions.acceleration_ms2)``,
            which is the net acceleration and therefore a lower bound.
        accel_events_per_km: Count of samples with ``|a| > 0.5 m/s²`` per kilometre.
        segment_distance_km: Length of the homogeneous road segment this window belongs to.
            Defaults to the segment's own distance, which is the right answer whenever the
            caller analyses whole segments rather than windows inside them.
        constants: Physics constants used to evaluate the HVAC load feature. Pass the same set
            the training data was produced with.

    Returns:
        A mapping keyed by :data:`FEATURE_NAMES`; feed it to :func:`feature_array`.
    """
    speed_kmh = max(0.0, conditions.speed_kmh)
    limit_kmh = (
        conditions.speed_limit_kmh
        if conditions.speed_limit_kmh is not None
        else default_speed_limit_kmh(conditions.road_class)
    )
    return {
        "speed_kmh": speed_kmh,
        "avg_speed_kmh": speed_kmh if avg_speed_kmh is None else max(0.0, avg_speed_kmh),
        "speed_std_kmh": max(0.0, speed_std_kmh),
        "acceleration_abs_mean_ms2": (
            abs(conditions.acceleration_ms2)
            if acceleration_abs_mean_ms2 is None
            else abs(acceleration_abs_mean_ms2)
        ),
        "accel_events_per_km": max(0.0, accel_events_per_km),
        "outside_temperature_c": conditions.outside_temperature_c,
        "battery_temperature_c": conditions.effective_battery_temperature_c,
        "soc_percent": soc_percent,
        "road_class_ordinal": float(conditions.road_class.ordinal),
        "speed_limit_kmh": limit_kmh,
        "traffic_severity_ordinal": float(conditions.traffic_severity.ordinal),
        "precipitation_mm": max(0.0, conditions.precipitation_mm),
        "wind_speed_ms": max(0.0, conditions.wind_speed_ms),
        "gradient_percent": conditions.gradient_percent,
        "segment_distance_km": (
            conditions.distance_km if segment_distance_km is None else max(0.0, segment_distance_km)
        ),
        "mass_kg": profile.mass_kg,
        "drag_area": profile.drag_area,
        "nominal_consumption_kwh_100km": profile.nominal_consumption_kwh_100km,
        "hvac_load_kw": constants.hvac_power_kw(conditions.outside_temperature_c),
        "is_motorway": 1.0 if conditions.road_class is RoadClass.motorway else 0.0,
    }


def feature_array(features: Mapping[str, float]) -> npt.NDArray[np.float64]:
    """Turn one feature mapping into the ``(1, 20)`` array the estimator expects.

    Raises:
        ValidationError: If a feature is missing or is not a finite number. Unknown *extra*
            keys are ignored — a caller may pass a richer context object — but a missing key
            is fatal, because silently substituting a zero would move the prediction without
            telling anyone.
    """
    return feature_batch((features,))


def feature_batch(rows: Sequence[Mapping[str, float]]) -> npt.NDArray[np.float64]:
    """Turn a sequence of feature mappings into the ``(n, 20)`` design matrix.

    Raises:
        ValidationError: Naming the first offending row index and feature.
    """
    if not rows:
        msg = "feature batch is empty; there is nothing to predict on"
        raise ValidationError(msg, details={"rows": 0})
    matrix = np.empty((len(rows), len(FEATURE_NAMES)), dtype=np.float64)
    for row_index, row in enumerate(rows):
        for column_index, name in enumerate(FEATURE_NAMES):
            if name not in row:
                msg = f"feature {name!r} is missing from row {row_index}"
                raise ValidationError(msg, details={"feature": name, "row_index": row_index})
            value = float(row[name])
            if not np.isfinite(value):
                msg = f"feature {name!r} in row {row_index} is {value!r}, which is not finite"
                raise ValidationError(msg, details={"feature": name, "row_index": row_index})
            matrix[row_index, column_index] = value
    return matrix
