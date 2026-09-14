"""Training dataset: load, clean, and split **grouped by trip** (BUILD_SPEC §10.2).

One row of ``data/gold/training/energy_windows.parquet`` is one aggregated telemetry window —
by default 60 simulated seconds inside a single trip. The simulator writes the file; this
module reads it, throws out the rows that cannot be true, and cuts it into 70/15/15 folds.

Why the split is grouped by ``trip_id``
---------------------------------------

Consecutive windows of one trip share almost everything: the same vehicle, the same weather,
the same driver, often the same stretch of Autobahn a minute apart. Their consumption values
are therefore strongly autocorrelated. A *random row* split puts window 41 of a trip in the
training set and window 42 in the test set, and the model is then scored on rows whose answer
it has effectively already seen. The reported R² goes up, the model gets no better, and the
number stops meaning "how well does this generalise to a trip it has never seen".

Grouping by trip closes that leak: a trip is wholly in train, wholly in validation, or wholly
in test. The folds are then assembled to hit the requested **row** proportions rather than
splitting the trip *count* 70/15/15, because trips differ in length and equal trip counts would
give unequal fold sizes.

This project measured the difference rather than asserting it — see ``docs/ml/methodology.md``
for the random-split versus grouped-split numbers on the same data.

The physics sweep
-----------------

:func:`generate_physics_sweep` produces a dataset in exactly the same schema **when the
simulator has not run yet**, so the ML pipeline can be built and validated independently. It
is not a stand-in that pretends to be simulator output: it writes a sidecar
``energy_windows.source.json`` recording what produced the file, the sidecar is checksum-bound
to the Parquet, and the training metrics carry the provenance through to the model registry.
Everything about it is documented in ``docs/ml/methodology.md`` under LIMITATIONS.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import numpy as np
import polars as pl
from numpy import typing as npt
from numpy.random import Generator, default_rng

from autotwin_contracts.enums import DataOrigin, RoadClass, TrafficSeverity
from autotwin_contracts.temporal import utc_now
from autotwin_contracts.vehicles import GENERIC_VEHICLE_PROFILES, VehicleProfile
from autotwin_core.errors import NotFoundError, ValidationError
from autotwin_core.logging import get_logger
from autotwin_ml.baseline import PhysicalEnergyModel
from autotwin_ml.constants import KMH_PER_MS, MS_PER_KMH, SECONDS_PER_HOUR
from autotwin_ml.features import FEATURE_NAMES, GROUP_COLUMN, TARGET_COLUMN
from autotwin_ml.types import SegmentConditions

__all__ = [
    "DEFAULT_SPLIT_RATIOS",
    "DEFAULT_TRAINING_PATH",
    "TRAINING_SCHEMA",
    "CleaningReport",
    "DatasetSplit",
    "TrainingDataSource",
    "clean_training_frame",
    "describe_training_source",
    "generate_physics_sweep",
    "load_dataset",
    "load_training_frame",
    "split_by_trip",
]

_LOGGER = get_logger(__name__)

DEFAULT_TRAINING_PATH: Final[Path] = Path("data/gold/training/energy_windows.parquet")
"""Where the simulator writes the training set and where the trainer looks for it."""

_SOURCE_SIDECAR_SUFFIX: Final[str] = ".source.json"
"""Sidecar naming: ``energy_windows.parquet`` → ``energy_windows.source.json``."""

DEFAULT_SPLIT_RATIOS: Final[tuple[float, float, float]] = (0.70, 0.15, 0.15)
"""Train / validation / test **row** proportions (BUILD_SPEC §10.2)."""

TRAINING_SCHEMA: Final[MappingProxyType[str, pl.DataType]] = MappingProxyType(
    {
        "trip_id": pl.String(),
        "vehicle_id": pl.String(),
        "vehicle_code": pl.String(),
        "window_start": pl.Datetime(time_unit="us", time_zone="UTC"),
        "window_end": pl.Datetime(time_unit="us", time_zone="UTC"),
        "distance_km": pl.Float64(),
        "duration_s": pl.Float64(),
        "speed_kmh": pl.Float64(),
        "avg_speed_kmh": pl.Float64(),
        "speed_std_kmh": pl.Float64(),
        "acceleration_abs_mean_ms2": pl.Float64(),
        "accel_events_per_km": pl.Float64(),
        "outside_temperature_c": pl.Float64(),
        "battery_temperature_c": pl.Float64(),
        "soc_percent": pl.Float64(),
        "road_class": pl.String(),
        "road_class_ordinal": pl.Int64(),
        "speed_limit_kmh": pl.Float64(),
        "traffic_severity": pl.String(),
        "traffic_severity_ordinal": pl.Int64(),
        "precipitation_mm": pl.Float64(),
        "wind_speed_ms": pl.Float64(),
        "gradient_percent": pl.Float64(),
        "segment_distance_km": pl.Float64(),
        "mass_kg": pl.Float64(),
        "drag_area": pl.Float64(),
        "nominal_consumption_kwh_100km": pl.Float64(),
        "hvac_load_kw": pl.Float64(),
        "is_motorway": pl.Int64(),
        "energy_kwh": pl.Float64(),
        "energy_consumption_kwh_100km": pl.Float64(),
        "data_origin": pl.String(),
    }
)
"""The shared Parquet contract: column name → dtype, in write order.

