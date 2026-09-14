# Simulated vehicle telemetry

> **This page describes data that AutoTwin DE makes up.**
>
> Every vehicle position, speed, state of charge, battery temperature and consumption figure in
> this project is produced by the simulator described below. **None of it is real fleet
> telemetry, and none of it may ever be presented as such.** No German OEM publishes connected-
> vehicle data, and none ever will without consent and a contract — telematics is proprietary
> and carries personal data. Simulating it is the honest option; presenting the result as
> measured would be fraud.
>
> The labelling is structural, not a disclaimer: `telemetry.data_origin = 'simulated'` on every
> row, `data_origin` in every Kafka envelope, `training_data_origin` on every registered ML
> model, and a `SIMULIERT` badge on every surface of the web app that is backed by it. See
> [ADR 004](../adr/004-simulation-vs-real-vehicle-data.md).

**What *is* real** in everything the simulator touches: the corridors (OSRM geometry over
OpenStreetMap), the road classes and speed limits (`route_segments`, from OSM), the charging
sites the vehicles stop at (Bundesnetzagentur Ladesäulenregister), the traffic events on the
corridors (Autobahn GmbH), and the weather when it has been ingested (DWD). The *vehicles* are
simulated. The *world* is, wherever possible, not.

---

## 1. What the simulator is

`services/simulator/src/autotwin_simulator/` — six modules along the causal chain of one
simulated second:

| module | question it answers |
|---|---|
| `environment.py` | Where am I? Road class, limit, gradient, weather, traffic, chargers. |
| `driver.py` | How fast do I want to go, and how do I get there? |
| `vehicle.py` | What does that cost? SOC, pack temperature, range, charging. |
| `engine.py` | N vehicles, a clock, a sink, and the `SimulationState` lifecycle. |
| `runner.py` | One asyncio task, started by FastAPI or by the CLI, stopped cleanly. |
| `training_data.py` | 60-second windows → `data/gold/training/energy_windows.parquet`. |

**Consumption is not computed in the simulator.** It comes from
`autotwin_ml.baseline.PhysicalEnergyModel` — the same object, with the same constants, that
`POST /api/v1/routes/analyze` serves predictions from. There is exactly one energy model in this
repository. That is deliberate, and it has a consequence that is stated wherever ML metrics are
reported: **the learned model is validated against data its own baseline generated.** The
interesting claim is the methodology, not the R².

---

## 2. The physics

Per integration step (1 simulated second by default), for each vehicle:

```
F_roll  = c_rr · m · g · cos(theta)
F_aero  = 0.5 · rho(T) · c_d · A · (v + v_head) · |v + v_head|
F_grade = m · g · sin(theta)
F_inert = m · a · 1.05
P_wheel = (F_roll + F_aero + F_grade + F_inert) · v
P_batt  = P_wheel / 0.90              if P_wheel >= 0, times the cold-battery factor
        = P_wheel · 0.65              if P_wheel <  0, divided by it, floored at -50 kW
P_aux   = 0.35 kW + HVAC(T_ambient)
E       = (P_batt + P_aux) · dt / 3.6e6      [kWh]
```

with `theta = atan(gradient_percent / 100)`, `rho(T) = 1.225 · 288.15 / (273.15 + T)` and the
cold-battery factor `1 + max(0, 15 - T_batt) · 0.008`. Every constant, including the HVAC
envelope, is documented in [`docs/ml/energy-model.md`](../ml/energy-model.md) and lives in
`autotwin_ml.constants.PhysicsConstants`. Vehicle parameters (mass, `c_d`, frontal area, `c_rr`,
capacities, charging power) are the five generic class profiles of BUILD_SPEC §9 — **public
order-of-magnitude figures for a vehicle class, not reverse-engineered OEM data.**

### Consequences worth knowing

- **State of charge never rises while driving.** The energy model caps a segment at zero net
  energy: a long descent cannot refill the pack, because the model has no brake blending and no
  charge-acceptance limit, and an uncapped credit would let a 6 % descent recharge a battery
  faster than any real car. Recuperation still shows up in `instantaneous_power_kw`, which is
  signed and goes negative on a descent or under braking.
- **Precipitation has no effect on consumption.** Wet-road rolling resistance and spray drag are
  real, a few percent. The project has no data to calibrate a coefficient against, so the
  physical model ignores precipitation entirely; it reaches the ML feature vector and the driver
  model (which slows down in rain) but not the force balance.
