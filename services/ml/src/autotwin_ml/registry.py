"""Model registry: which versions exist, which one is active, and how to load it.

Two stores describe the same thing and neither is redundant.

**The models directory** is the serving truth. ``models/energy_consumption_v3.joblib`` plus its
``.metrics.json`` sidecar and a one-line ``energy_consumption.active`` pointer are everything
the API needs to answer a prediction. A container that mounts only ``models/`` serves correctly
with no database round trip on the request path, and a model that exists on disk is a model
that can actually be loaded — which is the property a registry row cannot guarantee.

**The ``ml_models`` table** is the queryable truth. ``GET /api/v1/ml/models`` needs metrics,
training row counts and provenance for every version ever trained, joined against the
predictions that used them, and that is a database's job rather than a directory listing's.

:func:`register_model` writes the row from the artefact, so the two never drift by accident,
and :func:`sync_registry` re-derives the table from the directory when they have drifted anyway
— after a clone that ships the artefacts but starts with an empty database, which is the normal
first-run state of this project.

Degrading with nothing trained
------------------------------

Every resolution path raises :class:`ModelNotAvailable` — an ``AutoTwinError`` carrying HTTP
503 and the stable code ``model_not_available`` — rather than ``FileNotFoundError`` or, worse,
an ``ImportError`` at module import. The API can therefore mount its ML routes unconditionally
and answer *"no model has been trained yet, run `python -m autotwin_ml.cli train`"* with a
correct status code, which is what BUILD_SPEC §0.3 means by degrading instead of crashing.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final
from uuid import UUID

import joblib
import numpy as np
import sqlalchemy as sa
from numpy import typing as npt
from sqlalchemy.ext.asyncio import AsyncSession

from autotwin_contracts.enums import DataOrigin
from autotwin_contracts.temporal import ensure_utc
from autotwin_core.config import Settings, get_settings
from autotwin_core.db.models import MLModel
from autotwin_core.errors import AutoTwinError, ValidationError
from autotwin_core.logging import get_logger
from autotwin_ml.features import FEATURE_NAMES

__all__ = [
    "ARTIFACT_FORMAT_VERSION",
    "LoadedModel",
    "ModelNotAvailable",
    "ModelRecord",
    "activate_model",
    "artifact_paths",
    "clear_model_cache",
    "discover_models",
    "get_active_model",
    "list_registered_models",
    "load_model",
    "next_version",
    "register_model",
    "resolve_model",
    "save_artifact",
    "set_active_version",
    "sync_registry",
]

_LOGGER = get_logger(__name__)

ARTIFACT_FORMAT_VERSION: Final[int] = 1
"""Layout version of the joblib payload. Bumped if the dict keys ever change shape."""

_ARTIFACT_TEMPLATE: Final[str] = "{name}_v{version}.joblib"
_METRICS_TEMPLATE: Final[str] = "{name}_v{version}.metrics.json"
_ACTIVE_POINTER_TEMPLATE: Final[str] = "{name}.active"

_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^(?P<name>.+)_v(?P<version>\d+)$")
"""``energy_consumption_v12`` → name ``energy_consumption``, version ``12``.

