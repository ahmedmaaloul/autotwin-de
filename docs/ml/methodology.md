# ML methodology — energy consumption

> **Read the LIMITATIONS section before quoting any number on this page.**
> The model is trained on **simulated telemetry**. What is validated here is the *methodology* —
> the feature contract, the leak-free split, the like-for-like baseline comparison, the
> attribution — and **not** the energy behaviour of any real vehicle.

This page documents `autotwin_ml.features`, `dataset`, `training`, `registry`, `inference` and
`explain`, which together implement BUILD_SPEC §10.2. The physical model they are measured
against is documented separately in [`energy-model.md`](energy-model.md).

---

## 1. The question

A gradient-boosted regressor is only worth shipping if it beats the transparent model it is
supposed to improve on. The whole pipeline is built around one comparison:

> On rows that neither model has seen, does LightGBM predict consumption in kWh/100 km better
> than the road-load model of BUILD_SPEC §10.1?

Everything below exists to make that answer hard to fake.

---

## 2. The data

### 2.1 What one row is

One row is one **aggregated telemetry window** — 60 simulated seconds inside a single trip,
produced by `python -m autotwin_simulator.cli generate-training-data` and written to
`data/gold/training/energy_windows.parquet`. The 32-column schema is a contract shared with the
simulator; `autotwin_ml.dataset.TRAINING_SCHEMA` is the machine-readable copy that the loader
validates against, and the simulator's output conforms to it exactly (only the three ordinal
columns arrive as `Int32` rather than `Int64`, which the feature builder normalises).

Four columns are easy to misread, so they are defined here:

| column | definition |
|---|---|
| `speed_kmh` | Mean speed over the window **including standstill** = `distance_km / (duration_s/3600)`. The speed the steady-state baseline is evaluated at. |
| `avg_speed_kmh` | Mean **running** speed, over moving samples only. Equal to `speed_kmh` in free flow, higher in stop-and-go; the gap between them is itself a congestion signal. |
| `segment_distance_km` | Length of the *homogeneous road segment* the window sits in — a route-geometry property. The distance the window actually covered is `distance_km`, which is deliberately **not** a feature. |
| `accel_events_per_km` | Count of 1 Hz samples with `|a| > 0.5 m/s²` per kilometre. 0.5 m/s² is the usual drive-cycle cut: below it a passenger cannot feel the change. |

### 2.2 The dataset this model was trained on

| property | value |
|---|---|
| source | `autotwin_simulator` telemetry windows |
| rows | 24 149 (24 148 after cleaning) |
| trips | 266 |
| `data_origin` | `simulated` (every row) |
| SHA-256 | `1f30bac0921f7a6e8b27d03f049b034f859206c647cc3e200f08d5ce99d6aa60` |

The SHA-256 is recorded in the artefact's metrics file under
`metrics.training_data_source`, because the simulator regenerates the Parquet on every run and
the numbers on this page move by a few percent between regenerations.
**`python -m autotwin_ml.cli evaluate` re-scores the stored artefact against whatever is on
disk now, so a drift is one command away from being visible.**

`describe_training_source()` reports `kind = "unknown"` for this file, which is correct and not
a defect: the simulator does not write a provenance sidecar, so there is nothing to verify. The
fingerprint (path, size, SHA-256, mtime) is recorded anyway, so a model whose provenance cannot
be *named* can still be tied to the exact bytes it was trained on. When a sidecar *is* present
its checksum is checked against the file, and a stale one is discarded rather than believed —
attaching a false provenance to a real training run is worse than admitting ignorance.

### 2.3 The physics sweep — the documented fallback

`autotwin_ml.dataset.generate_physics_sweep()`, reachable as
`python -m autotwin_ml.cli generate-data --trips 400`, writes a dataset in the *same* schema
from a seeded sweep over the physical model. It exists because the ML half of the project was
built while the simulator was still being written and needed a reproducible dataset that did
not depend on a 40-vehicle run finishing first. It is not a disguise: it writes a sidecar whose
SHA-256 is bound to the Parquet, and the trainer carries `kind="physics_sweep"` into the metrics
file and the `ml_models` row, so a model trained on a sweep can never be reported as trained on
simulator output.