- **The drivetrain efficiency is a single flat 0.90.** Real efficiency is a torque-speed map that
  peaks near 0.95 and collapses below 0.80 at very low load. Learning that shape from the
  simulated windows is part of what the ML model is for.

### Battery temperature

Not part of the energy model; the simulator adds it, because the cold-battery penalty needs a
pack temperature and "assume it equals ambient" would make a car that has been driving hard for
an hour as cold as one that just woke up. Per step:

```
dT = (T_ambient - T) / 1800 s · dt                    passive exchange with the air
   + 0.63 K/kWh · |E_through_pack|                    ohmic self-heating
   - (T - 35 °C) / 600 s · dt        if T > 35 °C     active thermal management
```

`0.63 K/kWh` is **derived, not tuned**: 7 % of the energy through the pack is dissipated in it,
and the pack's heat capacity is 0.111 kWh/K (≈400 kg at ≈1000 J/(kg·K)). At a 25 kW motorway
load those two terms balance about 8 K above ambient; a 150 kW charging session runs the pack up
into the forties, where the cooling branch holds it. It warms under load and cools at rest, which
is the behaviour that matters. It is **an envelope, not a thermal model of a cooling circuit** —
no chiller capacity limit, no pre-conditioning, no cell-to-cell gradient.

### Range estimate

`estimated_range_km = usable_capacity · SOC / 100 / consumption · 100`, where `consumption` is a
**distance-weighted** exponential average of recent consumption with a 5 km half-life, capped at
five times the vehicle's nominal figure so that one hard acceleration cannot move it. Distance-
weighted rather than time-weighted because the quantity is kWh per 100 km: an hour at a
standstill must not dominate it.

---

## 3. The driver

Target speed (pure, checkable on paper):

```
v_target = cruise(road_class) · aggressiveness
         capped at speed_limit · limit_compliance   (when a limit is posted)
         ÷ TrafficSeverity.delay_factor
         × weather_factor(condition)
```

| road class | free-flow cruise |
|---|---|
| motorway | 130 km/h — the German *Richtgeschwindigkeit* |
| trunk | 110 |
| primary (Bundesstraße) | 100 |
| secondary (Landesstraße) | 85 |
| tertiary (Kreisstraße) | 70 |
| residential | 50 |
| service | 30 |
| unknown | 80 |

Traffic enters as a **divisor** using the same `TrafficSeverity.delay_factor` (1.0 / 1.15 / 1.4 /
2.0) the route analyser multiplies travel *time* by, so the simulator and the insight generator
agree about what a severity means. Weather multipliers: clear 1.00, rain 0.93, snow 0.75, fog
0.80, storm 0.88 — **stated, coarse, and not measured**; no public German dataset pairs road
weather with free-flow speed at this granularity.

Getting to the target is a first-order approach, `a = (v_target - v) / tau`, clipped to the
driver's comfort limits, plus a **correlated** noise term: an AR(1) process with an 8-second
correlation time and σ = 0.18 m/s², faded out below 15 km/h. The correlation is the point — white
noise on acceleration produces a trace no car has ever driven and would teach the ML model that
`acceleration_abs_mean_ms2` is uninformative. The reported acceleration is measured from the
speed change, not from the requested value, so the telemetry trace integrates correctly.

**Standstills.** Severity categories alone cannot produce stop-and-go: dividing the target speed
by 2.0 yields a vehicle gliding along a jammed Autobahn at a steady 65 km/h, which is not what a
jam is and not what it costs. A driver in `high` traffic therefore halts on average once every
15 minutes, and in `severe` traffic once every 3 minutes, for a uniform 8–50 s. The per-step
probability is `1 - exp(-rate · dt)`, so the halt frequency does not change when the speed factor
changes the step length.

### Per-driver parameters

Drawn once per vehicle from bounded distributions and then clamped, so no seed can produce an
implausible driver:

| parameter | distribution | clamp |
|---|---|---|
| `aggressiveness` | N(1.00, 0.09) | 0.80 – 1.20 |
| `limit_compliance` | N(1.05, 0.05) | 0.95 – 1.18 |
| `reaction_tau_s` | N(6.0, 1.5) | 3 – 12 s |
| `comfort_acceleration_ms2` | N(1.2, 0.25) | 0.6 – 2.2 |
| `comfort_deceleration_ms2` | N(1.6, 0.35) | 0.9 – 3.0 |
| `reserve_soc_percent` | `28 − 14 · aggressiveness + N(0, 2)` | 8 – 30 % |
| `departure_soc_percent` | N(80, 5) | 65 – 92 % |