Versions are integers rendered without padding, so they sort numerically rather than
lexicographically once parsed — ``v10`` must come after ``v9``, and a zero-padded scheme would
have to guess a width up front."""


class ModelNotAvailable(AutoTwinError):
    """No usable model artefact could be resolved.

    HTTP 503 rather than 404: the resource is not missing, the *service* is not ready to answer
    yet. A client that retries after training has been run will succeed, which is exactly the
    semantics 503 carries and 404 does not.
    """

    code: ClassVar[str] = "model_not_available"
    http_status: ClassVar[int] = 503


# ======================================================================================
# Paths
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    """The two files one model version consists of, plus its active-pointer file."""

    artifact: Path
    """``<model_dir>/<name>_v<version>.joblib`` — the estimator."""

    metrics: Path
    """``<model_dir>/<name>_v<version>.metrics.json`` — everything the /ml page renders."""

    active_pointer: Path
    """``<model_dir>/<name>.active`` — one line naming the version the API should serve."""


def _resolve_model_dir(model_dir: Path | None, settings: Settings | None) -> Path:
    """Directory holding the artefacts: explicit argument, else ``AUTOTWIN_ML_MODEL_DIR``."""
    if model_dir is not None:
        return model_dir
    return (settings or get_settings()).model_dir_path


def artifact_paths(
    name: str,
    version: str,
    *,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> ArtifactPaths:
    """Locate the files of one model version without touching the filesystem."""
    directory = _resolve_model_dir(model_dir, settings)
    return ArtifactPaths(
        artifact=directory / _ARTIFACT_TEMPLATE.format(name=name, version=version),
        metrics=directory / _METRICS_TEMPLATE.format(name=name, version=version),
        active_pointer=directory / _ACTIVE_POINTER_TEMPLATE.format(name=name),
    )


# ======================================================================================
# Records
# ======================================================================================


@dataclass(frozen=True, slots=True)
class ModelRecord:
    """One trained version, as described by its metrics sidecar or its ``ml_models`` row.

    The same dataclass serves both stores so that a caller reading the registry does not have
    to care which one answered — and so that :func:`sync_registry` can compare them field by
    field rather than by shape.
    """

    name: str
    version: str
    algorithm: str
    trained_at: datetime
    training_rows: int
    feature_names: tuple[str, ...]
    metrics: dict[str, Any]
    artifact_path: Path
    metrics_path: Path
    training_data_origin: DataOrigin
    is_active: bool = False
    notes: str | None = None

    @property
    def version_number(self) -> int:
        """Numeric version, for ordering. ``v10`` sorts after ``v9``."""
        return int(self.version)

    @property
    def label(self) -> str:
        """``energy_consumption v3`` — how the version is named in logs and on the /ml page."""
        return f"{self.name} v{self.version}"

    @property
    def headline_metrics(self) -> dict[str, float]:
        """The six numbers of BUILD_SPEC §3.2's ``ml_models.metrics``, defaulting to NaN.

        NaN rather than 0.0 for an absent metric: a missing MAE must not render as a perfect
        one.
        """
        keys = ("mae", "rmse", "r2", "baseline_mae", "baseline_rmse", "baseline_r2")
        return {key: float(self.metrics.get(key, float("nan"))) for key in keys}

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready summary — the shape ``GET /api/v1/ml/models`` serves."""
        return {
            "name": self.name,
            "version": self.version,
            "algorithm": self.algorithm,
            "trained_at": self.trained_at.isoformat(),
            "training_rows": self.training_rows,
            "feature_names": list(self.feature_names),
            "metrics": self.headline_metrics,
            "artifact_path": str(self.artifact_path),
            "is_active": self.is_active,
            "training_data_origin": self.training_data_origin.value,
            "notes": self.notes,
        }

    @classmethod
    def from_metrics_file(cls, path: Path, *, is_active: bool = False) -> ModelRecord:
        """Build a record from a ``*.metrics.json`` sidecar.

        Raises:
            ValidationError: If the file is unreadable or does not name a version — a metrics
                file that cannot be parsed is a corrupt artefact, and silently skipping it
                would hide a half-finished training run.
        """
        try:
            payload: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"metrics file {path} is unreadable: {exc}"
            raise ValidationError(msg, details={"path": str(path)}) from exc
        if not isinstance(payload, dict):
            msg = f"metrics file {path} does not contain a JSON object"
            raise ValidationError(msg, details={"path": str(path)})

        stem_match = _VERSION_PATTERN.match(path.name.removesuffix(".metrics.json"))
        if stem_match is None:
            msg = f"metrics file {path} does not follow the <name>_v<version>.metrics.json scheme"
            raise ValidationError(msg, details={"path": str(path)})
        name = str(payload.get("name") or stem_match.group("name"))
        version = str(payload.get("version") or stem_match.group("version"))
        origin_raw = str(payload.get("training_data_origin", DataOrigin.simulated.value))
        try:
            origin = DataOrigin(origin_raw)
        except ValueError:
            origin = DataOrigin.simulated

        trained_at_raw = payload.get("trained_at")
        trained_at = (
            ensure_utc(datetime.fromisoformat(str(trained_at_raw)))
            if trained_at_raw
            else datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        )
        feature_names = payload.get("feature_names") or list(FEATURE_NAMES)
        # ``metrics`` inside the file is exactly what ``ml_models.metrics`` stores: the six
        # headline numbers of BUILD_SPEC §3.2 at the top level, with the richer training report
        # (feature importance, SHAP, scatter points) nested beside them.
        nested = payload.get("metrics")
        metrics = nested if isinstance(nested, dict) else payload
        return cls(
            name=name,
            version=version,
            algorithm=str(payload.get("algorithm", "unknown")),
            trained_at=trained_at,
            training_rows=int(payload.get("training_rows", 0)),
            feature_names=tuple(str(value) for value in feature_names),
            metrics=metrics,
            artifact_path=path.with_name(path.name.replace(".metrics.json", ".joblib")),
            metrics_path=path,
            training_data_origin=origin,
            is_active=is_active,
            notes=payload.get("notes"),
        )


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """An estimator in memory together with the record that describes it.

    The feature names travel *inside* the artefact and are checked against
    :data:`~autotwin_ml.features.FEATURE_NAMES` at load time. That check is the reason this
    wrapper exists: a booster is a column-position machine, and an artefact trained before a
    feature was inserted would otherwise keep predicting confidently on shifted columns.
    """

    record: ModelRecord
    estimator: Any
    feature_names: tuple[str, ...] = field(default=FEATURE_NAMES)

    @property
    def name(self) -> str:
        """Model name, e.g. ``energy_consumption``."""
        return self.record.name

    @property
    def version(self) -> str:
        """Model version, e.g. ``3``."""
        return self.record.version

    @property
    def algorithm(self) -> str:
        """``lightgbm`` or ``sklearn_hist_gradient_boosting``."""
        return self.record.algorithm

    def predict(self, matrix: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Predict kWh/100 km for an ``(n, 20)`` design matrix.

        Raises:
            ValidationError: If the matrix does not have exactly the expected column count,
                which is the one shape error that would otherwise produce numbers rather than
                an exception.
        """
        if matrix.ndim != 2 or matrix.shape[1] != len(self.feature_names):
            msg = (
                f"expected a (n, {len(self.feature_names)}) feature matrix, "
                f"got shape {matrix.shape}"
            )
            raise ValidationError(msg, details={"shape": list(matrix.shape)})
        raw = self.estimator.predict(matrix)
        return np.asarray(raw, dtype=np.float64).reshape(-1)


# ======================================================================================
# Artefact I/O
# ======================================================================================


def save_artifact(
    estimator: Any,
    *,
    name: str,
    version: str,
    algorithm: str,
    trained_at: datetime,
    path: Path,
    feature_names: tuple[str, ...] = FEATURE_NAMES,
) -> Path:
    """Write the joblib payload. The format lives here so loading and saving cannot disagree."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": ARTIFACT_FORMAT_VERSION,
        "name": name,
        "version": version,
        "algorithm": algorithm,
        "trained_at": ensure_utc(trained_at).isoformat(),
        "feature_names": list(feature_names),
        "estimator": estimator,
    }
    # compress=3: ~4x smaller than raw pickle at negligible load cost, which is what keeps the
    # artefact inside the "small enough to commit" rule of BUILD_SPEC §10.2.
    joblib.dump(payload, path, compress=3)
    return path