Both halves of the seam depend on it — the simulator's ``generate-training-data`` writes it and
this module reads it — so it lives in one place and both sides are validated against it.
"""

_MAX_PLAUSIBLE_CONSUMPTION_KWH_100KM: Final[float] = 120.0
"""Above this a target is a defect, not a heavy vehicle: a van crawling through a winter city
peaks near 45 kWh/100 km, and the worst case a 60 s window can produce is a near-standstill
with the heater on, which this bound still clears by a factor of two."""

_MIN_PLAUSIBLE_CONSUMPTION_KWH_100KM: Final[float] = 1.0
"""Below this a window is a sustained descent the model cannot represent, not light driving.

The road-load model has no state-of-charge-dependent charge-acceptance limit, so on a long
steep descent the recuperation credit cancels the whole window and the target collapses towards
zero. Such a row carries no learnable relationship — the label says "this stretch was free" —
and training on it teaches the regressor to predict near-zero consumption from a steep negative
gradient. The rows are rare (well under 1 % of a sweep) and dropping them is honest, because
what is wrong with them is the *model*, not the aggregation."""

_MAX_PLAUSIBLE_SPEED_KMH: Final[float] = 250.0
"""A 60 s window averaging above this is corrupt telemetry, not an Autobahn run."""


# ======================================================================================
# Provenance of the training file
# ======================================================================================


@dataclass(frozen=True, slots=True)
class TrainingDataSource:
    """What produced the training Parquet — carried into the model registry.

    The distinction between simulator output and a physics sweep is the difference between
    "the ML pipeline learned the residual of the physical model over simulated driving" and
    "the ML pipeline learned the residual of the physical model over a synthetic sweep built
    for exactly that purpose". Both are honest; conflating them is not, and the metrics JSON
    and the ``ml_models`` row therefore both record which one it was.
    """

    kind: str
    """``"physics_sweep"``, ``"simulator"`` or ``"unknown"``."""

    detail: Mapping[str, Any]
    """Everything the sidecar recorded: seed, trip count, generator version, timestamps."""

    verified: bool
    """True when a sidecar was found **and** its checksum matches the Parquet on disk.

    A stale sidecar — the simulator overwrote the file after a sweep was generated — is
    reported as ``kind="unknown"`` rather than trusted, because a wrong provenance claim is
    worse than a missing one."""

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form for the metrics file and the ``ml_models.metrics`` column."""
        return {"kind": self.kind, "verified": self.verified, "detail": dict(self.detail)}


def _sidecar_path(parquet_path: Path) -> Path:
    """Path of the provenance sidecar belonging to ``parquet_path``."""
    return parquet_path.with_suffix("").with_suffix(_SOURCE_SIDECAR_SUFFIX)


def _sha256(path: Path) -> str:
    """Hex SHA-256 of a file, read in chunks so a large Parquet does not land in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_training_source(path: Path | None = None) -> TrainingDataSource:
    """Read the provenance sidecar of the training Parquet, verifying its checksum.

    Returns ``kind="unknown"`` — never a guess — when no sidecar exists or when the recorded
    checksum does not match the file. The second case is the important one: it is exactly what
    happens after the simulator overwrites a previously generated sweep, and reporting the
    stale sidecar's claim then would attach a false provenance to a real training run.

    An unknown *kind* is still not an unknown *file*. The fingerprint — path, size, SHA-256 and
    modification time — is always recorded, so a model whose provenance cannot be named can at
    least be tied to the exact bytes it was trained on. The simulator does not currently write a
    sidecar of its own, so this is the normal state for a model trained on simulator output.
    """
    parquet_path = path or DEFAULT_TRAINING_PATH
    if not parquet_path.is_file():
        return TrainingDataSource(kind="unknown", detail={}, verified=False)

    stat = parquet_path.stat()
    fingerprint: dict[str, Any] = {
        "path": str(parquet_path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        "parquet_sha256": _sha256(parquet_path),
    }

    sidecar = _sidecar_path(parquet_path)
    if not sidecar.is_file():
        return TrainingDataSource(kind="unknown", detail=fingerprint, verified=False)
    try:
        payload: Any = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _LOGGER.warning("dataset.sidecar_unreadable", path=str(sidecar))
        return TrainingDataSource(kind="unknown", detail=fingerprint, verified=False)
    if not isinstance(payload, dict):
        return TrainingDataSource(kind="unknown", detail=fingerprint, verified=False)

    matches = payload.get("parquet_sha256") == fingerprint["parquet_sha256"]
    if not matches:
        _LOGGER.warning(
            "dataset.sidecar_stale",
            path=str(sidecar),
            claimed_kind=payload.get("kind"),
        )
        return TrainingDataSource(kind="unknown", detail=fingerprint, verified=False)
    return TrainingDataSource(
        kind=str(payload.get("kind", "unknown")), detail=payload, verified=True
    )


# ======================================================================================
# Loading and cleaning
# ======================================================================================


@dataclass(frozen=True, slots=True)
class CleaningReport:
    """How many rows each rejection rule removed, in the order the rules ran.

    Attribution is first-failure: a row that is both zero-distance and null-target counts once,
    against ``zero_or_negative_distance``. That keeps the counts summing to
    ``rows_in - rows_out``, which is the property that makes the report auditable.
    """

    rows_in: int
    rows_out: int
    rejected: Mapping[str, int]

    @property
    def rows_rejected(self) -> int:
        """Total rows dropped."""
        return self.rows_in - self.rows_out

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form, stored in the metrics file."""
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_rejected": self.rows_rejected,
            "rejected_by_rule": {key: value for key, value in self.rejected.items() if value},
        }