How it works, in one paragraph: 400 synthetic journeys, each with one of the five generic
vehicle profiles, one of three route archetypes (Autobahn corridor / regional / urban), one
day's German weather, a warming pack and a draining battery. Every 60 s window generates a 1 Hz
speed trace from an Ornstein–Uhlenbeck process clipped to a real EV's acceleration envelope, and
the label is the **integral of the physical model over all 60 samples** using signed
instantaneous battery power, plus 2 % noise. The raggedness parameter was *calibrated* rather
than guessed — its first setting produced a mean |acceleration| of 0.39 m/s² on free-flowing
Autobahn, roughly three times reality, which inflated the baseline's error and manufactured the
ML model's advantage. It was reduced until the per-regime statistics were right (0.20 m/s² at
`low` severity rising to 0.70 at `severe`).

Results on the sweep are reported in §5.3 as a cross-check, and its LIMITATIONS are §8.

### 2.4 Cleaning

`clean_training_frame()` drops rows that cannot be true, attributing each to the **first** rule
it fails so the counts sum to the total rejected. On the simulator dataset exactly one row is
dropped out of 24 149:

| rule | rows |
|---|---|
| `implausible_low_target` (target < 1 kWh/100 km) | 1 |
| everything else (nulls, non-finite, zero distance/duration, SOC out of range, implausible speed or temperature, duplicate window) | 0 |

That the eleven other rules fire zero times on 24 149 rows of simulator output is itself a
result worth stating: the simulator and this loader agree on what a valid window is. (On the
physics sweep the same rule rejects 3.8 % of rows, all of them sustained descents — see §8.4.)

---

## 3. The split — grouped by `trip_id`, and how much it matters

Consecutive windows of one trip share the vehicle, the weather, the driver and often the same
stretch of Autobahn a minute apart. A random *row* split puts window 41 of a trip in training
and window 42 in test, and the model is then scored on rows whose answer it has effectively
already seen. `split_by_trip()` assigns whole trips: a trip is wholly in train, wholly in
validation, or wholly in test.

Trips are shuffled once under the seed, then walked in that order, each going to the fold
furthest below its target **row** count. Assigning by rows rather than by trip count matters
because a 90-window Autobahn run and a 20-window city trip are not interchangeable units.

| fold | rows | trips |
|---|---|---|
| train | 16 899 | 182 |
| validation | 3 649 | 42 |
| test | 3 600 | 42 |

### 3.1 The leak, measured rather than asserted

The evaluation rows were held **fixed**: half of the test trips' windows were moved into
training (the leak), and both models were scored on the *same* remaining half.

**Simulator dataset:**

| training set | trees | MAE | RMSE | R² |
|---|---|---|---|---|
| grouped — no sibling windows | 1 109 | **2.331** | 3.279 | 0.8654 |
| leaked — sibling windows in train | 552 | 2.035 | 2.954 | 0.8908 |

The leak buys a **12.7 % apparent MAE improvement and +0.025 R² that the model has not earned.**
That is the effect the grouped split exists to prevent, and on this data it is large enough to
change how a reader would judge the model.

**Physics sweep, same experiment:**

| training set | trees | MAE | RMSE | R² |
|---|---|---|---|---|
| grouped | 339 | 2.180 | 2.951 | 0.9255 |
| leaked | 458 | 2.212 | 3.025 | 0.9217 |

Here the leak is **within noise** — and the contrast is the interesting part. The sweep
resamples gradient, road class, traffic state and target speed at every road segment, so its
within-trip autocorrelation is weak. Real simulated driving keeps a vehicle on the same
corridor at the same speed for twenty consecutive minutes, and the sibling windows are then
genuinely close to duplicates. **A protocol that looks unnecessary on synthetic data is
exactly the one that turns out to matter on real data**, which is the argument for choosing the
protocol on principle rather than on whichever dataset happens to be in front of you.

---

## 4. Training

LightGBM (`LGBMRegressor`), with scikit-learn's `HistGradientBoostingRegressor` as a real
fallback for platforms where LightGBM's `libomp` dependency will not load. `--algorithm auto`
(the default) prefers LightGBM and falls back with a logged warning; an explicit
`--algorithm lightgbm` on a machine without it **fails** rather than quietly training something
else and labelling the artefact wrongly.

