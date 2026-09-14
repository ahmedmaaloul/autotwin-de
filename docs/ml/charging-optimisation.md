# Corridor charging optimisation

> **Status.** Like the energy model it sits on, this is an **engineering approximation built for
> transparency, not a production trip planner**. The charging curve is a three-segment
> piecewise-linear stand-in for a proprietary OEM characteristic; the station data is a monthly
> CSV from the Bundesnetzagentur with no live availability; the detour geometry is a straight-line
> corridor projection. It produces defensible, reproducible, explainable plans. It must not be
> used to decide whether a real car reaches a real charger. §5 lists exactly what a real
> implementation would need that this one does not have.

Implemented in `services/ml/src/autotwin_ml/charging.py`, per BUILD_SPEC §10.3 and §7.4.
Pure Python — **no solver dependency**, as the specification requires.

---

## 1. The charging curve

### 1.1 State-of-charge taper

`P(soc) = P_max · f(soc)`, with `f` exactly as BUILD_SPEC §10.3 prescribes:

| SOC | 0-50 % | 50 → 80 % | 80 → 100 % |
|---|---|---|---|
| `f(soc)` | 1.00 | linear 1.00 → 0.55 | linear 0.55 → 0.18 |

**Real OEM charging curves are proprietary and none of them look like this.** A measured curve
has a ramp at the start while the pack is conditioned, plateaus and steps where the battery
management system switches current limits, and a taper whose shape depends on cell chemistry,
pack age and the preceding drive. What this stand-in *does* capture is the one fact that governs
trip planning: the last 20 % of the battery takes as long as the first 50, which is why a good
plan prefers two short stops to one long one.

### 1.2 Temperature derating

Multiplied on top, piecewise-linear and clamped at both ends:

| T_pack (°C) | -20 | -10 | 0 | 10 | 45 | 50 | 55 | 60 |
|---|---|---|---|---|---|---|---|---|
| derate | 0.15 | 0.25 | 0.50 | 1.00 | 1.00 | 0.70 | 0.40 | 0.25 |

Lithium-ion cells cannot accept high current when cold — plating risk — so a management system
cuts the limit hard below roughly 10 °C and lets it recover only as the pack warms. That is why
preconditioning on the way to a fast charger matters so much in a German winter, and why the cold
derate here is severe. Above ~45 °C the limit drops again to protect cell life and stay inside the
cooling system's capacity.

Same status as the taper: an order-of-magnitude envelope calibrated against nothing but published
class-level observations.

### 1.3 Session time

`charge_time_minutes` integrates numerically over SOC with a **midpoint rule**, default step
0.25 pp:

```
t = Σ_steps  E_step / min(station_power, P_vehicle(soc_mid, T))      E_step = C_usable · Δsoc / 100
```

A 10 → 80 % session is 280 evaluations — microseconds — and converges to well under a second of
error against a 0.01 pp step. The midpoint rather than the left edge matters: on the 80-100 %
ramp a left-edge rule over-estimates the power in every step and would shave minutes off the
slowest, most decision-relevant part of the stop.

### 1.4 What the curve actually produces

Measured 2026-09-14 for `sedan_ev` (74 kWh usable, 205 kW peak DC) on a 300 kW pillar:

| Session | Energy | Time | Avg power |
|---|---|---|---|
| 10 → 50 % | 29.60 kWh | 8.66 min | 205.0 kW |
| **10 → 80 %** | **51.80 kWh** | **17.30 min** | **179.7 kW** |
| 10 → 90 % | 59.20 kWh | 22.10 min | 160.8 kW |
| 10 → 100 % | 66.60 kWh | 30.37 min | 131.6 kW |
| 80 → 100 % | 14.80 kWh | 13.08 min | 67.9 kW |

Sanity bounds on the headline case, all satisfied:

- naive constant-power at the pillar rating, `51.80 / 300 kW` = **10.36 min** — the plan must be
  slower than this, and is (17.30);
- constant-power at the *vehicle* limit, `51.80 / 205 kW` = **15.16 min** — still optimistic,
  because it ignores the taper above 50 %; the plan is slower than this too;
- constant-power at the *end-of-session* power `P(80 %) = 112.75 kW`, `51.80 / 112.75` =
  **27.57 min** — the pessimistic bound, and the plan is faster.