def load_training_frame(path: Path | None = None) -> pl.DataFrame:
    """Read the training Parquet.

    Raises:
        NotFoundError: When the file does not exist, naming both ways to produce one — the
            simulator (the real source) and the physics sweep (the stand-in). A bare
            ``FileNotFoundError`` here would be the single most common first-run failure, and
            an error that tells the reader the next command is worth the three extra lines.
        ValidationError: When mandatory columns of :data:`TRAINING_SCHEMA` are absent, which
            means the file was written by something that does not implement the shared
            contract.
    """
    parquet_path = path or DEFAULT_TRAINING_PATH
    if not parquet_path.is_file():
        msg = (
            f"training dataset not found at {parquet_path}. Produce one with "
            "`python -m autotwin_simulator.cli generate-training-data` (simulated telemetry, "
            "the real source) or `python -m autotwin_ml.cli generate-data` (physics sweep, "
            "the documented stand-in used when the simulator has not run yet)."
        )
        raise NotFoundError(msg, details={"path": str(parquet_path)})

    frame = pl.read_parquet(parquet_path)
    missing = [name for name in TRAINING_SCHEMA if name not in frame.columns]
    if missing:
        msg = (
            f"{parquet_path} does not implement the training-set contract; "
            f"missing column(s): {', '.join(missing)}"
        )
        raise ValidationError(msg, details={"path": str(parquet_path), "missing": missing})
    _LOGGER.info(
        "dataset.loaded",
        path=str(parquet_path),
        rows=frame.height,
        trips=frame.get_column(GROUP_COLUMN).n_unique(),
    )
    return frame


def clean_training_frame(frame: pl.DataFrame) -> tuple[pl.DataFrame, CleaningReport]:
    """Drop rows that cannot be true, returning the survivors and an auditable report.

    The rules encode what the physics and the database schema already guarantee, so anything
    they catch is a defect upstream — an aggregation over an empty window, a ``-999`` DWD
    sentinel that escaped conversion, a duplicated window after a consumer replay. Dropping is
    the right response rather than imputing: the dataset is large, and a fabricated feature
    value would teach the model a relationship nobody observed.
    """
    rows_in = frame.height
    rejected: dict[str, int] = {}
    working = frame

    def apply(rule: str, keep: pl.Expr) -> None:
        nonlocal working
        before = working.height
        working = working.filter(keep)
        rejected[rule] = before - working.height

    numeric_columns = [*FEATURE_NAMES, TARGET_COLUMN, "distance_km", "duration_s", "energy_kwh"]

    apply(
        "null_identifier", pl.col(GROUP_COLUMN).is_not_null() & pl.col("vehicle_code").is_not_null()
    )
    apply(
        "null_numeric",
        pl.all_horizontal([pl.col(name).is_not_null() for name in numeric_columns]),
    )
    apply(
        "non_finite_numeric",
        pl.all_horizontal([pl.col(name).cast(pl.Float64).is_finite() for name in numeric_columns]),
    )
    apply("zero_or_negative_distance", pl.col("distance_km") > 0.0)
    apply("zero_or_negative_duration", pl.col("duration_s") > 0.0)
    apply("non_positive_target", pl.col(TARGET_COLUMN) > 0.0)
    apply("implausible_low_target", pl.col(TARGET_COLUMN) >= _MIN_PLAUSIBLE_CONSUMPTION_KWH_100KM)
    apply(
        "implausible_target",
        pl.col(TARGET_COLUMN) <= _MAX_PLAUSIBLE_CONSUMPTION_KWH_100KM,
    )
    apply(
        "implausible_speed",
        (pl.col("speed_kmh") >= 0.0) & (pl.col("speed_kmh") <= _MAX_PLAUSIBLE_SPEED_KMH),
    )
    apply("soc_out_of_range", pl.col("soc_percent").is_between(0.0, 100.0))
    apply(
        "implausible_temperature",
        pl.col("outside_temperature_c").is_between(-40.0, 55.0)
        & pl.col("battery_temperature_c").is_between(-40.0, 80.0),
    )
    apply("negative_energy", pl.col("energy_kwh") >= 0.0)

    before_duplicates = working.height
    working = working.unique(
        subset=[GROUP_COLUMN, "window_start"], keep="first", maintain_order=True
    )
    rejected["duplicate_window"] = before_duplicates - working.height

    report = CleaningReport(rows_in=rows_in, rows_out=working.height, rejected=rejected)
    if report.rows_rejected:
        _LOGGER.info("dataset.cleaned", **report.as_dict())
    return working, report


# ======================================================================================
# Grouped split
# ======================================================================================