The reserve is deliberately **correlated with aggressiveness**: the bold driver is also the one
who runs the battery down, and a timid speeder who plugs in at 25 % would be an incoherent
character. The 80 % departure target is where the charging taper knee sits, and where German
drivers actually unplug.

---

## 4. The environment: what is observed and what is synthetic

Every value is one of two kinds, and `RouteEnvironment` says which through
`weather_source` / `traffic_source`, which the engine logs and writes into
`simulation_runs.config`.

| field | source |
|---|---|
| corridor geometry, length, duration | **observed** — `routes`, from OSRM/OSM |
| road class, speed limit | **observed** — `route_segments`, from OSM; falls back to a class inferred from the corridor's own average speed when a corridor has not been analysed |
| charging sites | **observed** — `charging_stations`, from the Bundesnetzagentur register, DC ≥ 50 kW within 5 km of the corridor |
| traffic | **observed** — `traffic_events`, from Autobahn GmbH — where a corridor has ingested events; a seeded rush-hour model everywhere else |
| ambient temperature, precipitation, wind | **observed** — `weather_observations`, from DWD, averaged over the corridor and held constant for the run; a seeded climatology when none has been ingested or when the operator picks a scenario |
| **gradient** | **always synthetic** — see below |

Everything is resolved **once per corridor and direction, at engine start**, into arrays at 500 m
resolution. The tick loop issues no database query. That is what lets several hundred vehicles run
on a laptop.

### 4.1 Gradient — always synthetic, and here is why

OSRM returns no elevation. Neither the public demo server nor a locally built Geofabrik extract
carries a height dimension, because OpenStreetMap itself does not. A real system samples a
**digital elevation model** — SRTM at 1 arc-second, or Copernicus EU-DEM at 25 m for Europe —
along the route geometry and differentiates it. That is the correct implementation and it is not
in this project because it would mean shipping a multi-gigabyte raster.

AutoTwin substitutes a seeded sum of three sinusoids per corridor:

| wavelength | amplitude |
|---|---|
| 40 km | 1.20 % |
| 12 km | 0.80 % |
| 3 km | 0.55 % |

Phases are drawn from `seeded_rng(seed, "terrain", base_slug)`, so a corridor has the same hills
in every run of the same seed. The sum peaks near ±2.5 %, which is where German motorway design
keeps ordinary grades; it does not find the 6 % of the A8 *Albaufstieg*. The mean is zero over a
corridor by construction, so a route neither gains nor loses net height — a property a real DEM
would **not** have, and one more reason this is labelled synthetic.

The **return direction mirrors and negates** the profile rather than re-seeding it, so a climb
southbound is the same descent northbound. That is the single most visible sanity check a
reviewer can run on simulated consumption by direction.

### 4.2 Synthetic climatology

Used when `weather_observations` has nothing near the corridor, or when the operator selects
`--weather-mode synthetic` (the default for training-data generation, because an observed
snapshot is frozen and gives a training set no temperature range at all).

```
T = 9.3 °C                                          German areal annual mean (DWD 1991-2020)
  − 8.8 · cos(2π (doy − 20) / 365.25)                seasonal, minimum in late January
  − 4.5 · cos(2π (hour_CET − 15) / 24)               diurnal, maximum at 15:00 local
  + (51.0 − latitude) · 0.55                         latitude gradient
  + 5.0 · sin(2π t / 84 h + phase)                   multi-day synoptic spells
```

Over a year at German latitudes that spans roughly **−11 °C to +30 °C**, which is the range of
*daily mean* extremes the DWD network records. It does not reach the −20 °C and +38 °C of record
days: those are the tails where the HVAC curve is clamped anyway, and inventing them would widen
the training set with conditions the model has no way to be right about.

**It is a climatology, not a forecast.** It reproduces the distribution German weather is drawn
from and says nothing about any particular day. CEST is not modelled — one hour of phase is far
below the accuracy of a 4.5 K diurnal amplitude.