The last 20 % of the pack costs 13.08 minutes for 14.80 kWh while the first 40 % costs 8.66
minutes for 29.60 kWh — 0.884 min/kWh against 0.293 min/kWh, a factor of 3.0. That difference is
the entire reason the optimiser exists.

Temperature, same 10 → 80 % session on the same pillar:

| T_pack | -10 °C | 0 °C | 10 °C | 20 °C | 50 °C |
|---|---|---|---|---|---|
| Time | 69.18 min | 34.59 min | 17.30 min | 17.30 min | 24.71 min |

---

## 2. The objective

Verbatim from BUILD_SPEC §10.3, and surfaced on every plan as `ChargingPlan.objective`:

```
objective = driving_time_min + charging_time_min + 2 · detour_time_min + range_risk_penalty_min
```

**Detour time is weighted twice on purpose, and that is a preference, not a clock.** Leaving the
corridor costs more than the minutes it takes: an unfamiliar exit, a car park to find, a pillar
that may be occupied, and the risk of not being able to rejoin quickly. `ChargingPlan.total_time_min`
reports honest wall-clock time and counts each detour **once**; only the objective double-counts
it. Keeping the two apart is what lets a plan be reported truthfully and still be chosen for its
comfort.

### 2.1 Range-risk penalty

```
comfort_soc = min_soc + risk_buffer                       (default 10 % + 5 pp = 15 %)
penalty     = Σ_legs max(0, comfort_soc - soc_at_leg_end) · 2.0 min/pp
```

Charged at every stop arrival and at the destination. Calibrated so that a plan arriving right at
the hard minimum costs 10 objective-minutes more than one arriving with a 5 pp reserve: enough to
break a tie in favour of the safer plan, not enough to buy a whole extra stop for it.

The hard floor `min_soc` (default 10 %) is a constraint, not a penalty — a plan that would go
below it is never generated. 10 % of a usable pack is 30-50 km, enough to reach an alternative if
a pillar turns out to be occupied or broken, which German HPC sites regularly are.

---

## 3. The search

Beam search, **beam width 8, at most 3 stops**, exactly as BUILD_SPEC §10.3 specifies.

```
state = (offset_km, soc, charge_time, detour_km, detour_time, risk_penalty, stops, visited)

expand(state) = { (candidate, target_soc) : candidate.offset > state.offset + 5 km,
                                            candidate not already visited,
                                            soc on arrival >= min_soc,
                                            target_soc >= arrival + 2 pp }

at every depth, including depth 0:  try to run straight to the destination and,
                                    if that is feasible, record the complete plan
keep per depth:                     the best `beam_width` partial plans
answer:                             the best complete plan by objective
```

### 3.1 Departure-SOC grid

`{60, 70, 80, 90} %` **plus one adaptive target**: exactly the SOC needed to reach the
destination with the requested arrival reserve. Without the adaptive target the grid would force
a driver to 60 % when 43 % would have finished the trip, and the objective would reward a
needlessly long stop. Nothing above 90 % is offered, because the 90-100 % band is the slowest
energy on the curve and is almost never worth the clock on a corridor.

A coarse grid rather than a continuous variable, because the taper makes the objective nearly
flat between neighbouring targets while a continuous search would multiply the state space for no
measurable gain.

### 3.2 Ranking partial plans

Partial plans are not comparable on committed cost alone — a plan that simply postponed its
charging looks cheapest right up to the moment it strands. The beam therefore ranks on an
A*-flavoured score:

```
score = (charge_time + 2·detour_time + risk_penalty)        committed cost, g
      + remaining_energy_deficit / (0.6 · P_max_dc) · 60    optimistic remainder, h
```

`h` assumes the remaining deficit is charged at 60 % of the vehicle's peak DC power, which no
real session beats over a meaningful SOC span, so it rarely over-estimates and the beam is not
misled into dropping the eventual winner.

### 3.3 Determinism

A hard requirement, not a nicety: the API caches plans, the frontend snapshots them, and a plan
that changes between two identical requests is a bug report.