@dataclass(frozen=True, slots=True)
class DatasetSplit:
    """Three disjoint folds, no ``trip_id`` appearing in more than one of them."""

    train: pl.DataFrame
    validation: pl.DataFrame
    test: pl.DataFrame
    seed: int
    ratios: tuple[float, float, float]
    cleaning: CleaningReport
    source_path: Path
    data_origins: tuple[str, ...]
    """Distinct ``data_origin`` values found in the data — expected to be ``("simulated",)``."""

    @property
    def feature_count(self) -> int:
        """Number of model inputs; constant, but read from the contract rather than hardcoded."""
        return len(FEATURE_NAMES)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready description of the split, stored beside the metrics."""
        return {
            "strategy": f"grouped_by_{GROUP_COLUMN}",
            "seed": self.seed,
            "ratios": list(self.ratios),
            "rows": {
                "train": self.train.height,
                "validation": self.validation.height,
                "test": self.test.height,
            },
            "trips": {
                "train": self.train.get_column(GROUP_COLUMN).n_unique(),
                "validation": self.validation.get_column(GROUP_COLUMN).n_unique(),
                "test": self.test.get_column(GROUP_COLUMN).n_unique(),
            },
            "source_path": str(self.source_path),
            "data_origins": list(self.data_origins),
            "cleaning": self.cleaning.as_dict(),
        }


def split_by_trip(
    frame: pl.DataFrame,
    *,
    seed: int,
    ratios: tuple[float, float, float] = DEFAULT_SPLIT_RATIOS,
    cleaning: CleaningReport | None = None,
    source_path: Path | None = None,
) -> DatasetSplit:
    """Split ``frame`` into train/validation/test folds that never share a ``trip_id``.

    The trips are shuffled once with a seeded :class:`numpy.random.Generator` and then walked
    in that order, each being assigned to the fold that is furthest below its target **row**
    count. Assigning by rows rather than by trip count matters because a 90-window Autobahn run
    and a 20-window city trip are not interchangeable units; equal trip counts would leave the
    test fold systematically smaller or larger than 15 % of the data.

    Raises:
        ValidationError: If the frame is empty, if the ratios do not sum to 1, or if there are
            fewer than three trips — a grouped split of two trips cannot produce three folds,
            and returning two empty folds would let training "succeed" against nothing.
    """
    if abs(sum(ratios) - 1.0) > 1e-9:
        msg = f"split ratios must sum to 1.0, got {ratios!r}"
        raise ValidationError(msg, details={"ratios": list(ratios)})
    if frame.height == 0:
        msg = "cannot split an empty training frame"
        raise ValidationError(msg, details={"rows": 0})

    trip_sizes = (
        frame.group_by(GROUP_COLUMN)
        .len(name="rows")
        .sort(GROUP_COLUMN)  # deterministic starting order, independent of Parquet row order
    )
    trip_ids: list[str] = [str(value) for value in trip_sizes.get_column(GROUP_COLUMN).to_list()]
    row_counts: list[int] = [int(value) for value in trip_sizes.get_column("rows").to_list()]
    if len(trip_ids) < 3:
        msg = (
            f"a grouped split needs at least 3 distinct {GROUP_COLUMN} values, got {len(trip_ids)}"
        )
        raise ValidationError(msg, details={"trips": len(trip_ids)})

    rng = default_rng(seed)
    order = rng.permutation(len(trip_ids))
    total_rows = frame.height
    targets = [ratio * total_rows for ratio in ratios]
    assigned: list[float] = [0.0, 0.0, 0.0]
    buckets: tuple[list[str], list[str], list[str]] = ([], [], [])

    for index in order:
        trip_id = trip_ids[int(index)]
        rows = row_counts[int(index)]
        # Deficit = how far a fold still is from its target share. Largest deficit wins, so the
        # folds converge on the requested proportions however unevenly long the trips are.
        deficits = [targets[fold] - assigned[fold] for fold in range(3)]
        fold = max(range(3), key=lambda candidate: deficits[candidate])
        buckets[fold].append(trip_id)
        assigned[fold] += rows

    empty = [
        name for name, ids in zip(("train", "validation", "test"), buckets, strict=True) if not ids
    ]
    if empty:
        msg = f"grouped split left fold(s) {', '.join(empty)} empty; too few trips for {ratios!r}"
        raise ValidationError(msg, details={"empty_folds": empty, "trips": len(trip_ids)})

    def fold_frame(trip_subset: Sequence[str]) -> pl.DataFrame:
        return frame.filter(pl.col(GROUP_COLUMN).is_in(list(trip_subset)))

    origins = tuple(
        sorted(str(value) for value in frame.get_column("data_origin").unique().to_list())
    )
    split = DatasetSplit(
        train=fold_frame(buckets[0]),
        validation=fold_frame(buckets[1]),
        test=fold_frame(buckets[2]),
        seed=seed,
        ratios=ratios,
        cleaning=cleaning
        or CleaningReport(rows_in=frame.height, rows_out=frame.height, rejected={}),
        source_path=source_path or DEFAULT_TRAINING_PATH,
        data_origins=origins,
    )
    _LOGGER.info("dataset.split", **split.as_dict())
    return split


def load_dataset(
    path: Path | None = None,
    *,
    seed: int,
    ratios: tuple[float, float, float] = DEFAULT_SPLIT_RATIOS,
) -> DatasetSplit:
    """Load, clean and grouped-split the training set in one call."""
    parquet_path = path or DEFAULT_TRAINING_PATH
    frame = load_training_frame(parquet_path)
    cleaned, report = clean_training_frame(frame)
    if cleaned.height == 0:
        msg = f"every row of {parquet_path} was rejected by the cleaning rules"
        raise ValidationError(msg, details={"path": str(parquet_path), **report.as_dict()})
    return split_by_trip(
        cleaned,
        seed=seed,
        ratios=ratios,
        cleaning=report,
        source_path=parquet_path,
    )


# ======================================================================================
# Physics sweep — the documented stand-in for simulator output
# ======================================================================================

GENERATOR_VERSION: Final[str] = "physics_sweep/1"
"""Bumped whenever the sweep's distributions change, so two datasets are never confused."""

_WINDOW_SECONDS: Final[float] = 60.0
"""Aggregation window of one row, in simulated seconds — the shared contract's default."""

_SAMPLE_INTERVAL_S: Final[float] = 1.0
"""Resolution the micro-profile is integrated at. 1 Hz is the simulator's telemetry rate."""

_MAX_ACCELERATION_MS2: Final[float] = 2.6
"""Cap on the drive-cycle generator's acceleration; a loaded EV pulls ~2.5 m/s² in traffic."""

_MAX_DECELERATION_MS2: Final[float] = -3.4
"""Cap on braking in normal driving; emergency stops reach -8 and are not modelled."""