*Precipitation*: two sine waves of incommensurable period (11 h and 29 h) beat against each
other; everything below a threshold is dry, and the threshold is calibrated so that about one
sample in ten is wet, which is the order DWD reports for the fraction of hours with measurable
precipitation. The unit is **mm in the preceding 10 minutes**, matching DWD's `RWS_10`, peaking
at 1.4 mm/10 min (8.4 mm/h). Below +1 °C it falls as snow.

*Wind*: 3.6 m/s mean (the DWD areal average) ± 2.2 m/s on a 17-hour oscillation, blowing from
240° ± 70° — Germany's prevailing west-south-westerly. The simulator knows both the wind
direction and the vehicle heading, so it resolves a genuine **headwind component**, which is why
a southbound and a northbound vehicle on the same corridor consume differently on a windy day.

### 4.3 Synthetic traffic

Where a corridor has no ingested Autobahn events, each 500 m bin gets a seeded congestion
propensity drawn from Beta(2, 3), and the severity at a moment is

```
pressure = propensity · (baseline + (1 − baseline) · rush_hour_weight(t))
```

`rush_hour_weight` is a raised cosine over the German weekday commuting peaks — 06:30–09:00 and
15:30–18:30 at full weight, 11:00–13:30 at 0.45 — and a flat 0.35 at weekends, when freight is
banned on Sundays and leisure traffic peaks in the afternoon instead. The `--traffic` scenarios
set the baseline and the reachable peak:

| scenario | baseline | peak severity |
|---|---|---|
| `none` | — | always `low` |
| `light` | 0.10 | `moderate` |
| `moderate` | 0.25 | `high` |
| `heavy` | 0.45 | `severe` |
| `observed` (fallback) | 0.20 | `high` |

A *baseline* rather than a floor on the severity, deliberately: a floor makes every window of a
`moderate` run at least `moderate`, which leaves the `traffic_severity_ordinal` feature nearly
constant. A baseline shifts the diurnal cycle upward while keeping 03:00 free-flowing.

