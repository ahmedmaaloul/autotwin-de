# The AutoTwin DE energy model

> **Read this section first.**
>
> This is an **educational engineering approximation**, not an OEM battery model. It is a
> textbook longitudinal road-load model with a hand-drawn auxiliary-load envelope on top. It has
> been calibrated against **nothing**: no dynamometer run, no fleet telemetry, no manufacturer
> data. The only numbers it was fitted to are public, class-level, order-of-magnitude figures
> for a *vehicle segment* — "a 77 kWh sedan", never a specific car.
>
> It will tell you that a 2 100 kg sedan uses about 17.6 kWh/100 km at a steady 120 km/h at
> 20 °C. That is the right order of magnitude and a defensible engineering estimate. It is not a
> range prediction, it must not be used to decide whether a real vehicle reaches a real charger,
> and every consumption figure AutoTwin DE shows in its UI is labelled accordingly.
>
> The reason the model exists is not to be accurate. It exists to be **transparent**: it is the
> baseline the machine-learning regressor of §10.2 has to beat on the same held-out test set,
> and it is the mechanism the deterministic insight generator of §11 uses to attribute energy to
> causes. Both jobs need a model whose every number a reader can recompute on paper. This one is.

---

## 1. The model

Implemented in `services/ml/src/autotwin_ml/baseline.py` as `PhysicalEnergyModel`, exactly as
BUILD_SPEC §10.1 specifies. All SI units internally; kWh and km only at the boundary.

### 1.1 Force balance at the wheel

```
F_roll  = c_rr · m · g · cos(theta)                        [N]   rolling resistance
F_aero  = 0.5 · rho(T) · c_d · A · v_air · |v_air|         [N]   aerodynamic drag
F_grade = m · g · sin(theta)                               [N]   gradient
F_inert = m · a · 1.05                                     [N]   inertia + rotating mass
P_wheel = (F_roll + F_aero + F_grade + F_inert) · v        [W]
```

`theta = atan(gradient_percent / 100)`. The exact trigonometry is used rather than the
small-angle shortcut `sin(theta) ≈ gradient/100`; the difference is under 0.2 % on any public
road, but keeping `cos` and `sin` consistent costs nothing.

`v_air = v + headwind`. With no headwind — the default, and the only honest option when the
wind direction is unknown — this reduces to the specification's `0.5 · rho · c_d · A · v²`.
The signed square `v_air · |v_air|` keeps a tailwind stronger than the vehicle speed *pushing*
rather than flipping into a spurious drag force.

### 1.2 Wheel to battery

```
k_cold  = 1 + max(0, 15 - T_batt) · 0.008

P_batt  = P_wheel / 0.90 · k_cold                          if P_wheel >= 0
        = max(P_wheel · 0.65 / k_cold, -50 000 W)          if P_wheel <  0

P_aux   = 0.35 kW + HVAC(T_outside)

E_kwh   = (P_batt + P_aux) · t / 3.6e6
```

**The cold-battery factor is applied asymmetrically, and that is a deliberate deviation from a
literal reading of the spec.** BUILD_SPEC §10.1 says the factor is "applied to `P_batt`", which
it writes with the traction case in mind. Applied literally on the recuperation branch it would
*multiply* a negative number by 1.20 at -10 °C and make a cold pack recover **more** energy than
a warm one — the opposite of the physics, where a cold cell's higher internal resistance and its
charge-acceptance limit both cut recuperation hard. So the factor multiplies traction and
divides recuperation: a penalty in both directions.

### 1.3 The decomposition

`EnergyResult` carries the energy split by term, and the split is what makes §11's deterministic
explanation possible. The rule is **proportional allocation**: the specification applies the
drivetrain conversion to the *sum* of the four forces, so every term is converted to the battery
side with the same multiplier `k = P_batt / P_wheel` that the sum used. That makes the buckets
add up to the traction energy exactly, by construction, and it absorbs the -50 kW recuperation
floor into `k` automatically.

The additive identity, asserted by the unit tests and by `EnergyResult.components_kwh`:

```
kwh == rolling_kwh + aero_kwh + gradient_kwh + inertia_kwh + auxiliary_kwh + hvac_kwh
```

`regen_kwh` is a **memo, not a seventh addend**. It reports how much energy the battery actually
got back and is already contained in the negative gradient and inertia buckets. Adding it again
double-counts recuperation.

### 1.4 Guards