_ACCEL_EVENT_THRESHOLD_MS2: Final[float] = 0.5
"""|a| above which a 1 Hz sample counts as an acceleration event (see ``features``)."""

_SPEED_REVERSION_PER_S: Final[float] = 0.09
"""Mean-reversion rate of the speed process, in 1/s — a ~11 s correlation time."""

_LABEL_NOISE_FRACTION: Final[float] = 0.02
"""Relative standard deviation of the noise added to the energy label.

Real telemetry-derived consumption is not exact: pack energy is integrated from current and
voltage measurements, and the window boundaries quantise the distance. Two percent is a
deliberately modest figure whose only job is to keep an irreducible error floor in the problem
so that no model can reach R² = 1 and look like it discovered the generating function."""


@dataclass(frozen=True, slots=True)
class _RoadProfile:
    """Driving character of one road class in the sweep."""

    road_class: RoadClass
    speed_limit_kmh: float
    free_flow_kmh: float
    speed_sigma_ms: float
    """Diffusion of the speed process in m/s per second of elapsed time: how ragged the driving
    is at free flow.

    Calibrated so that the resulting mean |acceleration| matches what a 1 Hz telemetry trace
    shows in each regime — around 0.1 m/s² on a cruising Autobahn stretch, rising past 1.5 m/s²
    in stop-and-go city traffic once the severity multiplier of
    :data:`_SEVERITY_RAGGEDNESS` is applied. It is the single most consequential number in this
    generator: the cyclic accelerate-and-brake loss it produces is exactly the part of the
    label that the steady-state baseline cannot see, so setting it too high would manufacture
    the ML model's advantage instead of measuring it."""
    gradient_scale_percent: float
    segment_length_km: tuple[float, float]


_ROAD_PROFILES: Final[tuple[_RoadProfile, ...]] = (
    _RoadProfile(RoadClass.motorway, 130.0, 126.0, 0.11, 0.9, (6.0, 28.0)),
    _RoadProfile(RoadClass.trunk, 120.0, 108.0, 0.15, 1.1, (4.0, 16.0)),
    _RoadProfile(RoadClass.primary, 100.0, 88.0, 0.22, 1.5, (2.5, 11.0)),
    _RoadProfile(RoadClass.secondary, 100.0, 78.0, 0.26, 1.8, (2.0, 8.0)),
    _RoadProfile(RoadClass.tertiary, 80.0, 64.0, 0.32, 2.2, (1.2, 5.0)),
    _RoadProfile(RoadClass.residential, 50.0, 38.0, 0.55, 2.0, (0.4, 2.2)),
    _RoadProfile(RoadClass.service, 30.0, 22.0, 0.50, 1.6, (0.2, 1.0)),
)

_ROAD_PROFILE_BY_CLASS: Final[MappingProxyType[RoadClass, _RoadProfile]] = MappingProxyType(
    {profile.road_class: profile for profile in _ROAD_PROFILES}
)

_ARCHETYPES: Final[tuple[tuple[str, tuple[RoadClass, ...], tuple[float, ...]], ...]] = (
    (
        "autobahn_corridor",
        (RoadClass.motorway, RoadClass.trunk, RoadClass.primary),
        (0.74, 0.14, 0.12),
    ),
    (
        "regional",
        (RoadClass.primary, RoadClass.secondary, RoadClass.tertiary, RoadClass.motorway),
        (0.34, 0.30, 0.22, 0.14),
    ),
    (
        "urban",
        (RoadClass.residential, RoadClass.tertiary, RoadClass.secondary, RoadClass.service),
        (0.44, 0.26, 0.20, 0.10),
    ),
)
"""Trip archetypes and their road-class mixes: a Frankfurt-Stuttgart run, a Land corridor, and
city driving. Three shapes rather than one uniform sweep, because the feature interactions the
model has to learn (motorway speed with low variability versus city stop-and-go) only exist if
the dataset contains both regimes as coherent trips."""

_ARCHETYPE_WEIGHTS: Final[tuple[float, float, float]] = (0.42, 0.36, 0.22)

_SEVERITY_BY_CLASS: Final[MappingProxyType[RoadClass, tuple[float, float, float, float]]] = (
    MappingProxyType(
        {
            RoadClass.motorway: (0.62, 0.22, 0.11, 0.05),
            RoadClass.trunk: (0.66, 0.21, 0.09, 0.04),
            RoadClass.primary: (0.68, 0.20, 0.09, 0.03),
            RoadClass.secondary: (0.72, 0.18, 0.07, 0.03),
            RoadClass.tertiary: (0.76, 0.16, 0.06, 0.02),
            RoadClass.residential: (0.55, 0.25, 0.14, 0.06),
            RoadClass.service: (0.70, 0.20, 0.08, 0.02),
        }
    )
)
"""Probability of low/moderate/high/severe traffic per road class.

Roughly shaped after the Autobahn GmbH event feed: the motorway network carries most of the
reported disruptions, while residential streets are congested for a different reason (traffic
lights and parking manoeuvres) that shows up as stop-and-go rather than as a reported event."""

_SEVERITIES: Final[tuple[TrafficSeverity, ...]] = (
    TrafficSeverity.low,
    TrafficSeverity.moderate,
    TrafficSeverity.high,
    TrafficSeverity.severe,
)

_SEVERITY_RAGGEDNESS: Final[MappingProxyType[TrafficSeverity, float]] = MappingProxyType(
    {
        TrafficSeverity.low: 1.0,
        TrafficSeverity.moderate: 1.7,
        TrafficSeverity.high: 2.6,
        TrafficSeverity.severe: 3.6,
    }
)
"""Multiplier on the speed diffusion per severity: congestion is not just slower, it is
*jerkier*, and the jerkiness is what the steady-state baseline cannot see."""