Where a corridor **does** have ingested events, each event paints its severity onto ±2.5 km of
bins (the order of magnitude of a German roadworks zone; the Autobahn feed publishes a
representative point, and AutoTwin's ingested row keeps the point rather than the extent). A
`isBlocked` event is painted as `severe`.

### 4.4 Charging

Candidates are the ingested DC sites (`>= 50 kW`) within 5 km of the corridor, projected onto it
with `ST_LineLocatePoint` — the same corridor query the coverage report of BUILD_SPEC §7.2 runs,
so a simulated vehicle stops where the analysis says a charger is. Sites under 50 kW are not
candidates: a vehicle mid-corridor plugging into an 11 kW wallbox would sit there for four hours,
which is not a decision a driver makes.

The vehicle books one under either of two rules, checked once per 5 km of driving.

1. **Below the driver's reserve SOC**, take the **strongest** site within the distance the vehicle
   still believes it can cover (discounted by 15 %, so its own optimism cannot strand it).
   Strongest rather than nearest is the trade-off the route optimiser also makes: charging time
   dominates a stop's cost, so twenty extra kilometres to a 300 kW site beats a 50 kW one.
2. **Last chance before a gap.** Above the reserve the vehicle would rather keep driving — but
   only while another site stays reachable. If the best site in range is also the *last* site in
   range, driving past it means the next stop is the synthetic fallback charger at 3 % SOC, so it
   stops here. Without this rule a corridor whose chargers are unevenly spaced strands cars
   between them, which is exactly what the first version of this model did.

Charge is then recovered along `autotwin_ml.charging.charging_power_kw` — the same taper and the
same temperature derate the route optimiser plans with. The power actually delivered is
`min(site rating, vehicle acceptance)`, less the auxiliary load, which is carried by the charger
rather than the pack. `trips.energy_kwh` therefore never moves during a session: while plugged in
the vehicle is supplied by the grid, and counting the session as consumption would corrupt every
kWh/100 km figure derived from it. The vehicle leaves at its driver's departure target, clustered
around 80 % — where the taper knee sits and where German drivers actually unplug.

**The synthetic fallback charger.** If a corridor has no ingested DC site in range — the
Ladesäulenregister has not been ingested, or there genuinely is none — a vehicle that reaches 3 %
SOC charges **where it stands** at 50 kW, the most common German DC rating. The trip event
carries `station_id = None` and a `reason` that names the fallback. Nothing downstream may count
it as infrastructure: it corresponds to no real site. It exists so that `make demo` on a fresh
database shows a working fleet rather than a roadside full of bricked cars.

---

## 5. Time

Simulated time starts **one hour in the past** and advances `speed_factor` simulated seconds per
wall-clock second **until it reaches the present**, after which it advances in real time.

That is not a detail. A naive `speed_factor = 10` writes telemetry rows dated in the future within
minutes, which breaks every freshness check, every "latest per vehicle" query and the dbt test
that asserts no telemetry timestamp is in the future. Fast-forwarding a warm-up hour and then
tracking the present is both the useful demo behaviour — the live map has history the moment it
opens — and the only one that keeps the timestamps honest. At the default speed factor the clock
catches up after about seven wall-clock minutes.

Offline runs (`realtime=False`, used by `generate-training-data` and by the reproducibility
tests) skip the cap entirely and run as fast as the CPU allows.

The physics step is **1 simulated second regardless of the speed factor**: a tick covering 10
simulated seconds is integrated as ten 1-second steps. Without that, a run at 20× would integrate
a 20-second Euler step and every vehicle would overshoot the charging stop it planned.


### Lifecycle, and what a stopped run leaves behind

A run moves through the `SimulationState` cycle of BUILD_SPEC §2 — `pending → running ⇄ paused →
stopping → stopped | completed | failed` — driven either by the `/api/v1/simulations/{id}/…`
endpoints (through `autotwin_simulator.runner`, which the API holds as a process-wide singleton)
or by the CLI.

Stopping is **graceful by construction**. SIGINT and SIGTERM are installed on the asyncio loop,
not with `signal.signal`, so the request arrives between awaits and the current tick finishes
rather than unwinding through the middle of a database write. The engine then:

1. emits a `paused` trip event for every trip still in flight — `paused`, not `finished`, because
   the vehicle never reached its destination, so `trips.ended_at` stays null to say so;
2. flushes the sink, so the last batch reaches PostGIS or Redpanda;
3. writes the terminal state, `stopped_at` and the final counters onto the `simulation_runs` row.

A run therefore never gets stuck in `running`, and a fleet never stays `driving` after the
process that was driving it has exited. An unexpected exception inside the loop marks the run
`failed` with the exception on the row, and does **not** restart it: a simulator that silently
restarted itself would put an invisible discontinuity into a telemetry stream whose whole value
is that its history is explicable.

---

## 6. Randomness, and where every stream comes from

Nothing in the simulator calls a global random generator. Every stochastic element goes through

```python
seeded_rng(master_seed, "<concern>", <identity>...)  ->  random.Random
```

which joins its parts into a string and seeds `random.Random` with it. A `str` seed is hashed
with SHA-512 (`version=2`) and is therefore stable across interpreter runs and platforms, unlike
Python's `hash()`, which is randomised per process.

| stream | key | what it draws |
|---|---|---|
| terrain | `(seed, "terrain", base_slug)` | the three gradient phases |
| weather | `(seed, "weather", base_slug)` | synoptic, precipitation and wind phases |
| traffic | `(seed, "traffic", base_slug)` | the per-bin congestion propensity |
| driver profile | `(seed, "driver", vehicle_id)` | aggressiveness, reaction, comfort limits, reserve |
| driver behaviour | `(seed, "behaviour", vehicle_id)` | acceleration noise, standstill draws |
| fleet | `(seed, "fleet", vehicle_id)` | initial state of charge, uniform 35–95 % |
| vehicle | `(seed, "vehicle", vehicle_id)` | the idle dwell between trips, 2–15 min |
| route choice | `(seed, "route-choice", vehicle_id, trip_count)` | the next corridor and direction |

One generator per concern, not one shared stream. That is what makes a run reproducible **and**
composable: adding a vehicle, or changing the order vehicles are stepped in, cannot shift the
numbers any other vehicle sees.

The master seed is `AUTOTWIN_SIM_SEED` (default `20260214`) or `--seed`. With a pinned
`start_time` two runs of the same configuration produce byte-identical telemetry; the
verification below shows the check.

The vehicle mix is **not** random: a proportional mix is expanded into exactly `vehicle_count`
profiles by largest-remainder apportionment (the Hare-Niemeyer method), with ties broken on the
profile code, so the fleet depends only on the mix and the count.

---

## 7. The ML training set

`python -m autotwin_simulator.cli generate-training-data` writes
`data/gold/training/energy_windows.parquet`. One row is one **60-second driving window** of one
trip; the columns are the contract of BUILD_SPEC §10.2 and are declared once in
`autotwin_simulator.training_data.TRAINING_SCHEMA`.

Three filtering decisions shape it:

1. **Only fully-driving windows are kept.** A window that contains charging, idling or an
   operator stop is dropped: energy flowing *into* the pack from a charger has nothing to do with
   the consumption being predicted. Standstills **in traffic** are kept — the vehicle is still
   `driving` — because that is exactly the stop-and-go signal the model should learn.
2. **Windows under 50 m are dropped.** The target is a rate; dividing by a distance approaching
   zero produces a target approaching infinity, and a handful of such rows dominates any
   squared-error objective.
3. **The distributional features come from the physics step, not from the samples.** Speed,
   speed², |acceleration| and the acceleration-event count (|a| ≥ 1.0 m/s²) are accumulated at
   1 s resolution and summed per window. A standard deviation computed from six 10-second
   snapshots would blur exactly the structure that distinguishes a jam from a cruise.

Two column definitions are worth stating because they could otherwise be confused:

- `speed_kmh` — the arithmetic mean of the instantaneous speed over the window's physics steps.
- `avg_speed_kmh` — `distance / duration`, the journey average, which **includes standstills**.
  The two diverge precisely in stop-and-go, which is why both are features.
- `speed_limit_kmh` — an unrestricted Autobahn stretch (OSM reports no limit) is encoded as
  **130**, the *Richtgeschwindigkeit*, rather than 0 or a sentinel, so the feature stays monotone.
  The loss is real and is stated: the model cannot distinguish an unrestricted stretch from one
  posted at 130.

**Generation runs in seasonal batches.** One continuous run covers a few simulated days, over
which the climatology moves by less than two Kelvin — a model trained on that would see one
temperature and learn nothing about the winter penalty that is the whole point. The trip budget
is therefore split across six anchor dates spread through a fixed reference year (2025), which
produces a set spanning roughly −5 °C to +26 °C. For the same reason the command defaults to
`--weather-mode synthetic` and `--traffic moderate` rather than `observed`: a frozen DWD snapshot
and a handful of point traffic events give a training set almost no coverage of the two variables
it most needs. A live `run` keeps `observed`, because a demo should show the weather Germany is
actually having.

Every row carries `data_origin = "simulated"`.

---

## 8. Validity limits

What this simulator is **not** good for, stated plainly.

- **It cannot be used to estimate the real range of any car.** The profiles are class averages;
  the HVAC envelope has no humidity, no solar load, no pre-conditioning and no heat-pump/resistive
  distinction; the drivetrain efficiency is a constant. The largest single source of error is the
  HVAC curve at low temperature.
- **The terrain is invented.** Any conclusion that depends on where the hills are is a conclusion
  about the sinusoids in §4.1, not about Germany. Replace it with a DEM before drawing one.
- **The traffic model is a category, not a measurement.** `TrafficSeverity.delay_factor` is coarse
  by design: the sources publish a category, and inventing a finer scale would be false precision.
- **The weather is a climatology, not a forecast**, except where DWD rows were ingested.
- **Precipitation does not affect energy**, only the driver's chosen speed.
- **Validating the ML model against this data is circular.** The physical baseline generated it.
  This is stated wherever metrics are reported and is why the baseline is always shown alongside
  the learned model: the claim being demonstrated is a methodology, not a result.
- **Cyclic driving losses are in the data but not in the baseline.** A window whose speed starts
  and ends equal has zero net acceleration however violent the driving was in between, so the
  physical model cannot see stop-and-go losses that the simulator genuinely produced. The gap is a
  large part of what the learned model adds — and it is a property of this model pair, not a
  general result about ML versus physics.
- **Trip lengths are corridor lengths.** Vehicles drive demo corridors end to end and turn around;
  there is no origin-destination model, no urban duty cycle and no fleet dispatch logic.

---

## 9. Verification

Reproduce any of these with the commands shown. Every number below was measured on the code in
this repository, not estimated.

### 9.1 Consumption rises with speed

Controlled for vehicle class, weather and stop-and-go: one profile (`sedan_ev`), +20 °C, no
traffic, and only windows with `speed_std < 6 km/h` and `|gradient| < 0.6 %`. 40 vehicles, one
simulated hour, seed 20260214, on a corridor cut into motorway, primary, secondary, tertiary and
residential stretches. 2 352 windows, of which 814 steady and near-flat, over 1 193 km.

| speed band | windows | km | simulated kWh/100 km | physical model, steady state |
|---|---|---|---|---|
| 40–50 | 116 | 91.8 | 9.44 | 8.44 @ 40 km/h |
| 50–60 | 107 | 96.3 | 9.53 | 8.95 @ 50 |
| 60–70 | 131 | 140.1 | 10.25 | 9.67 @ 60 |
| 70–80 | 6 | 7.5 | 10.26 | 10.57 @ 70 |
| 80–90 | 31 | 44.5 | 11.87 | 11.65 @ 80 |
| 90–100 | 98 | 154.8 | 12.97 | 12.89 @ 90 |
| 100–110 | 80 | 140.2 | 15.96 | 14.29 @ 100 |
| 110–120 | 59 | 113.2 | 17.26 | 15.85 @ 110 |
| 120–130 | 94 | 194.4 | 19.01 | 17.57 @ 120 |
| 130–140 | 42 | 94.2 | 20.47 | 19.44 @ 130 |
| 140–150 | 43 | 104.4 | 22.48 | 21.47 @ 140 |

Monotonically rising, and consistently a little above the steady-state curve — which is what the
acceleration noise, the residual gradient and the recuperation cap should do.

**Without the controls the aggregate curve is U-shaped**, with its minimum near 40 km/h: below
that the auxiliary and HVAC load is spread over very little distance, and any window containing a
standstill has both a low average speed and a high consumption. That is the correct shape for
kWh/100 km against speed, and it is worth plotting both ways.

### 9.2 Consumption against temperature is U-shaped, worst in the cold

12 vehicles, one simulated hour, seed 20260214, `--traffic moderate`, the three fixed-temperature
scenarios:

| scenario | ambient | consumption | distance | vs +20 °C |
|---|---|---|---|---|
| `cold` | −10 °C | **31.63** kWh/100 km | 997 km | **+36.0 %** |
| `mild` | +20 °C | **23.26** kWh/100 km | 1 058 km | — |
| `hot` | +35 °C | **24.59** kWh/100 km | 1 056 km | **+5.7 %** |

`cold > hot > mild`, as the HVAC envelope (3.5 kW at −10 °C, 0 at +20 °C, 2.0 kW at +35 °C) plus
the cold-battery resistance factor require.

The same shape appears in the generated training set with no fixed scenario at all, from the
climatology alone (24 119 windows, 48 227 km):

| ambient band | windows | kWh/100 km |
|---|---|---|
| −5 … 0 °C | 2 686 | **27.04** |
| 0 … 5 °C | 4 802 | 25.44 |
| 5 … 10 °C | 4 627 | 23.97 |
| 10 … 15 °C | 4 571 | 23.94 |
| 15 … 20 °C | 3 430 | **22.31** |
| 20 … 25 °C | 3 182 | 22.49 |
| 25 … 30 °C | 821 | 22.62 |

Falling to a minimum in the high teens, then flattening and turning back up as the air
conditioning comes on.

### 9.3 State of charge is monotone

Six simulated hours, four vehicles, seed 20260214, four charging sites on the corridor
(150 / 300 / 50 / 150 kW at 45 / 90 / 135 / 180 km). All four vehicles charged. For the traced
vehicle: **0 samples where SOC rose while driving, 0 where it fell while charging.**

```
    time     state   soc%      km       kW  Tbatt
06:54:10   driving  25.97    84.2     -1.3   24.1     <- recuperating on a descent
07:03:10   driving  24.99    89.9      0.9   23.3
07:12:10  charging  57.30    90.0   -106.5   32.0     <- 300 kW site, car-limited to 120 kW
07:21:10  charging  80.59    90.0    -64.3   35.6     <- taper past 50 % SOC
07:30:10   driving  79.74    98.2     13.6   32.3
...
10:39:10  charging  25.32   180.0   -119.7   26.2
10:57:10  charging  81.03   180.0    -63.4   36.5
11:06:10   driving  77.73   195.0     19.3   33.3
11:15:10      idle  74.21   204.6      0.3   30.8     <- corridor complete, dwell
11:24:10   driving  71.87     7.5     19.6   28.7     <- next trip, other direction
```

The pack warms from 20 °C to the mid-thirties during a session and cools once the vehicle is
moving again; `instantaneous_power_kw` is negative while charging and occasionally negative while
driving, on descents.

### 9.4 The same seed produces the same telemetry

Two runs of the same configuration in one process, SHA-256 over every emitted `TelemetryEvent` in
order:

```
run 1: 4320 events sha256=3e5eacbdcd75632827c3c5c81079cd2c71152e257a6150b31fb8d6fbe8741807
run 2: 4320 events sha256=3e5eacbdcd75632827c3c5c81079cd2c71152e257a6150b31fb8d6fbe8741807
identical: True
```

And two separate invocations of the training-data generator produce a **byte-identical Parquet
file**:

```
$ python -m autotwin_simulator.cli generate-training-data --trips 60 --batches 3 \
      --vehicles 20 --seed 20260214 --out t1.parquet
10287 window(s) from 60 trip(s) across 20 vehicle(s), 12.4 simulated hour(s), 20119 km
$ ... --out t2.parquet
10287 window(s) from 60 trip(s) across 20 vehicle(s), 12.4 simulated hour(s), 20119 km
sha256(t1) = sha256(t2) = db86bf57d9b385012b3ef03bb058319e7ae75a8e8d77be469e577c3763b3a787
```

**The guarantee is "same seed *and* same inputs".** The corridors, their `route_segments`, and the
charging sites in range all come from the database, so re-running the ingestion pipelines — which
re-seeds `routes` with a freshly fetched OSRM geometry — legitimately changes the output. A run is
reproducible against a fixed data snapshot, not across a re-ingestion.

### 9.5 Throughput

`sim_tick_seconds = 1.0` gives each tick a 1 000 ms budget. Measured on an Apple-silicon laptop,
1 200 simulated seconds at speed factor 10 over five demo corridors (ten directions), telemetry to
a null sink so the measurement is of the simulator and not of PostgreSQL:

| fleet | ms per tick | events/s |
|---|---|---|
| 100 vehicles | 27 | 3 700 |
| 300 vehicles | 74 | 4 000 |
| 600 vehicles | 136 | 4 400 |

Linear in the fleet size; 600 vehicles use about 14 % of the tick budget.

### 9.6 End to end against PostGIS

```
$ python -m autotwin_simulator.cli run --vehicles 30 --speed-factor 60 --duration-s 1800 \
      --routes frankfurt-stuttgart,stuttgart-muenchen --sink db
simulator.environment.built  route=frankfurt-stuttgart  bins=407  charging_candidates=3
                             traffic_source=autobahn_events:1  weather_source=dwd_observations:2
simulated 0.50 h with 30 vehicle(s): 900 telemetry event(s), 30 trip event(s), 30 tick(s),
30.9 events/s, 0 error(s)
```

Afterwards: one `simulation_runs` row in `completed` with its counters, 30 `vehicles` rows, 30
`trips` rows with `distance_m` / `energy_kwh` / `avg_consumption_kwh_100km` maintained by the
sink, and 900 `telemetry` rows with real PostGIS points, `data_origin = 'simulated'` — and
**zero rows with `recorded_at > now()`**, which is the clock rule of §5 doing its job.

## 10. Running it

```bash
# a live fleet on the demo corridors, Kafka if it is up and PostGIS if it is not
python -m autotwin_simulator.cli run

# 300 vehicles at 20x on two corridors, straight to PostGIS, for ten simulated minutes
python -m autotwin_simulator.cli run --vehicles 300 --speed-factor 20 --duration-s 600 \
    --routes frankfurt-stuttgart,stuttgart-muenchen --sink db

# a January fleet, to see the winter penalty
python -m autotwin_simulator.cli run --weather-mode cold --traffic heavy

# the ML training set
python -m autotwin_simulator.cli generate-training-data --trips 240
```

Both commands accept `--seed`, `--log-level` and `--json-logs`, exit `0` / `1` / `2` per
BUILD_SPEC §15, and stop cleanly on SIGINT and SIGTERM — draining the current tick, flushing the
sink and closing the `simulation_runs` row rather than leaving a run stuck in `running`.

The demo corridors must exist first: `python -m autotwin_ingestion.cli seed routes`. The
simulator refuses to start against an empty `routes` table and says so, rather than inventing a
road.