| Situation | Behaviour | Why |
|---|---|---|
| `duration <= 0` | all-zero result | no time, no energy |
| `speed <= 0` | auxiliary + HVAC only, no distance | a car standing in a jam still heats its cabin |
| `P_wheel == 0` | all four traction buckets zero | avoids a division by zero in `k` |
| segment total would be negative | recuperation credit scaled back so the total is exactly 0 | see §4.2 |
| `distance_km < 0`, `|gradient| > 30 %`, `|T| > 60 °C`, `speed > 400 km/h` | `ValidationError` (HTTP 422) | catches unit mix-ups and unconverted DWD `-999` sentinels before they become plausible-looking output |

---

## 2. Every constant, and where it comes from

All of them live in one frozen dataclass, `autotwin_ml.constants.PhysicsConstants`, so that a
trained model artefact can be stored next to the constant set that produced its training data.

| Constant | Value | Source / justification |
|---|---|---|
| `gravity_ms2` | 9.80665 m/s² | Standard gravity, CGPM 1901 conventional value. |
| `air_density_reference_kg_m3` | 1.225 kg/m³ | Dry air at the ISA sea-level reference state (15 °C, 1013.25 hPa). |
| `air_density_reference_temperature_c` | 15 °C | The 288.15 K in BUILD_SPEC's `rho = 1.225 · 288.15 / (273.15 + T)`. |
| `drivetrain_efficiency` | 0.90 | Inverter + motor + single-speed reduction gear, combined and **flat**. Real efficiency is a map over torque and speed peaking near 0.95 and collapsing below 0.80 at very low load. Learning that shape is the ML model's job; keeping it flat is what makes the baseline recomputable by hand. |
| `regen_efficiency` | 0.65 | Lower than the traction efficiency because recuperation pays the drivetrain losses a second time (wheel → motor → inverter → cells) and because brake blending gives part of every real deceleration to the friction brakes. |
| `regen_power_floor_w` | -50 000 W | Stands in for motor peak generator torque, inverter current limit, and above all the battery's charge-acceptance power, which a BMS holds far below the discharge limit. |
| `rotating_mass_factor` | 1.05 | Wheels, half-shafts, reduction gear and rotor have to be spun up too. Conventionally folded into the translational equation as a 4-6 % equivalent mass surcharge for a single-speed EV driveline. |
| `auxiliary_base_kw` | 0.35 kW | The always-on 12 V side: control units, lighting, pumps, DC/DC converter loss, infotainment. Kept separate from HVAC because it does not vanish at 20 °C. |
| `cold_battery_reference_temperature_c` | 15 °C | Above this, no internal-resistance penalty. |
| `cold_battery_resistance_per_kelvin` | 0.008 /K | Li-ion internal resistance roughly doubles between +25 °C and -10 °C; 0.8 %/K over a 35 K span reproduces that order of magnitude with one linear term instead of an Arrhenius fit there is no data to calibrate. |

### 2.1 The HVAC envelope

`HVAC_CURVE_C_KW`, piecewise-linear, **clamped** outside the tabulated range (extrapolating a
hand-drawn envelope past -20 °C or +45 °C would be false precision):

| T (°C) | -20 | -10 | 0 | 10 | **20** | 25 | 30 | **35** | 45 |
|---|---|---|---|---|---|---|---|---|---|
| HVAC (kW) | 4.5 | **3.5** | 2.2 | 0.9 | **0.0** | 0.5 | 1.2 | **2.0** | 3.2 |

The three bold anchors are exactly the ones BUILD_SPEC §10.1 names. The rest interpolate along
the shape reported for mid-size European EVs: heating dominates the winter load (a resistive
heater alone draws 4-6 kW at -20 °C, a heat pump roughly half that, so 4.5 kW is a fleet-average
compromise), while air conditioning peaks far lower because the compressor only has to reject
heat across a 10-15 K gradient.

**This is the single largest source of modelling error at low temperatures.** It has no humidity
term, no cabin pre-conditioning, no solar load, no occupancy, no heat-pump/resistive distinction
and no recirculation state.

---

## 3. What the model actually produces

Measured on 2026-09-14 against the five seeded profiles of BUILD_SPEC §9, steady state over
100 km, flat, no wind, no acceleration. Reproduce with
`PhysicalEnergyModel().consumption_kwh_per_100km(profile, SegmentConditions(...))`.