_BASE_TIME: Final[datetime] = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
"""Fixed epoch of the synthetic timeline — no wall clock enters the data, so two runs with the
same seed produce byte-identical Parquet."""


def _sample_temperature_c(rng: Generator) -> float:
    """Ambient temperature in °C from a rough German annual distribution.

    Mean 10.5 °C with an 8.5 K spread and hard clipping at -12/+36 °C reproduces the range the
    DWD 10-minute feed actually delivers across a year, which is what the HVAC envelope and the
    cold-battery factor need to be exercised over. It is a sampling distribution for a sweep,
    not a climatology.
    """
    return float(np.clip(rng.normal(10.5, 8.5), -12.0, 36.0))


def _sample_wind_speed_ms(rng: Generator) -> float:
    """Mean wind speed in m/s — Weibull(k=2), the standard shape for surface wind."""
    return float(np.clip(rng.weibull(2.0) * 4.6, 0.0, 18.0))


def _drive_cycle(
    rng: Generator,
    *,
    target_speed_kmh: float,
    max_speed_kmh: float,
    sigma_ms: float,
    samples: int,
    entry_speed_ms: float,
) -> npt.NDArray[np.float64]:
    """Generate a 1 Hz speed trace, in m/s, around ``target_speed_kmh``.

    An Ornstein-Uhlenbeck process: the speed reverts to the target at
    :data:`_SPEED_REVERSION_PER_S` and is kicked by Gaussian noise of scale ``sigma_ms`` each
    second. That produces the right *statistics* — correlated speed, bounded excursions, more
    variance in congestion — without pretending to be a microscopic traffic model.

    The step is clipped to the acceleration envelope of a real EV before it is applied, so the
    trace can never contain a physically impossible jump, and it is clipped to ``[0, v_max]``,
    which is what creates genuine standstills in severe traffic: a low target with large noise
    spends real time against the zero bound.
    """
    target_ms = target_speed_kmh * MS_PER_KMH
    max_ms = max_speed_kmh * MS_PER_KMH
    trace = np.empty(samples + 1, dtype=np.float64)
    trace[0] = float(np.clip(entry_speed_ms, 0.0, max_ms))
    noise = rng.normal(0.0, sigma_ms, size=samples)
    for index in range(samples):
        current = trace[index]
        drift = _SPEED_REVERSION_PER_S * (target_ms - current) * _SAMPLE_INTERVAL_S
        step = float(
            np.clip(
                drift + noise[index],
                _MAX_DECELERATION_MS2 * _SAMPLE_INTERVAL_S,
                _MAX_ACCELERATION_MS2 * _SAMPLE_INTERVAL_S,
            )
        )
        trace[index + 1] = float(np.clip(current + step, 0.0, max_ms))
    return trace


def _integrate_window(
    model: PhysicalEnergyModel,
    profile: VehicleProfile,
    trace: npt.NDArray[np.float64],
    *,
    gradient_percent: float,
    outside_temperature_c: float,
    battery_temperature_c: float,
    precipitation_mm: float,
    wind_speed_ms: float,
    road_class: RoadClass,
    speed_limit_kmh: float,
    traffic_severity: TrafficSeverity,
) -> tuple[float, float, float, float, int]:
    """Integrate the physical model along the 1 Hz trace.

    Returns ``(energy_kwh, distance_km, mean_abs_acceleration, speed_std_kmh, accel_events)``.

    This is the step that makes the dataset non-trivial for the ML model. The energy is
    integrated over every second *including the accelerations*, using the signed instantaneous
    battery power (so recuperation is credited at the modelled 65 % and floored at -50 kW),
    whereas the baseline the model is later compared against evaluates the same window once, at
    its mean speed, with zero net acceleration. The difference between those two numbers is a
    real physical effect — the cyclic accelerate-and-brake loss of stop-and-go driving — and it
    is precisely what ``acceleration_abs_mean_ms2`` and ``accel_events_per_km`` let a learned
    model recover.

    The window energy is floored at zero for the same reason a single segment is
    (:meth:`~autotwin_ml.baseline.PhysicalEnergyModel.segment_energy`): without a
    state-of-charge-dependent charge-acceptance limit, an unbroken descent would otherwise
    report the window as a net energy source.
    """
    speeds_ms = trace[:-1]
    accelerations = np.diff(trace) / _SAMPLE_INTERVAL_S
    distance_m = float(np.sum(speeds_ms) * _SAMPLE_INTERVAL_S)
    energy_kwh = 0.0
    for index in range(speeds_ms.size):
        speed_ms = float(speeds_ms[index])
        conditions = SegmentConditions(
            speed_kmh=speed_ms * KMH_PER_MS,
            distance_km=speed_ms * _SAMPLE_INTERVAL_S / 1000.0,
            gradient_percent=gradient_percent,
            outside_temperature_c=outside_temperature_c,
            battery_temperature_c=battery_temperature_c,
            acceleration_ms2=float(accelerations[index]),
            traffic_severity=traffic_severity,
            precipitation_mm=precipitation_mm,
            wind_speed_ms=wind_speed_ms,
            road_class=road_class,
            speed_limit_kmh=speed_limit_kmh,
            duration_s=_SAMPLE_INTERVAL_S,
        )
        power_kw = model.instantaneous_power_kw(profile, conditions)
        energy_kwh += power_kw * _SAMPLE_INTERVAL_S / SECONDS_PER_HOUR

    speeds_kmh = speeds_ms * KMH_PER_MS
    accel_events = int(np.count_nonzero(np.abs(accelerations) > _ACCEL_EVENT_THRESHOLD_MS2))
    return (
        max(0.0, energy_kwh),
        distance_m / 1000.0,
        float(np.mean(np.abs(accelerations))),
        float(np.std(speeds_kmh)),
        accel_events,
    )