Both backends early-stop on **our** validation fold. LightGBM does it natively; the sklearn
path cannot, because `early_stopping=True` carves out a *random* internal slice and would
reintroduce exactly the leakage the grouped split removes — so `_fit_sklearn` adds trees in
blocks of 50 under `warm_start`, scores each block on the real validation fold, and refits once
at the best size so the saved model carries no trees past the optimum.

`n_estimators` is a **budget, not a target**, for both backends (4 000). A run that ends at the
cap has not converged and its tree count would be an artefact of the constant rather than a
property of the data. Both runs below stopped well inside it.

### 4.1 Capacity, chosen on validation under a size budget

BUILD_SPEC §10.2 requires the artefact to be committed, so capacity was chosen on the
**validation fold only** — never on test — with the artefact size as an explicit second
criterion:

| config | trees | validation MAE | artefact |
|---|---|---|---|
| 63 leaves, lr 0.05 | 2 027 | 2.486 | ~2.5 MB |
| 31 leaves, lr 0.05 | 2 036 | 2.457 | ~2.5 MB |
| **31 leaves, lr 0.08** (chosen) | **1 023** | **2.479** | **1.2 MB** |
| 15 leaves, lr 0.08 | 607 | 2.528 | ~0.4 MB |

The chosen row gives up 0.9 % of validation MAE against the best and halves the file. The
extremes are both worse choices: 63 leaves is strictly dominated (more capacity, *worse*
validation error — it memorises), and 15 leaves gives up 2.9 % for a saving nobody needs.

---

## 5. Results

`energy_consumption v1` · LightGBM · 1 023 trees · seed 20260214 · simulator dataset
`1f30bac0…`. **Test fold: 3 600 windows from 42 trips, never touched during training or early
stopping.**

| predictor | MAE | RMSE | R² |
|---|---|---|---|
| **ML model (LightGBM)** | **2.053** | **2.904** | **0.8961** |
| Physical baseline (BUILD_SPEC §10.1) | 3.265 | 4.935 | 0.6998 |

All figures in kWh/100 km. The ML model lowers MAE by **37.1 %** on identical rows.

Train MAE 1.056 / R² 0.978 against validation MAE 2.479 / R² 0.898. The gap is real and is
mostly a property of the grouping rather than of over-fitting: within-trip rows are easy,
a *previously unseen trip* is hard, and the validation fold contains only unseen trips.

Test-fold residuals: mean −0.09, median −0.04, p05 −4.65, p95 +4.24, σ 2.90 kWh/100 km. The
model is essentially unbiased, with a symmetric error distribution.

### 5.1 The scikit-learn fallback, and why LightGBM is still what ships

Trained on exactly the same split, `HistGradientBoostingRegressor` reaches **MAE 2.002 /
RMSE 2.898 / R² 0.8965** at 3 050 trees — very slightly *better* than LightGBM. LightGBM ships
anyway: 1.2 MB against 4.6 MB of artefact and roughly 5 s against 70 s of training, for 2.5 % of
MAE. Saying so plainly matters more than the 2.5 %; a fallback that quietly outperforms the
primary is exactly the kind of fact a report is tempted to leave out.

The two landing within 3 % of each other is also the point of running both: two independent
implementations of the same algorithm family agreeing is a check that neither is doing
something peculiar with the feature matrix.

### 5.2 Where the baseline loses, and why it is not a strawman

The baseline is the *full* road-load model evaluated per row at that row's real speed, gradient,
temperature, distance and duration — reconstructed from the very columns the ML model was given.
The one thing it cannot see is within-window speed *variation*, and that is the definition of a
steady-state model, not a handicap imposed for the comparison.

Its error is therefore not random. It under-predicts, and it under-predicts more the more
transient the driving is:

| regime | n | actual | baseline | bias |
|---|---|---|---|---|
| motorway | 3 210 | 25.55 | 24.02 | −1.53 |
| primary | 56 | 18.23 | 15.77 | −2.46 |
| tertiary | 20 | 19.21 | 14.35 | −4.86 |
| traffic `low` | 2 898 | 25.06 | 23.65 | −1.41 |
| traffic `moderate` | 664 | 24.13 | 20.91 | −3.21 |
| **overall** | 3 600 | 24.84 | 23.09 | −1.75 |

That ordering is the whole story in one table. On a cruising Autobahn segment the steady-state
model is within about 6 %, which is what a reader should expect and a check that the comparison
is fair. On slower, more variable roads it is 15–25 % low, because the energy lost to repeated
acceleration and imperfect recuperation is invisible to a model evaluated at the mean speed.
**The ML model's gain is that specific correction, not a general superiority.**

Note also what this test fold *is*: 89 % motorway, gradients within ±2.4 %, `accel_events_per_km`
zero at the median. The simulator's demo corridors are Autobahn routes, so the regimes where the
baseline is weakest are under-represented — see §8.5.

### 5.3 Cross-check on the physics sweep

The same pipeline, unchanged, on the 400-trip sweep (test fold 2 950 windows / 62 trips):
ML **MAE 2.167 / R² 0.9253** against baseline **MAE 3.704 / R² 0.7624** — a 41.5 % improvement.
Two datasets built by different code, with different regime mixes, give the same qualitative
answer and a similar margin, which is more reassuring than either number alone.

### 5.4 Prediction behaviour on hand-built segments

`EnergyPredictor.predict` on a `sedan_ev`, ML beside the baseline it is computed with (from the
sweep-trained model; the ordering is the same for the simulator-trained one):

| segment | ML | baseline | Δ |
|---|---|---|---|
| Autobahn 130 km/h, +20 °C | 20.53 | 19.44 | +5.6 % |
| Autobahn 130 km/h, −8 °C | 27.84 | 27.07 | +2.9 % |
| Landstraße 90 km/h, +10 °C | 16.19 | 14.61 | +10.7 % |
| Stadt 30 km/h, severe traffic, 0 °C | 20.16 | 16.44 | +22.6 % |
| Autobahn 120 km/h, +4 % gradient | 41.23 | 43.53 | −5.3 % |

The correction grows monotonically with how transient the regime is. It is not a constant
offset and it is not noise — it is the behaviour the training data implies.

---

## 6. Attribution (SHAP)

`shap.TreeExplainer` — exact tree SHAP, verified working on this platform for **both** LightGBM
boosters and `HistGradientBoostingRegressor`. If it ever fails to build, `explain.py` falls back
to SHAP's model-agnostic permutation explainer and stamps `method="permutation"` plus a
`degraded_reason` on every report it produces, so a consumer is never silently handed a
different quantity under the same name.

Global importance is computed **once at training time** over 2 000 test rows and stored in
`metrics.shap`; `GET /api/v1/ml/explain/global` serves it from there. Per-prediction
contributions are computed per request; the explainer is cached per artefact, keyed on the
artefact's mtime so a retrain in place invalidates it.

### 6.1 Global mean |SHAP|, `energy_consumption v1`

Base value (mean prediction) 23.619 kWh/100 km.

| rank | feature | mean \|SHAP\| | share | label (de) |
|---|---|---|---|---|
| 1 | `gradient_percent` | 5.3398 | 28.7 % | Steigung |
| 2 | `speed_kmh` | 3.5181 | 18.9 % | Durchschnittsgeschwindigkeit |
| 3 | `mass_kg` | 2.7974 | 15.0 % | Fahrzeugmasse |
| 4 | `acceleration_abs_mean_ms2` | 1.0617 | 5.7 % | Mittlere Beschleunigung |
| 5 | `outside_temperature_c` | 1.0452 | 5.6 % | Außentemperatur |
| 6 | `avg_speed_kmh` | 1.0274 | 5.5 % | Fahrgeschwindigkeit ohne Stillstand |
| 7 | `segment_distance_km` | 0.5399 | 2.9 % | Segmentlänge |
| 8 | `traffic_severity_ordinal` | 0.4746 | 2.5 % | Verkehrslage |
| 9 | `hvac_load_kw` | 0.4694 | 2.5 % | Klimatisierungsleistung |
| 10 | `battery_temperature_c` | 0.4450 | 2.4 % | Batterietemperatur |