def _load_artifact(record: ModelRecord) -> LoadedModel:
    """Read a joblib artefact and check its feature contract against the current one."""
    if not record.artifact_path.is_file():
        msg = (
            f"model artefact {record.artifact_path} is missing although its metrics file "
            f"exists; retrain with `python -m autotwin_ml.cli train --version {record.version}`"
        )
        raise ModelNotAvailable(msg, details={"artifact_path": str(record.artifact_path)})
    try:
        payload: Any = joblib.load(record.artifact_path)
    except Exception as exc:  # any unpickling failure means "not servable"
        msg = f"model artefact {record.artifact_path} could not be loaded: {exc}"
        raise ModelNotAvailable(msg, details={"artifact_path": str(record.artifact_path)}) from exc
    if not isinstance(payload, dict) or "estimator" not in payload:
        msg = f"model artefact {record.artifact_path} does not contain an estimator"
        raise ModelNotAvailable(msg, details={"artifact_path": str(record.artifact_path)})

    stored = tuple(str(value) for value in payload.get("feature_names", ()))
    if stored != FEATURE_NAMES:
        msg = (
            f"model artefact {record.artifact_path} was trained on a different feature "
            f"contract ({len(stored)} column(s)); retrain it against the current "
            f"FEATURE_NAMES ({len(FEATURE_NAMES)} column(s))"
        )
        raise ModelNotAvailable(
            msg,
            details={
                "artifact_path": str(record.artifact_path),
                "artifact_features": list(stored),
                "expected_features": list(FEATURE_NAMES),
            },
        )
    return LoadedModel(record=record, estimator=payload["estimator"], feature_names=stored)


