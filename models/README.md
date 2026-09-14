# `models/` — trained artefacts

Everything the API needs to serve `POST /api/v1/ml/predict` lives in this directory. It is
committed on purpose: a fresh clone can answer a prediction with no database, no training run
and no download.

> The model here is trained on **simulated telemetry**. `training_data_origin` is `simulated`
> in the artefact, the metrics file, the `ml_models` row and every API response.
> See [`docs/ml/methodology.md`](../docs/ml/methodology.md), especially LIMITATIONS.

---

## Layout

| file | what it is |
|---|---|
| `energy_consumption_v<N>.joblib` | the estimator, plus the feature contract it was trained on |
| `energy_consumption_v<N>.metrics.json` | metrics, SHAP, feature importance, scatter points, residuals, split description, data provenance |
| `energy_consumption.active` | one line naming the version the API serves |

`<N>` is an unpadded integer. `autotwin_ml.registry` parses it and orders numerically, so `v10`
comes after `v9`.

### The joblib payload

A dict, not a bare estimator:

```python
{"format_version": 1, "name": ..., "version": ..., "algorithm": ...,
 "trained_at": "...", "feature_names": [...20 names...], "estimator": <fitted model>}
```

The feature names travel **inside** the artefact and are checked against
`autotwin_ml.features.FEATURE_NAMES` on every load. A booster is a column-*position* machine: an
artefact trained before a feature was inserted would otherwise keep predicting confidently on
shifted columns. That check turns the worst silent failure in the stack into a
`ModelNotAvailable` at load time.

### The metrics file

`metrics.*` is exactly what goes into the `ml_models.metrics` JSONB column (BUILD_SPEC §3.2):
the six headline numbers — `mae`, `rmse`, `r2`, `baseline_mae`, `baseline_rmse`, `baseline_r2` —
at the top level, with the richer report nested beside them:

`split` · `cleaning` · `training_data_source` · `train` · `validation` · `feature_importance[]` ·
`shap.global_importance[]` · `predicted_vs_actual[]` (400 points) · `residuals[]` ·
`residual_summary` — which is what `GET /api/v1/ml/metrics` and the `/ml` page render.

---

## What is committed

| version | algorithm | active | test MAE | test R² | baseline MAE | baseline R² | artefact |
|---|---|---|---|---|---|---|---|
| `v1` | LightGBM, 1 023 trees | **yes** | **2.053** | **0.8961** | 3.265 | 0.6998 | 1.2 MB |

kWh/100 km, on an untouched test fold of 3 600 telemetry windows from 42 held-out trips
(simulator dataset `1f30bac0…`, 24 149 rows / 266 trips). **Total committed weight ≈ 1.25 MB.**

### The scikit-learn fallback is verified but not committed

BUILD_SPEC §10.2 requires a working fallback for platforms where LightGBM's `libomp` dependency
will not load, and a fallback that has never been run is not a fallback. It was trained on the
identical split and reaches **MAE 2.002 / RMSE 2.898 / R² 0.8965** at 3 050 trees — very
slightly *better* than the LightGBM model that ships.

It is not committed because the artefact is **4.6 MB against 1.2 MB** and training takes ~70 s
against ~5 s, which is a poor trade for 2.5 % of MAE in a repository whose model directory is
meant to be cheap to clone. Reproduce it in one command:

```bash
python -m autotwin_ml.cli train --algorithm sklearn --no-activate
```

`--no-activate` leaves `v1` serving, so the comparison costs nothing but disk.

---

## Commands

```bash
# Train a new version. Writes both files, activates the version, inserts an ml_models row.
python -m autotwin_ml.cli train

# Train without disturbing what is currently served, e.g. to compare a backend.
python -m autotwin_ml.cli train --algorithm sklearn --no-activate

# Retrain a version in place (the registry picks it up by mtime, no API restart needed).
python -m autotwin_ml.cli train --version 1

# Re-score a stored artefact against the physical baseline on the same rows.
python -m autotwin_ml.cli evaluate --version 1

# Read the global SHAP ranking; --out also writes it as JSON.
python -m autotwin_ml.cli explain --top 10
```

Training needs `data/gold/training/energy_windows.parquet`. Produce it with
`python -m autotwin_simulator.cli generate-training-data` (simulated telemetry, the real source)
or `python -m autotwin_ml.cli generate-data` (the documented physics-sweep stand-in, used while
the simulator was being written).

The simulator rewrites that Parquet on every run, so the committed artefact's numbers will
diverge from a freshly regenerated dataset. The file's SHA-256 is recorded in the metrics, and
`evaluate` re-scores the stored artefact against whatever is on disk now — if its "as trained"
row and its recomputed row disagree, the data has moved and `train` should be re-run.

---

## Activation

`energy_consumption.active` is the serving pointer and the filesystem is authoritative for it.
Resolution order without an explicit version: the pointer if it names a version that exists,
otherwise the highest version present — so a fresh clone serves its committed artefact with no
activation step.

`autotwin_ml.registry.activate_model()` writes the pointer **first** and the database second. If
the database write then fails, serving still follows the operator's intent and the row can be
repaired with `sync_registry()`; the reverse order would leave a database claiming a version the
API does not actually serve.

---

## With nothing trained

Deleting everything here is a supported state. Every resolution path then raises
`ModelNotAvailable` — an `AutoTwinError` with the stable code `model_not_available` and HTTP
**503** — whose message names the command that fixes it. The API mounts its ML routes
unconditionally and answers with a correct status code rather than failing at import.
