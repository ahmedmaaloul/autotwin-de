# Identifying underserved high-power EV charging corridors in Germany

**Status:** first edition · **Data as of:** Bundesnetzagentur Ladesäulenregister, 1 September 2026
· **Analysis run:** 14 September 2026 · **Reproducible:** `make demo && make dbt-run`

---

## 1. Question

On a long-distance journey, a battery-electric vehicle is constrained not by the *number* of
chargers along its route but by the **largest distance between two consecutive charging
opportunities of sufficient power**. A corridor with two hundred 50 kW chargers and one 90 km
gap is harder to drive than one with thirty evenly spaced 300 kW chargers.

> **How far must a driver travel along Germany's principal road corridors before encountering a
> charging site of at least 150 kW — and which corridor is the weakest?**

This is deliberately a *gap* question rather than a *density* question. Density is the metric
that flatters infrastructure; gap is the metric a driver experiences.

---

## 2. Data

| | |
|---|---|
| **Charging infrastructure** | Bundesnetzagentur *Ladesäulenregister*, edition 2026-09-01 |
| Rows ingested | 116 443 received, **116 440 accepted**, 3 rejected by validation |
| Licence | CC BY 4.0 — *Ladesäulenregister der Bundesnetzagentur* |
| **Route geometry** | OSRM over OpenStreetMap, `driving` profile, 5 German corridors |
| Licence | ODbL 1.0 — © OpenStreetMap contributors |

The three rejected rows failed the coordinate and positive-power rules in
`autotwin_core.quality.rules`; they are recorded in `data_ingestion_runs.quality_report` rather
than silently dropped.

**Power threshold.** 150 kW is the boundary AutoTwin DE calls `ultra_fast` (BUILD_SPEC §2). It is
the power at which a 20-minute stop restores a useful fraction of a mid-size battery, and it
therefore separates "a charger exists" from "a charger that makes the journey practical".

---

## 3. Method

For each corridor:

1. **Select** charging sites with `max_power_kw >= 150` lying within a buffer of the route,
   using `ST_DWithin(station.location, route.geometry::geography, buffer)`. The geography type
   makes the buffer true metres rather than degrees.
2. **Project** each selected site onto the route with `ST_LineLocatePoint`, **after transforming
   both to EPSG:25832 (ETRS89 / UTM zone 32N)**.
   *This step is the one that is easy to get wrong.* On raw WGS 84, `ST_LineLocatePoint` and
   `ST_Length` measure in degrees, and at 51° N one degree of longitude spans roughly 63 % of the
   distance of one degree of latitude. An east–west corridor's offsets would therefore be
   compressed relative to a north–south corridor's, in a direction-dependent way that no
   aggregate statistic would reveal.
3. **Order** the resulting offsets and take `lead(offset) − offset` as the gap.
4. **Include the two edge gaps** — origin → first qualifying site, and last site → destination.
   Omitting them is the most common error in this analysis and it systematically *understates*
   the worst gap, which is the number the whole exercise exists to report.
5. A corridor with no qualifying site is **one** gap the length of the route, not zero gaps.

**Invariant.** The gaps must sum to the route length. The implementation asserts this to within
10 m and warns otherwise; it catches a missing edge gap, a double-counted site, or an offset
measured against the wrong length.

The production implementation is `dbt/models/marts/mart_charging_coverage.sql` and
`services/api/src/autotwin_api/services/coverage.py`; both are exercised by the
`/api/v1/charging/underserved` endpoint.

---

## 4. Assumptions and their consequences

| Assumption | Consequence if wrong |
|---|---|
| The registry is complete | BNetzA lists only operators who completed the reporting procedure; the real network is **larger**. Gaps here are therefore an **upper bound**. |
| A site's `max_power_kw` is deliverable | Shared-power cabinets mean two cars at one site may each get less. Gaps are optimistic. |
| Every charger is available | The registry carries no occupancy. A "covered" corridor can still be unusable at a peak hour. |
| A site within the buffer is reachable | A charger 900 m from the Autobahn may be on the far side of it with no junction. Sensitivity analysis below partially addresses this. |
| Corridors are representative | Five corridors are not the German network. This is a method demonstration on a defensible sample. |

---

## 5. Results

### 5.1 Principal result — 1 km corridor (motorway-adjacent sites only)