- candidates are sorted **once** by `(round(offset_km, 6), station_id)`;
- every beam ranking and every best-plan comparison uses the total key
  `(round(score, 6), stop_count, tuple_of_station_ids)`;
- no `set` or `dict` whose iteration order could depend on float comparisons is ever walked;
- SOC targets are de-duplicated by `round(value, 6)` and then sorted.

Verified: 25 runs of the same 600 km problem with a randomly shuffled candidate list produce
one distinct result — identical stops, identical objective value to 9 decimal places, identical
arrival SOC.

### 3.4 Energy and SOC arithmetic

Segment energies come from the route analysis (`RouteEnergySegment`: offset, distance, duration,
energy). The optimiser never recomputes physics — it integrates what it was given, prorating
linearly inside a segment that is only partly covered by a leg. That is the same homogeneity
assumption the physical model already makes inside a segment, so the proration adds no error the
model had not already accepted. It also means the same beam search runs unchanged on
physical-baseline energies, on ML predictions, or on a blend.

**Detour energy** is costed at the vehicle's nominal consumption, not the physical model: the
detour is off the analysed corridor, so there is no geometry, no weather and no gradient for it.
The error is bounded by the detour being short — 3 km at 16.5 kWh/100 km is half a kilowatt-hour,
well inside the model's own uncertainty.

`detour_km` is defined as the **extra distance driven, off the route and back** — not one-way.

---

## 4. Infeasibility

`optimise_charging` never raises for a hard charging problem; "this car cannot do this route
today" is a legitimate answer the UI has to render, not a server error. It returns
`feasible=False` with a bilingual reason that always names a number the user can act on.
Three distinguishable causes, in the order a driver would ask about them. Real output:

**No stations in the corridor**

> Kein Ladeplan möglich: im Korridor liegt keine passende Ladesäule. Mit 80 % SOC sind rund
> 230 km der 600 km erreichbar (Bedarf 127,1 kWh).
>
> No charging plan possible: no suitable station in the corridor. At 80 % SOC about 230 km of
> 600 km are reachable (demand 127.1 kWh).

**First station out of range**

> Kein Ladeplan möglich: die erste erreichbare Ladesäule liegt bei km 390, mit 40 % SOC reicht
> die Reichweite aber nur bis km 105 (Mindest-SOC 10 %).
>
> No charging plan possible: the first station sits at km 390, but at 40 % SOC the range ends at
> km 105 (minimum SOC 10 %).

**Gaps too large for the stop budget**

> Kein Ladeplan möglich: die Strecke ist mit höchstens 3 Ladestopps nicht zu schaffen — die
> Lücken zwischen den 2 Ladesäulen im Korridor sind für Transporter EV zu groß (Bedarf
> 224,7 kWh auf 600 km).
>
> No charging plan possible: the route cannot be completed with at most 3 stops — the gaps
> between the 2 corridor stations are too large for the Transporter EV (demand 224.7 kWh over
> 600 km).

The reachable-distance figure is walked segment by segment along the actual route, prorating
inside the segment where the energy runs out — it is a real distance on this route, not a
nominal-consumption estimate.

---

## 5. Per-stop rationale

Every stop carries `rationale_de` and `rationale_en`, **generated from that stop's own computed
numbers**. There is no phrase bank and no template with an adjective in it: a canned string
("a good place to charge") would be indistinguishable from a plan that had not been computed at
all. The sentence names the power that was actually available at the arrival SOC, whether the
vehicle or the pillar was the binding limit, the energy actually added, the time it takes and the
detour actually driven.

Real output from the 600 km reference problem (`sedan_ev`, start 80 %, 4 °C):

> Autohof Ulm bei km 300: Ankunft mit 16 % SOC, Laden auf 70 % — 40,3 kWh in 13 min bei 205 kW
> (fahrzeugseitig begrenzt, Ø 192 kW), 0,8 km Umweg.
>
> Autohof Ulm at km 300: arrive at 16 % SOC, charge to 70 % — 40.3 kWh in 13 min at 205 kW
> (vehicle-limited, avg 192 kW), 0.8 km detour.

German numbers use a comma decimal separator, English a point.

### 5.1 A worked plan

600 km motorway corridor, `sedan_ev`, 4 °C, start 80 % SOC, 12 candidate sites between 50 kW and
400 kW, demand 127.1 kWh against a 74 kWh usable pack:

| | |
|---|---|
| Stops | 3 — km 120 → 70 %, km 300 → 70 %, km 480 → 60 % |
| Driving | 300.0 min |
| Charging | 27.3 min |
| Detour | 2.3 km / 2.3 min |
| **Total (wall clock)** | **329.6 min** |
| Objective value | 331.95 |
| Arrival SOC | 25.7 % |
| Minimum SOC reached | 15.5 % |
| Partial plans evaluated | 319 |

Note what the optimiser chose: three medium stops at 70/70/60 % rather than two long ones to
90 %, and it skipped the 400 kW site at km 390 in favour of the 300 kW site at km 480 because the
vehicle's own 205 kW limit makes the extra pillar power worthless and the 480 km position needs
less charging overall. Note also that all three stops are **vehicle-limited**: at 205 kW peak DC,
`sedan_ev` cannot use a 300 kW pillar's headroom, and the plan says so in its own rationale.

---

## 6. What a real implementation would need that this one does not have

Roughly in order of how badly each one is missed:

1. **Live station availability and reliability.** The Bundesnetzagentur register is a monthly
   CSV: it says a pillar exists, never whether it works or whether someone is plugged into it.
   A real planner consumes a roaming/eMSP availability feed, keeps a per-operator reliability
   prior, and plans a fallback for every stop. This is the single largest gap between this
   optimiser and a usable one.
2. **A real charging curve per vehicle.** Measured, SOC- **and** temperature-resolved, with the
   BMS's step changes in it — and a *thermal model* that knows whether the pack will actually be
   at that temperature when the car arrives, because preconditioning en route is worth more
   winter minutes than any routing decision.
3. **Real detour geometry.** Here `detour_km` is whatever the corridor query measured. A real
   planner routes to the site and back through the actual network, which changes both the
   distance and the time (an exit and re-entry on a motorway is rarely symmetric), and it knows
   whether the site is on the correct carriageway — a 300 kW pillar on the other side of an
   Autobahn is not a candidate at all.
4. **Price.** The objective is pure time. Real drivers trade minutes against euros, and German
   ad-hoc tariffs vary by a factor of two between operators. A real objective is
   `α · time + β · cost` with a user-set or learned `α/β`.
5. **Queueing.** Arrival time at a popular site on a Friday afternoon is not the same as at
   03:00. A real planner carries an occupancy forecast and adds an expected wait to each stop.
6. **Amenities and stop quality.** A 25-minute stop at a service area with food is worth more
   than a 20-minute stop in an industrial car park. The current objective cannot express that.
7. **Optimality guarantees.** A beam of 8 is a heuristic. For problems this size a branch-and-
   bound or a small MILP over the (station × target SOC) grid would be provably optimal and still
   fast; the beam is specified because it is transparent and dependency-free, and it does find
   the optimum on every hand-checked case, but it is not *guaranteed* to.
8. **Multi-vehicle / fleet coordination.** Two vehicles of the same fleet planned independently
   will happily both stop at the same two-pillar site.
9. **Uncertainty.** Every number in the plan is a point estimate. A real planner propagates the
   energy model's error into an arrival-SOC distribution and plans against a quantile, not a mean.

---

## 7. API surface

```python
from autotwin_ml import ChargingCandidate, charge_time_minutes, charging_power_kw, optimise_charging

power_kw = charging_power_kw(profile, soc_percent=35.0, battery_temp_c=5.0)
minutes  = charge_time_minutes(profile, 10.0, 80.0, station_power_kw=300.0, battery_temp_c=20.0)

plan = optimise_charging(
    route_segments_energy,   # Sequence[RouteEnergySegment] from the route analysis
    vehicle,                 # VehicleProfile
    start_soc=80.0,
    min_soc=10.0,
    candidates=corridor_candidates,
    battery_temp_c=4.0,
)
```

`ChargingPlan` maps field for field onto BUILD_SPEC §7.4's payload; the API resolves each stop's
`ChargingCandidate` into a full `ChargingStationSummary` for the response. `objective_value` is
`None` on an infeasible plan rather than infinity, because `Infinity` is not valid JSON — a detail
that only ever surfaces in production.