| profile | nominal | 80 km/h | 100 km/h | 120 km/h | 130 km/h | 120 km/h @ -10 °C |
|---|---|---|---|---|---|---|
| `compact_ev` | 15.5 | 11.03 | 14.01 | 17.70 | 19.81 | 25.78 |
| `sedan_ev` | 16.5 | 11.65 | 14.29 | **17.57** | 19.44 | **25.44** |
| `performance_ev` | 20.0 | 13.15 | 16.10 | 19.75 | 21.83 | 28.21 |
| `suv_ev` | 19.5 | 15.48 | 19.36 | 24.15 | 26.88 | 34.01 |
| `van_ev` | 23.0 | 19.51 | 25.04 | 31.86 | 35.74 | 44.16 |

Three properties worth checking, because they are what make the model usable at all:

**Aerodynamic drag is quadratic in speed, so the aero *energy per kilometre* is quadratic too.**
For `sedan_ev` over 100 km, the aero bucket is 0.683 kWh at 30 km/h, 2.731 at 60, 10.922 at 120
and 43.688 at 240 — a factor of exactly 4.000 for every doubling of speed. That is the single
most important qualitative behaviour in the model and the reason motorway speed dominates every
long-distance explanation.

**The cold penalty is large and mostly HVAC.** `sedan_ev` at 120 km/h goes from 17.57 to
25.44 kWh/100 km between +20 °C and -10 °C, +45 %. Of the 7.87 kWh/100 km difference, 2.92 is
cabin heating and 4.95 is the denser air plus the cold-pack resistance factor applied to the
road load (rolling 6.36 → 7.63, aero 10.92 → 14.60).

**A standing vehicle still consumes.** Ten minutes stationary costs 0.058 kWh at 20 °C and
0.642 kWh at -10 °C — the difference between an unnoticed and a very noticeable traffic jam.

### 3.1 A note on the 120 km/h figure

17.57 kWh/100 km for `sedan_ev` sits marginally **below** the 18-24 kWh/100 km band usually
quoted for a car of this class at a steady 120 km/h. That is expected, and it is a property of
the scenario rather than a defect:

- it is the most favourable case the model can be asked for — dead flat, perfectly still air,
  20 °C, zero acceleration, dry road, a single steady speed;
- the constants are BUILD_SPEC §9's class values, which are optimistic on `c_rr` (0.010, typical
  of a new low-rolling-resistance tyre at full pressure and a warm road);
- the model has no drivetrain efficiency map, no tyre temperature, no road-surface roughness and
  no precipitation term, and every one of those effects points the same way — upwards.

Add any real condition and the figure moves into the expected band immediately: 130 km/h gives
19.44, 120 km/h at 10 °C gives 19.41, and a 3 m/s headwind at 120 km/h gives 19.62. The
commonly quoted 18-21 corresponds to the model's *typical* rather than *ideal* input.

---

## 4. Validity envelope

### 4.1 Where the model is meant to be used

| Quantity | Valid range | Beyond it |
|---|---|---|
| Speed | 0-200 km/h | Rejected above 400 km/h. Between 200 and 400 the physics still evaluates but no constant was chosen with it in mind. |
| Ambient temperature | -20 … +45 °C | The HVAC curve clamps. Rejected beyond ±60 °C. |
| Gradient | -8 … +8 % | Rejected beyond ±30 %. German motorways stay under 6 %. |
| Segment length | ≥ ~1 km, or a telemetry window of ~60 s | Shorter, and "mean speed" and "mean gradient" stop describing the segment. |
| Vehicle | The five class profiles of §9 | Any `VehicleProfile` works numerically; the constants were chosen for passenger EVs of 1 700-2 500 kg. |

### 4.2 Known limitations, in descending order of impact

1. **Cyclic acceleration losses are invisible.** `F_inert` uses the *net* signed acceleration
   over a segment. A telemetry window whose speed starts and ends at 50 km/h has a net
   acceleration of zero however violent the driving was in between, and the model charges it
   nothing for the accelerate-brake-accelerate cycle that dominates urban and stop-and-go energy.
   This is the largest structural gap, and it is precisely what the ML features
   `acceleration_abs_mean_ms2` and `accel_events_per_km` exist to capture. **Consequence for
   explanations:** in this baseline, motorway congestion *reduces* energy, because it reduces
   speed and therefore drag. That is honest for free-flowing-to-slow motorway traffic and wrong
   for urban stop-and-go, and the insight generator's `traffic` driver must be read with that in
   mind.