### 6.2 How to read it

**`gradient_percent` at 28.7 % is correct, not a bug.** Potential energy dominates every other
term over a 60 s window: a ±2 % gradient at 120 km/h moves the wheel power by roughly ±14 kW,
comparable to the entire rolling-plus-aero load. A model that did *not* put gradient first would
be wrong.

**`mass_kg` at 15.0 % is the vehicle-class signal.** The five profiles differ by 800 kg and by
7.5 kWh/100 km of nominal consumption, and mass enters the rolling, gradient and inertia terms
at once, so it acts as the identity of the vehicle as much as as a physical parameter.

**`acceleration_abs_mean_ms2` + `avg_speed_kmh` + `speed_std_kmh` + `accel_events_per_km` ≈ 13 %
is the ML model's actual contribution.** These four features have *no counterpart in the
physical baseline* — they describe within-window variation a steady-state model cannot express.
The share is modest and yet accounts for most of the 37 % MAE improvement, because it is
concentrated exactly where the baseline is wrong.

**Temperature is split across three features** (`outside_temperature_c` 5.6 %, `hvac_load_kw`
2.5 %, `battery_temperature_c` 2.4 %) that are strongly correlated by construction —
`hvac_load_kw` is a deterministic function of the outside temperature. Shapley values divide
credit between correlated features rather than double-counting it, so the ~10.5 % combined
figure is the honest total and no individual row of that group should be quoted alone.

**Gain-based importance ranks `speed_std_kmh` fifth (4.1 % of gain, 3 629 splits) while SHAP
ranks it eleventh.** The disagreement is expected and informative: gain measures how useful a
feature was to the *fitting procedure*, SHAP how much it moves *predictions*. A feature split on
constantly to make small corrections scores high on the first and lower on the second. Both are
stored in the metrics file; only SHAP is rendered next to a prediction, because only SHAP is
additive — `base_value + Σ contributions == prediction`, asserted by
`PredictionExplanation.reconstruction_error` rather than assumed (it is 0.0 in practice).

### 6.3 Folding SHAP into the deterministic drivers

`features.INSIGHT_FACTOR_BY_FEATURE` maps 14 of the 20 features onto the driver ids of the
counterfactual ladder in `autotwin_ml.insights` (BUILD_SPEC §11), and
`PredictionExplanation.by_insight_factor()` aggregates contributions along it. The six unmapped
features — `soc_percent`, `segment_distance_km`, `precipitation_mm`, `mass_kg`, `drag_area`,
`nominal_consumption_kwh_100km` — describe the vehicle or the observation window rather than a
driver the trip could have avoided, and are summed under `"other"` so the aggregation still
totals correctly.

The two attributions remain **separate quantities**. The ladder is a counterfactual
decomposition of the *physical* model; SHAP is a decomposition of the *learned* model.
Presenting one as the other would be exactly the dishonesty this project is built to avoid.

---

## 7. Serving

`registry.py` keeps two stores and neither is redundant:

- **the models directory** is the serving truth — `energy_consumption_v1.joblib`, its
  `.metrics.json`, and a one-line `energy_consumption.active` pointer. A container that mounts
  only `models/` serves correctly with no database on the request path;
- **`ml_models`** is the queryable truth for `GET /api/v1/ml/models`.

`register_model()` writes the row from the artefact so the two cannot drift by accident, and
`sync_registry()` re-derives the table from the directory when they have drifted anyway — the
normal state of a fresh clone, which ships artefacts and an empty database.
`activate_model()` writes the filesystem pointer **first** and the database second, so a failed
database write leaves serving following the operator's intent rather than a database claiming a
version the API does not serve.

Feature names travel **inside** the joblib payload and are checked against `FEATURE_NAMES` at
load time. An artefact trained on a different feature contract raises `ModelNotAvailable`
instead of predicting on shifted columns — the failure mode that produces confident nonsense and
no exception.

Nothing trained yet? Every resolution path raises `ModelNotAvailable`, an `AutoTwinError` with
the stable code `model_not_available` and HTTP **503**, whose message names the command that
fixes it. The API can mount its ML routes unconditionally.