_CACHE_LOCK: Final[threading.Lock] = threading.Lock()
_MODEL_CACHE: dict[Path, tuple[int, LoadedModel]] = {}
"""Path → (artefact mtime in ns, loaded model).

Keyed on the modification time rather than only the path so that retraining a version in place
— which ``--version`` deliberately allows during development — is picked up without restarting
the API. A plain ``lru_cache`` would serve the superseded booster until the process died.

A :class:`threading.Lock` rather than an ``asyncio`` one because FastAPI runs synchronous
dependencies in a worker thread pool, so the cache is genuinely reachable from several threads.
"""


def clear_model_cache() -> None:
    """Forget every loaded estimator — for tests and for an explicit reload."""
    with _CACHE_LOCK:
        _MODEL_CACHE.clear()


def load_model(record: ModelRecord) -> LoadedModel:
    """Load (or return the cached) estimator for ``record``."""
    path = record.artifact_path
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError as exc:
        msg = f"model artefact {path} is not readable: {exc}"
        raise ModelNotAvailable(msg, details={"artifact_path": str(path)}) from exc

    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(path)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

    loaded = _load_artifact(record)
    with _CACHE_LOCK:
        _MODEL_CACHE[path] = (mtime_ns, loaded)
    _LOGGER.info("ml.model_loaded", model=loaded.record.label, algorithm=loaded.algorithm)
    return loaded


# ======================================================================================
# Filesystem registry
# ======================================================================================