2. **A segment can never be a net energy source.** If the recuperation credit exceeds what the
   segment's dissipative and auxiliary terms consumed, the credit is scaled back so the total
   lands exactly at zero (the scaling is applied to the negative buckets, so the additive
   identity survives). A real vehicle on a sustained 8 % descent *does* net-charge its pack, and
   over such a segment this model under-reports the recovery. The cap exists because the model
   has no brake blending and no state-of-charge-dependent charge-acceptance limit; without it, a
   long descent would refill the battery faster than any real vehicle. It is a deliberate
   conservative choice mandated by the build specification.
3. **Precipitation is ignored by the physics.** `SegmentConditions.precipitation_mm` is carried
   as an ML feature and reported in explanations, but no force term reads it. Wet-road rolling
   resistance and spray drag are real (a few percent), and the project has no data to calibrate
   a coefficient against. Inventing one would break BUILD_SPEC §0's no-invented-numbers rule.
4. **Wind needs a direction the data usually does not have.** DWD's 10-minute feed gives the
   scalar `FF_10`; the direction `DD_10` covers only part of the network. Where both are
   available, `headwind_component_ms(wind_speed, wind_direction, heading)` resolves a real
   headwind and the aerodynamic term uses it. Where they are not, the headwind stays 0 and **no
   wind effect is claimed** — the aerodynamic term is then exactly the specification's `v²` form.
5. **Drivetrain efficiency is a single flat number.** No torque/speed map, so low-load city
   driving is modelled as too efficient and high-load motorway cruising as slightly too lossy.
6. **The battery is a resistance, not a model.** One linear cold-temperature term. No
   state-of-charge-dependent internal resistance, no cell chemistry, no ageing, no thermal
   dynamics — `battery_temperature_c` is an input, never a state the model evolves.
7. **Payload, altitude and tyre pressure are absent.** Mass is the profile's kerb mass; air
   density ignores the actual pressure (a sub-percent correction that would add a missing-data
   path for the frequently-absent `PP_10`).

### 4.3 What would be needed to make this real

A dynamometer coast-down to measure `F_0`, `F_1`, `F_2` per vehicle instead of assuming
`c_rr`, `c_d` and `A`; a measured drivetrain efficiency map; a thermal model of the pack and the
cabin with a real heat-pump characteristic; and a fleet of instrumented vehicles to fit all of it
against. None of those are available to a zero-cost portfolio project, which is why the honest
answer is to publish the approximation *with its error bars stated* and let a learned model close
the gap where training data exists.

---

## 5. How the rest of the project uses it

| Consumer | Uses |
|---|---|
| `autotwin_simulator` | `instantaneous_power_kw` per tick, `segment_energy` per telemetry window. Produces the labelled-simulated training set. |
| `autotwin_ml.training` | The physical baseline's MAE / RMSE / R² on the same held-out test set as the LightGBM model — the comparison BUILD_SPEC §10.2 requires. |
| `autotwin_ml.insights` | Counterfactual re-runs, plus the `EnergyResult` decomposition, to attribute a route's energy to causes without an LLM. See §11 and the module docstring. |
| `autotwin_api` | `POST /api/v1/routes/analyze` — per-segment `kwh`, `kwh_per_100km` and `energy_intensity`. |
| `autotwin_ml.charging` | Segment energies drive the beam search's SOC arithmetic. |

`import autotwin_ml` pulls in **only** this pure-Python core — no LightGBM, SHAP, NumPy, polars or
joblib. The simulator calls the model on every vehicle tick and the API imports it at start-up;
dragging the gradient-boosting stack into either would cost hundreds of milliseconds and ~200 MB
of resident memory for code neither executes.

---

## 6. Reproducing the numbers in this document

```python
from autotwin_contracts.vehicles import get_vehicle_profile
from autotwin_ml import PhysicalEnergyModel, SegmentConditions

model = PhysicalEnergyModel()
sedan = get_vehicle_profile("sedan_ev")

conditions = SegmentConditions(speed_kmh=120.0, distance_km=100.0, outside_temperature_c=20.0)
result = model.segment_energy(sedan, conditions)

print(result.kwh_per_100km)  # 17.570...
print(result.aero_kwh)  # 10.922...
print(result.components_kwh)  # identical to result.kwh
```

Unit tests with known-value assertions live in `tests/` per BUILD_SPEC §13.