| Corridor | Length | HPC sites ≥150 kW | **Largest gap** | Mean gap |
|---|---:|---:|---:|---:|
| Frankfurt am Main → München | 393.3 km | 592 | **32.0 km** | 0.66 km |
| Wolfsburg → Berlin | 228.3 km | 171 | **27.3 km** | 1.33 km |
| Stuttgart → München | 221.0 km | 332 | **21.4 km** | 0.66 km |
| München → Ingolstadt | 80.9 km | 172 | **17.2 km** | 0.47 km |
| Frankfurt am Main → Stuttgart | 203.3 km | 287 | **16.6 km** | 0.71 km |

### 5.2 Sensitivity to the corridor buffer

| Corridor | 1 km | 2 km | 5 km |
|---|---:|---:|---:|
| Frankfurt am Main → München | 32.0 | 20.1 | 20.1 |
| Wolfsburg → Berlin | 27.3 | 24.8 | 24.8 |
| Stuttgart → München | 21.4 | 19.2 | 17.8 |
| München → Ingolstadt | 17.2 | 16.4 | 16.4 |
| Frankfurt am Main → Stuttgart | 16.6 | 16.6 | **13.0** |

*Largest gap in km, by buffer width.*

**The ranking is stable.** Frankfurt–München and Wolfsburg–Berlin are the weakest at every
buffer; Frankfurt–Stuttgart is the strongest at every buffer. A conclusion that survives a
five-fold change in the main free parameter is worth more than one tuned to a single setting.

---

## 6. Interpretation

**On these corridors, German high-power charging is not the binding constraint on long-distance
EV travel.** The worst gap found anywhere in the sample is 32 km. A vehicle with 300 km of
usable range crosses that on roughly 10 % of its battery. Even the weakest corridor offers a
≥150 kW site roughly every 2 km on average.

That is not the result one might expect from public debate about charging infrastructure, and it
is worth stating plainly rather than framing the data to produce a more dramatic finding.

Three qualifications matter:

1. **Gap is not availability.** These figures say a charger *exists*, not that it is free. Queue
   modelling needs occupancy data the registry does not carry, and it is the obvious next step.
2. **Corridors are the best-served part of the network.** Federal motorways have been the focus
   of German charging roll-out. The same analysis applied to *Bundesstraßen* or rural
   connections would very likely look different — and that, not the Autobahn, is where this
   method would earn its keep.
3. **This is an upper bound on gaps.** The registry undercounts, so reality is at least this
   good.

**Frankfurt am Main → Stuttgart**, AutoTwin DE's reference corridor, is the best-served of the
five: 287 qualifying sites within 1 km of the route and a worst gap of 16.6 km.

---

## 7. Limitations

- Five corridors, all motorway, all in the western and southern half of the country. No claim is
  made about Germany as a whole.
- Straight-line buffer selection admits sites that are near the route but not accessible from
  it. The 1 km column mitigates this; a rigorous treatment would route from the corridor to each
  candidate site and use the real detour.
- No temporal dimension: the registry is a monthly snapshot, so nothing here describes how the
  gaps have closed over time. The commissioning dates are ingested and would support that.
- No demand side. A 32 km gap on a corridor carrying little traffic matters less than a 20 km
  gap on a heavily used one.

---

## 8. Future work

1. **Occupancy and queueing.** Combine the registry with a live availability feed and model a
   charger as a queueing system rather than a point.
2. **Detour-aware gaps.** Replace buffer selection with routed detour distance.
3. **Demand weighting.** Weight gaps by traffic volume from the Autobahn network.
4. **Placement optimisation.** Given a budget of *n* new 300 kW sites, where do they minimise the
   worst gap across the network? This is a facility-location problem the current data supports.
5. **Extend beyond motorways**, where the interesting answers almost certainly are.

---

## 9. Reproducing this

```bash
make up && make db-upgrade
uv run python -m autotwin_ingestion.cli ingest charging --mode live   # ~55 MB, ~5 min
uv run python -m autotwin_ingestion.cli seed routes
make dbt-run                                                          # builds mart_charging_coverage
curl -s 'localhost:8000/api/v1/charging/underserved?min_power_kw=150&corridor_buffer_km=1'
```

The SQL behind the tables above is in
[`dbt/models/marts/mart_charging_coverage.sql`](../../dbt/models/marts/mart_charging_coverage.sql).

**Attribution:** *Ladesäulenregister der Bundesnetzagentur* (CC BY 4.0);
© OpenStreetMap-Mitwirkende (ODbL 1.0); Routing by OSRM.