def generate_physics_sweep(
    *,
    trips: int = 400,
    seed: int = 20_260_214,
    out_path: Path | None = None,
    window_seconds: float = _WINDOW_SECONDS,
    model: PhysicalEnergyModel | None = None,
) -> Path:
    """Write a training Parquet in the shared schema from a seeded physical sweep.

    **This is not simulator output and never claims to be.** It exists so the ML half of the
    project can be trained, evaluated and reviewed while the simulator is still being written,
    and so the whole pipeline has a reproducible dataset that does not depend on a 40-vehicle
    run finishing first. A sidecar records what produced the file; the trainer reads it and
    carries the provenance into the metrics JSON and the ``ml_models`` row.

    **What it does contain.** ``trips`` synthetic journeys, each with one of the five generic
    vehicle profiles, one of three route archetypes (Autobahn corridor, regional, urban), one
    day's weather drawn from a German annual distribution, a warming battery pack, a draining
    battery, and a sequence of road segments with sampled gradients and traffic states. Every
    60 s window is integrated at 1 Hz over a generated drive cycle, so the label carries the
    transient losses that the steady-state baseline cannot express.

    Args:
        trips: Number of synthetic journeys. 400 yields roughly 22 000 windows.
        seed: Master seed. The same seed produces byte-identical output.
        out_path: Destination; defaults to :data:`DEFAULT_TRAINING_PATH`.
        window_seconds: Aggregation window. The shared contract's default is 60 s.
        model: Physical model to integrate with; a default instance is used when omitted.

    Returns:
        The path the Parquet was written to.
    """
    if trips < 3:
        msg = f"a grouped split needs at least 3 trips, got {trips}"
        raise ValidationError(msg, details={"trips": trips})
    samples_per_window = round(window_seconds / _SAMPLE_INTERVAL_S)
    if samples_per_window < 2:
        msg = f"window_seconds must cover at least two 1 Hz samples, got {window_seconds}"
        raise ValidationError(msg, details={"window_seconds": window_seconds})

    physics = model or PhysicalEnergyModel()
    rng = default_rng(seed)
    rows: list[dict[str, Any]] = []

    for trip_index in range(trips):
        rows.extend(
            _generate_trip(
                rng,
                physics=physics,
                trip_index=trip_index,
                samples_per_window=samples_per_window,
                window_seconds=window_seconds,
            )
        )

    if not rows:
        msg = "the sweep produced no windows; check the trip parameters"
        raise ValidationError(msg, details={"trips": trips})

    frame = pl.DataFrame(rows, schema=dict(TRAINING_SCHEMA))
    destination = out_path or DEFAULT_TRAINING_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(destination)

    sidecar = {
        "kind": "physics_sweep",
        "generator": GENERATOR_VERSION,
        "generated_at": utc_now().isoformat(),
        "seed": seed,
        "trips": trips,
        "rows": frame.height,
        "window_seconds": window_seconds,
        "sample_interval_s": _SAMPLE_INTERVAL_S,
        "label_noise_fraction": _LABEL_NOISE_FRACTION,
        "physics": {
            "drivetrain_efficiency": physics.constants.drivetrain_efficiency,
            "regen_efficiency": physics.constants.regen_efficiency,
            "auxiliary_base_kw": physics.constants.auxiliary_base_kw,
        },
        "note": (
            "Synthetic stand-in for `python -m autotwin_simulator.cli generate-training-data`. "
            "Labels are the 1 Hz integral of the physical road-load model over a generated "
            "drive cycle plus 2 % noise — not measurements of any real vehicle."
        ),
        "parquet_sha256": _sha256(destination),
    }
    _sidecar_path(destination).write_text(
        json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _LOGGER.info(
        "dataset.sweep_written",
        path=str(destination),
        rows=frame.height,
        trips=trips,
        seed=seed,
    )
    return destination


def _generate_trip(
    rng: Generator,
    *,
    physics: PhysicalEnergyModel,
    trip_index: int,
    samples_per_window: int,
    window_seconds: float,
) -> list[dict[str, Any]]:
    """Generate every window of one synthetic trip.

    A trip is the unit the split groups on, so everything that is constant for a journey —
    vehicle, weather, archetype — is drawn once here. That is what makes the grouped split
    meaningful: a leaked window would hand the test fold the trip's weather and vehicle for
    free.
    """
    profile = GENERIC_VEHICLE_PROFILES[trip_index % len(GENERIC_VEHICLE_PROFILES)]
    archetype_index = int(rng.choice(len(_ARCHETYPES), p=_ARCHETYPE_WEIGHTS))
    archetype_name, classes, class_weights = _ARCHETYPES[archetype_index]

    outside_temperature_c = _sample_temperature_c(rng)
    wind_speed_ms = _sample_wind_speed_ms(rng)
    precipitation_mm = 0.0 if rng.random() < 0.72 else float(rng.exponential(0.55))
    # A parked car has cold-soaked to ambient; the pack then warms under load towards a
    # steady state a dozen kelvin above ambient, which is what the cold-battery factor decays
    # along over the first half hour of driving.
    battery_temperature_c = outside_temperature_c + float(rng.normal(0.0, 1.5))
    battery_steady_c = outside_temperature_c + float(rng.uniform(8.0, 16.0))

    soc_percent = float(rng.uniform(28.0, 96.0))
    windows_planned = int(rng.integers(18, 92))
    trip_id = f"sweep-{trip_index:05d}"
    vehicle_id = f"ATW-S{trip_index % 250:04d}"
    window_start = _BASE_TIME + timedelta(days=trip_index % 210, minutes=7 * (trip_index % 60))

    rows: list[dict[str, Any]] = []
    entry_speed_ms = 0.0
    segment_remaining_km = 0.0
    road_profile = _ROAD_PROFILE_BY_CLASS[classes[0]]
    segment_length_km = 0.0
    gradient_percent = 0.0
    severity = TrafficSeverity.low

    for _ in range(windows_planned):
        if segment_remaining_km <= 0.0:
            class_index = int(rng.choice(len(classes), p=class_weights))
            road_profile = _ROAD_PROFILE_BY_CLASS[classes[class_index]]
            low, high = road_profile.segment_length_km
            segment_length_km = float(rng.uniform(low, high))
            segment_remaining_km = segment_length_km
            gradient_percent = float(
                np.clip(rng.normal(0.0, road_profile.gradient_scale_percent), -5.0, 5.0)
            )
            severity = _SEVERITIES[
                int(rng.choice(4, p=_SEVERITY_BY_CLASS[road_profile.road_class]))
            ]

        raggedness = _SEVERITY_RAGGEDNESS[severity]
        free_flow_kmh = road_profile.free_flow_kmh * float(rng.uniform(0.88, 1.08))
        target_speed_kmh = free_flow_kmh / severity.delay_factor
        trace = _drive_cycle(
            rng,
            target_speed_kmh=target_speed_kmh,
            max_speed_kmh=road_profile.free_flow_kmh * 1.25,
            sigma_ms=road_profile.speed_sigma_ms * raggedness,
            samples=samples_per_window,
            entry_speed_ms=entry_speed_ms,
        )
        entry_speed_ms = float(trace[-1])

        (
            energy_kwh,
            distance_km,
            accel_abs_mean,
            speed_std_kmh,
            accel_events,
        ) = _integrate_window(
            physics,
            profile,
            trace,
            gradient_percent=gradient_percent,
            outside_temperature_c=outside_temperature_c,
            battery_temperature_c=battery_temperature_c,
            precipitation_mm=precipitation_mm,
            wind_speed_ms=wind_speed_ms,
            road_class=road_profile.road_class,
            speed_limit_kmh=road_profile.speed_limit_kmh,
            traffic_severity=severity,
        )
        if distance_km <= 1e-4:
            # A window spent entirely at a standstill has no consumption per 100 km to learn
            # from. The simulator emits such windows; the training set drops them, exactly as
            # clean_training_frame would.
            window_start += timedelta(seconds=window_seconds)
            continue

        energy_kwh *= 1.0 + float(rng.normal(0.0, _LABEL_NOISE_FRACTION))
        energy_kwh = max(energy_kwh, 1e-6)
        speeds_kmh = trace[:-1] * KMH_PER_MS
        moving = speeds_kmh[speeds_kmh > 1.0]
        mean_speed_kmh = distance_km / (window_seconds / SECONDS_PER_HOUR)

        rows.append(
            {
                "trip_id": trip_id,
                "vehicle_id": vehicle_id,
                "vehicle_code": profile.code,
                "window_start": window_start,
                "window_end": window_start + timedelta(seconds=window_seconds),
                "distance_km": distance_km,
                "duration_s": window_seconds,
                "speed_kmh": mean_speed_kmh,
                "avg_speed_kmh": float(np.mean(moving)) if moving.size else 0.0,
                "speed_std_kmh": speed_std_kmh,
                "acceleration_abs_mean_ms2": accel_abs_mean,
                "accel_events_per_km": accel_events / distance_km,
                "outside_temperature_c": outside_temperature_c,
                "battery_temperature_c": battery_temperature_c,
                "soc_percent": soc_percent,
                "road_class": road_profile.road_class.value,
                "road_class_ordinal": road_profile.road_class.ordinal,
                "speed_limit_kmh": road_profile.speed_limit_kmh,
                "traffic_severity": severity.value,
                "traffic_severity_ordinal": severity.ordinal,
                "precipitation_mm": precipitation_mm,
                "wind_speed_ms": wind_speed_ms,
                "gradient_percent": gradient_percent,
                "segment_distance_km": segment_length_km,
                "mass_kg": profile.mass_kg,
                "drag_area": profile.drag_area,
                "nominal_consumption_kwh_100km": profile.nominal_consumption_kwh_100km,
                "hvac_load_kw": physics.constants.hvac_power_kw(outside_temperature_c),
                "is_motorway": 1 if road_profile.road_class is RoadClass.motorway else 0,
                "energy_kwh": energy_kwh,
                "energy_consumption_kwh_100km": energy_kwh / distance_km * 100.0,
                "data_origin": DataOrigin.simulated.value,
            }
        )

        segment_remaining_km -= distance_km
        soc_percent -= energy_kwh / profile.usable_capacity_kwh * 100.0
        battery_temperature_c += (battery_steady_c - battery_temperature_c) * 0.035
        window_start += timedelta(seconds=window_seconds)
        if soc_percent <= 8.0:
            # Out of usable charge: the trip ends here rather than continuing on an impossible
            # state of charge. Trips therefore vary in length for a physical reason.
            break

    _LOGGER.debug(
        "dataset.trip_generated",
        trip_id=trip_id,
        archetype=archetype_name,
        vehicle_code=profile.code,
        windows=len(rows),
    )
    return rows