---

## 8. LIMITATIONS

**Read this before quoting any number above.**

1. **The labels are simulated. The model has never seen a real vehicle.** Every target was
   produced by the simulator integrating `autotwin_ml.baseline` — an educational engineering
   approximation, explicitly not an OEM battery model. The metrics in §5 validate that *the
   pipeline works*: the features are well-formed, the split does not leak, the baseline
   comparison is like-for-like, the attribution is additive, the artefact round-trips. They say
   **nothing** about how accurately any real EV consumes energy. `training_data_origin =
   simulated` is carried into the artefact, the metrics file, the `ml_models` row and the API
   response so this cannot be lost downstream.

2. **The model's ceiling is the physical model's correctness.** The regressor learns the
   residual between a steady-state evaluation and a finer-grained integration *of the same
   physics*. Where that physics is wrong — the HVAC envelope has no humidity, solar or occupancy
   term; drivetrain efficiency is a flat 0.90 rather than a torque/speed map; there is no
   charge-acceptance limit and no brake blending — the ML model reproduces the error faithfully
   and confidently. It cannot discover a term the simulation does not contain.

3. **The dataset moves.** The simulator regenerates the Parquet on every
   `generate-training-data` run, and the numbers on this page shift by a few percent between
   regenerations (observed: test MAE 2.05–2.35 across three regenerations of comparable size).
   The SHA-256 of the exact file is in the metrics; `python -m autotwin_ml.cli evaluate` re-scores
   the stored artefact against whatever is on disk now.

4. **The physics sweep drops sustained descents.** 3.8 % of sweep windows are rejected by
   `implausible_low_target`, all of them long downhills where the baseline's recuperation
   cancels the window entirely (no charge-acceptance limit). A model trained on the sweep has
   never seen a steep descent and will over-predict there. The simulator dataset does not have
   this problem (1 row in 24 149) because its corridors are not that steep.

5. **The simulator test fold is 89 % motorway.** Gradients stay within ±2.4 %, the median window
   has zero acceleration events, and `severe` traffic does not occur at all. That is the regime
   where the baseline is *strongest*, so the 37 % improvement is measured on the conservative
   side — but it also means the model's behaviour in city traffic is extrapolation, supported by
   the sweep's results (§5.3) rather than by this dataset.

6. **Only five vehicles exist.** `mass_kg`, `drag_area` and `nominal_consumption_kwh_100km` take
   five distinct value triples. The model has learned five points, not a continuum. A prediction
   for a vehicle between or outside them is an extrapolation the metrics do not cover.
   `EnergyPredictor.predict_features` flags the related problem explicitly: `c_rr` is not one of
   the 20 features, so a raw feature vector cannot fully specify a vehicle and the baseline it
   is compared against is marked `baseline_is_approximate`.

7. **No uncertainty estimate.** The model returns a point prediction. Residual quantiles over
   the test fold (`metrics.residual_summary`) are the only spread information available, and
   they are a property of that fold, not a calibrated per-prediction interval.

8. **No temporal validation.** The simulated timeline is short and synthetic, so there is no
   meaningful train-on-past/test-on-future protocol. On real telemetry a time-based split should
   be added on top of the grouping.

---

## 9. Reproducing

```bash
python -m autotwin_simulator.cli generate-training-data     # the real training set
python -m autotwin_ml.cli generate-data --trips 400         # or the physics-sweep stand-in

python -m autotwin_ml.cli train                             # LightGBM, or sklearn where it must
python -m autotwin_ml.cli train --algorithm sklearn --no-activate
python -m autotwin_ml.cli evaluate                          # re-score the stored artefact
python -m autotwin_ml.cli explain --top 10 --out models/shap.json
```

Every command is deterministic given its input Parquet and `AUTOTWIN_SIM_SEED` (default
20260214): two `train` runs on the same file produce the same MAE to the last digit, and
`generate-data` twice with the same seed produces byte-identical Parquet. `evaluate` reproduces
the split from the seed, so its "as trained" row and its recomputed row must match — if they do
not, the dataset has changed underneath the artefact.