def active_version(
    *,
    name: str | None = None,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Version named by the ``<name>.active`` pointer, or ``None`` when there is no pointer."""
    resolved = settings or get_settings()
    model_name = name or resolved.ml_active_model
    pointer = artifact_paths(model_name, "0", model_dir=model_dir, settings=resolved).active_pointer
    if not pointer.is_file():
        return None
    value = pointer.read_text(encoding="utf-8").strip()
    return value or None


def set_active_version(
    version: str,
    *,
    name: str | None = None,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> Path:
    """Point ``<name>.active`` at ``version``, refusing a version that has no artefact.

    Refusing is the point: an active pointer to a missing file turns every prediction request
    into a 503 that looks like "nothing is trained" when in fact something is.
    """
    resolved = settings or get_settings()
    model_name = name or resolved.ml_active_model
    paths = artifact_paths(model_name, version, model_dir=model_dir, settings=resolved)
    if not paths.artifact.is_file():
        msg = f"cannot activate {model_name} v{version}: {paths.artifact} does not exist"
        raise ModelNotAvailable(msg, details={"artifact_path": str(paths.artifact)})
    paths.active_pointer.parent.mkdir(parents=True, exist_ok=True)
    paths.active_pointer.write_text(f"{version}\n", encoding="utf-8")
    _LOGGER.info("ml.model_activated", model=f"{model_name} v{version}")
    return paths.active_pointer


def discover_models(
    *,
    name: str | None = None,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> tuple[ModelRecord, ...]:
    """Every version found in the models directory, newest version first.

    Driven by the ``*.metrics.json`` sidecars rather than by the ``.joblib`` files: a metrics
    file without an artefact is a broken training run worth reporting, while an artefact
    without metrics cannot be described at all and is skipped with a warning.
    """
    resolved = settings or get_settings()
    model_name = name or resolved.ml_active_model
    directory = _resolve_model_dir(model_dir, resolved)
    if not directory.is_dir():
        return ()
    pointer = active_version(name=model_name, model_dir=directory, settings=resolved)

    records: list[ModelRecord] = []
    for metrics_path in sorted(directory.glob(f"{model_name}_v*.metrics.json")):
        try:
            record = ModelRecord.from_metrics_file(metrics_path)
        except ValidationError as exc:
            _LOGGER.warning("ml.metrics_file_invalid", path=str(metrics_path), error=exc.message)
            continue
        records.append(record)

    if not records:
        return ()
    newest = max(records, key=lambda item: item.version_number).version
    active = pointer if pointer is not None else newest
    return tuple(
        sorted(
            (
                ModelRecord(
                    name=record.name,
                    version=record.version,
                    algorithm=record.algorithm,
                    trained_at=record.trained_at,
                    training_rows=record.training_rows,
                    feature_names=record.feature_names,
                    metrics=record.metrics,
                    artifact_path=record.artifact_path,
                    metrics_path=record.metrics_path,
                    training_data_origin=record.training_data_origin,
                    is_active=record.version == active,
                    notes=record.notes,
                )
                for record in records
            ),
            key=lambda item: item.version_number,
            reverse=True,
        )
    )


def next_version(
    *,
    name: str | None = None,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> str:
    """The next unused integer version for ``name`` — ``"1"`` when nothing is trained yet."""
    existing = discover_models(name=name, model_dir=model_dir, settings=settings)
    if not existing:
        return "1"
    return str(max(record.version_number for record in existing) + 1)


def resolve_model(
    version: str | None = None,
    *,
    name: str | None = None,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> ModelRecord:
    """Find the requested version, or the active one, or fail with a 503-shaped error.

    Resolution order without an explicit ``version``: the ``<name>.active`` pointer if it names
    an existing version, else the highest version present. Falling back to the highest version
    means a freshly cloned repository serves its committed artefact without anyone having to
    run an activation step.
    """
    resolved = settings or get_settings()
    model_name = name or resolved.ml_active_model
    records = discover_models(name=model_name, model_dir=model_dir, settings=resolved)
    if not records:
        directory = _resolve_model_dir(model_dir, resolved)
        msg = (
            f"no trained model named {model_name!r} in {directory}. "
            "Train one with `python -m autotwin_ml.cli train`."
        )
        raise ModelNotAvailable(msg, details={"model": model_name, "model_dir": str(directory)})

    if version is None:
        for record in records:
            if record.is_active:
                return record
        return records[0]

    for record in records:
        if record.version == version:
            return record
    known = ", ".join(record.version for record in records)
    msg = f"model {model_name!r} has no version {version!r}; available versions: {known}"
    raise ModelNotAvailable(msg, details={"model": model_name, "requested_version": version})


def get_active_model(
    version: str | None = None,
    *,
    name: str | None = None,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> LoadedModel:
    """Resolve and load the served model — the one call the API's prediction path makes.

    Raises:
        ModelNotAvailable: When nothing is trained, the requested version is unknown, or the
            artefact is unreadable or was trained on a different feature contract. Every one of
            those is a 503 with a message that names the next action.
    """
    return load_model(resolve_model(version, name=name, model_dir=model_dir, settings=settings))


# ======================================================================================
# Database registry
# ======================================================================================


def _record_from_row(row: MLModel) -> ModelRecord:
    """Adapt an ``ml_models`` row onto the shared record type."""
    artifact_path = Path(row.artifact_path)
    return ModelRecord(
        name=row.name,
        version=row.version,
        algorithm=row.algorithm,
        trained_at=ensure_utc(row.trained_at),
        training_rows=row.training_rows,
        feature_names=tuple(str(value) for value in row.feature_names),
        metrics=dict(row.metrics),
        artifact_path=artifact_path,
        metrics_path=artifact_path.with_suffix(".metrics.json"),
        training_data_origin=row.training_data_origin,
        is_active=row.is_active,
        notes=row.notes,
    )


async def list_registered_models(
    session: AsyncSession,
    *,
    name: str | None = None,
) -> list[ModelRecord]:
    """Every row of ``ml_models``, newest training run first — ``GET /api/v1/ml/models``."""
    statement = sa.select(MLModel).order_by(MLModel.trained_at.desc())
    if name is not None:
        statement = statement.where(MLModel.name == name)
    rows = (await session.execute(statement)).scalars().all()
    return [_record_from_row(row) for row in rows]


async def register_model(
    session: AsyncSession,
    record: ModelRecord,
    *,
    activate: bool = True,
) -> UUID:
    """Insert or update the ``ml_models`` row for ``record`` and return its id.

    Upsert on ``(name, version)`` rather than insert-only because retraining a version in place
    is a normal development action and a ``UniqueViolation`` on the second run would be a
    pointlessly hostile failure. When ``activate`` is set, every other version of the same model
    is deactivated in the same transaction, so ``is_active`` can never be true twice.
    """
    existing = (
        await session.execute(
            sa.select(MLModel).where(
                MLModel.name == record.name,
                MLModel.version == record.version,
            )
        )
    ).scalar_one_or_none()

    if activate:
        await session.execute(
            sa.update(MLModel).where(MLModel.name == record.name).values(is_active=False)
        )

    values = {
        "algorithm": record.algorithm,
        "trained_at": record.trained_at,
        "training_rows": record.training_rows,
        "feature_names": list(record.feature_names),
        "metrics": record.metrics,
        "artifact_path": str(record.artifact_path),
        "is_active": activate,
        "training_data_origin": record.training_data_origin,
        "notes": record.notes,
    }
    if existing is None:
        row = MLModel(name=record.name, version=record.version, **values)
        session.add(row)
        await session.flush()
    else:
        for key, value in values.items():
            setattr(existing, key, value)
        await session.flush()
        row = existing

    _LOGGER.info("ml.model_registered", model=record.label, active=activate, id=str(row.id))
    return row.id


async def activate_model(
    session: AsyncSession,
    version: str,
    *,
    name: str | None = None,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> ModelRecord:
    """Make ``version`` the served model in **both** stores.

    The filesystem pointer is written first: if the database write then fails, serving still
    follows the operator's intent and the registry row can be repaired with
    :func:`sync_registry`. The reverse order would leave a database claiming a version the API
    does not actually serve.
    """
    resolved = settings or get_settings()
    model_name = name or resolved.ml_active_model
    record = resolve_model(version, name=model_name, model_dir=model_dir, settings=resolved)
    set_active_version(version, name=model_name, model_dir=model_dir, settings=resolved)

    known = (
        await session.execute(
            sa.select(MLModel.id).where(
                MLModel.name == model_name,
                MLModel.version == version,
            )
        )
    ).scalar_one_or_none()
    if known is None:
        # The artefact exists on disk but was never registered — a cloned repository before
        # anyone ran sync_registry. Insert it rather than activating a row that is not there.
        await register_model(session, record, activate=True)
        return record

    await session.execute(
        sa.update(MLModel).where(MLModel.name == model_name).values(is_active=False)
    )
    await session.execute(
        sa.update(MLModel)
        .where(MLModel.name == model_name, MLModel.version == version)
        .values(is_active=True)
    )
    return record


async def sync_registry(
    session: AsyncSession,
    *,
    name: str | None = None,
    model_dir: Path | None = None,
    settings: Settings | None = None,
) -> int:
    """Re-derive ``ml_models`` from the artefacts on disk; returns the number of rows written.

    The normal first-run repair: a clone ships ``models/*.joblib`` and a freshly migrated
    database has no ``ml_models`` rows at all, so ``GET /api/v1/ml/models`` would answer with an
    empty list while the API happily serves predictions. Running this once makes the two agree.
    """
    resolved = settings or get_settings()
    records = discover_models(name=name, model_dir=model_dir, settings=resolved)
    for record in records:
        await register_model(session, record, activate=record.is_active)
    return len(records)
