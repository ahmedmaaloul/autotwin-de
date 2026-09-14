# AutoTwin DE — analytical layer (dbt)

The warehouse side of AutoTwin DE. It reads the operational PostgreSQL database
(`BUILD_SPEC.md` §3) and builds the aggregates the API and the dashboard serve, per
`BUILD_SPEC.md` §16.

**dbt never writes to `public`.** The operational schema is the system of record for the API,
the simulator and the streaming consumer; this project reads from it and materialises into its
own schemas. An analytical rebuild cannot corrupt a transactional table, by construction.

---

## Honesty: which numbers are simulated

This is the first thing to know about this project and the reason for
`BUILD_SPEC.md` §0.2. Two kinds of data live in the same warehouse, and they are never mixed
without saying so.

| Built on | Models | What it is |
|---|---|---|
| **Official sources** | `stg_charging_stations`, `stg_charging_points`, `stg_weather_observations`, `stg_traffic_events`, `stg_routes`, `dim_charging_station`, `dim_route`, `fct_traffic_events`, `mart_charging_infrastructure`, `mart_charging_coverage`, `mart_traffic_impact` | Bundesnetzagentur, DWD, Autobahn GmbH, OSM/OSRM. Real measurements of the real German network. |
| **Simulated** | `stg_telemetry`, `stg_trips`, `int_trip_energy`, `fct_vehicle_telemetry`, `fct_trips` | Produced by `autotwin_simulator`. **No row was measured on a vehicle.** Every consumption figure, SOC curve and trip aggregate in these models is model output. |
| **Derived** | `stg_route_segments`, `dim_vehicle_model`, `mart_route_energy` | Computed by AutoTwin from official inputs — a route cut into chunks, the energy model of §10 run over it, curated public vehicle-class figures. No external source to attribute them to. |

Every model carries `data_origin` on every row, and it is tested: `accepted_values` on
`fct_vehicle_telemetry.data_origin` admits **only** `simulated`. If you are looking at a chart
built on `fct_trips` or `fct_vehicle_telemetry`, you are looking at a simulation — the UI labels
it `SIMULIERT` for the same reason.

`mart_route_energy` is the subtle one. Its inputs — road geometry, weather, traffic — are
official; its predictions are model output. It is marked `derived`, not `official`.

---

## Layers

```
sources    public.*                          the operational tables (BUILD_SPEC §3.2)
  ↓
staging    view       analytics_staging.*     rename · cast · light clean · nothing else
  ↓
intermediate ephemeral (no relation)          named joins, inlined as CTEs
  ↓
marts      table      analytics_marts.*       what the API and the dashboard read
```

**Staging** is a contract, not a copy. One model per source table, `stg_` prefixed, doing
renaming, type casting and the cleaning that is a property of the *source* (DWD's `-999`
sentinel, the Autobahn feed's stray whitespace, a German postcode that lost its leading zero).
No joins, no business logic. Views, because materialising them would double the storage of
`telemetry` to add nothing but column names.

Staging also unpacks the PostGIS `geography` columns into plain `latitude` / `longitude`
doubles with `ST_Y(location::geometry)` / `ST_X(location::geometry)`, so that no downstream
model has to know PostGIS. The geography column is passed through as well, for the three models
that legitimately do spatial work.

**Intermediate** models are ephemeral: they exist to name a join, not to be queried. They are
inlined as CTEs into the marts, which keeps the mart SQL readable without leaving intermediate
relations lying around for someone to query by accident.

**Marts** are tables, read on every page load. `fct_vehicle_telemetry` is the one incremental
model, because it is the only one whose input grows without bound.

### Model reference

| Model | Grain | Answers |
|---|---|---|
| `dim_charging_station` | site | Where is it, who runs it, how strong is it *really* |
| `dim_route` | route | Corridor attributes plus the shape of its segmentation |
| `dim_vehicle_model` | EV profile | The five generic classes of §9 and their derived constants |
| `fct_vehicle_telemetry` | telemetry tick | The simulated fleet, per tick, with the vehicle resolved |
| `fct_trips` | trip | Journey aggregates, with both accounts of their energy |
| `fct_traffic_events` | traffic event | Disruptions with lifecycle and severity resolved |
| `mart_charging_infrastructure` | Bundesland | How each state's network compares, normalised by area |
| `mart_charging_coverage` | demo route | Can an EV actually drive this corridor — the gap analysis |
| `mart_route_energy` | route segment | Predicted energy, with weather and traffic attribution |
| `mart_traffic_impact` | road × type × severity | Disruption volume and which corridors it lands on |

---

## Running it

```bash
# From the repository root. The profile is committed at dbt/profiles.yml.
# There is no `dbt deps` step: this project has no package dependencies at all.
dbt seed   --project-dir dbt --profiles-dir dbt
dbt run    --project-dir dbt --profiles-dir dbt
dbt test   --project-dir dbt --profiles-dir dbt

# Or all of it, in dependency order, tests interleaved with the models they guard:
dbt build  --project-dir dbt --profiles-dir dbt

# Validate the DAG and all Jinja without touching a database:
dbt parse  --project-dir dbt --profiles-dir dbt --no-partial-parse

# Are the sources still being ingested?
dbt source freshness --project-dir dbt --profiles-dir dbt
```

Requires **dbt-core >= 1.8, < 2.0** with the `dbt-postgres` adapter, and a database with
**PostGIS** — `mart_charging_coverage` and `int_corridor_stations` genuinely need it.

### Connection

`profiles.yml` is committed and reads everything from the environment, with the local
development values of `BUILD_SPEC.md` §5 as defaults:

| Variable | Default | |
|---|---|---|
| `AUTOTWIN_DB_HOST` | `localhost` | |
| `AUTOTWIN_DB_PORT` | `5433` (`dev`) / `5432` (`ci`) | the compose stack publishes 5433 |
| `AUTOTWIN_DB_USER` | `autotwin` | |
| `AUTOTWIN_DB_PASSWORD` | `autotwin` | the local compose credential, nothing else |
| `AUTOTWIN_DB_NAME` | `autotwin` | |
| `AUTOTWIN_DBT_SCHEMA` | `analytics` | |
| `AUTOTWIN_DBT_THREADS` | `4` | |

The only literal credential in the file is the one `docker-compose.yml` creates for local
development, which is already in plain text in the compose file and in `.env.example`. There is
no secret here. A deployed target supplies all five variables from its own environment.

Two targets: `dev` (the compose stack) and `ci` (Postgres as a GitHub Actions service container,
default port, throwaway database).

### Schemas it creates

| Schema | Contents |
|---|---|
| `analytics_staging` | the eight `stg_` views |
| `analytics_marts` | the ten mart tables |
| `analytics_reference` | the `bundesland_reference` seed |
| `analytics_test_failures` | the offending rows from every failed test |

---

## Tests

Run `dbt build` and 447 tests run with the models — 439 generic, 8 singular. They break down as:

- `unique` + `not_null` on every primary and natural key;
- `accepted_values` on **every** enum column, with the values taken verbatim from
  `BUILD_SPEC.md` §2 — so a new enum member that reaches the database without reaching this
  project fails the build rather than appearing as an unlabelled slice in a chart;
- `relationships` on every foreign key;
- `accepted_range` on the numeric invariants (SOC 0-100, coordinates in the German bounding box,
  power > 0, percentages 0-100);
- `expression_is_true` for cross-column invariants (`ended_at >= started_at`, a fast charger's
  category, the gap decomposition summing to the route length);
- eight singular tests in `tests/` for the domain invariants that span models.

`store_failures` is on for everything. Failing rows land in
`analytics_test_failures.<test_name>` instead of being counted and thrown away — a data-quality
project that cannot show you the offending row is not a data-quality project. The cost is honest
and worth knowing: dbt creates one (usually empty) relation per test, so `analytics_test_failures`
holds ~450 tables after a full build. They are cheap, and having the offending row already
materialised when someone asks "which station?" is worth more than a tidy schema listing.

### The singular tests

| Test | Invariant |
|---|---|
| `assert_soc_within_bounds` | A state of charge is a percentage: 0-100, on every tick and both ends of every trip |
| `assert_coordinates_within_germany` | Every point is inside the German bounding box of §6 — a point outside it is a parsing error, not a foreign location |
| `assert_no_future_telemetry` | Nothing in the simulated fleet is recorded in the future |
| `assert_positive_charging_power` | A stated charging power is strictly positive (NULL stays a legitimate "unknown") |
| `assert_no_orphan_route_segments` | Every segment has a parent route *and* lies inside it |
| `assert_trip_energy_reconciles` | The consumer's running trip totals agree with the telemetry — **warn**, see below |
| `assert_bundesland_spine_complete` | The regional mart has exactly sixteen states, no more and none missing |
| `assert_connector_provenance_matches_station` | A connector's attribution is its station's |

Each file opens with the invariant it protects *and the specific ways it could break* — the
decimal comma in the Ladesäulenregister, the Autobahn feed's `long` key, the simulator's speed
factor. They are written to catch known failure modes of these four specific sources, not to
decorate the project with green ticks.

`assert_trip_energy_reconciles` is the one test at `warn`. The Kafka consumer is at-least-once
and updates the trip row in a separate transaction from the telemetry insert, so a trip that is
running *while dbt builds* is expected to disagree. Failing the build on that would make
`dbt build` non-deterministic against a live simulator, and a test that cries wolf during normal
operation is one people learn to ignore. It checks finished trips only, and the divergence is
published as a column on `fct_trips` either way.

### Freshness

`dbt source freshness` is configured on the four ingested tables, against their natural
timestamps:

| Source | Field | warn | error |
|---|---|---|---|
| `charging_stations` | `ingested_at` | 7 days | 35 days | 
| `weather_observations` | `observed_at` | 2 h | 12 h |
| `traffic_events` | `ingested_at` | 6 h | 24 h |
| `telemetry` | `recorded_at` | 15 min | 60 min |

The telemetry thresholds describe a **running simulator**. When no simulation is active that
source is legitimately stale and freshness is *expected* to warn — that is the correct reading,
not a false alarm. Likewise, on a database where no ingestion has run yet all four report
`ERROR STALE`, because `max(loaded_at_field)` over an empty table is NULL: "never loaded" is
infinitely stale, which is the honest answer.

`dbt seed` must run before `dbt run` on a fresh database — `dim_charging_station` and
`mart_charging_infrastructure` both `ref('bundesland_reference')`, and without it the run fails
with `relation "analytics_reference.bundesland_reference" does not exist`. `dbt build` handles
the ordering on its own and is the safer entry point.

---

## Design notes

Things a reader will reasonably challenge, answered once here and again at the top of each model.

**Why there is no `packages.yml`.** The only package worth pulling would be `dbt_utils`, for
three generic tests: `accepted_range`, `expression_is_true` and
`unique_combination_of_columns`. They are implemented directly in `macros/generic_tests/`,
under the names their `dbt_utils` equivalents use and with the same argument shapes, so the YAML
reads the way anyone who knows the package expects. That buys a project with **no package
dependency at all**: `dbt parse` and `dbt build` work on a machine that cannot reach the dbt hub,
CI has no `dbt deps` step to break, and there is no transitive version constraint to reconcile
against `require-dbt-version`. Three small, documented macros are a better trade than a
dependency. If a fourth were needed, that judgement would flip and `packages.yml` would appear.

**Two distances, always.** `distance_km` is what the routing engine drove; `geometry_length_km`
is the length of the returned line measured in the metric grid, and comes out a few per mille
shorter because a polyline is a chord approximation of a road. Gaps and offsets are measured
along the *geometry*, because that is the line stations were projected onto; densities per 100 km
use the *driving* distance, because that is the number the UI shows. Both are published on every
model that reports either. Mixing them is how a gap analysis stops adding up to its own route —
and `mart_charging_coverage` asserts that its gaps sum to the route length to ten metres.

**Why the metric CRS.** `ST_LineLocatePoint` and `ST_Length` measure in the units of the
coordinate system they are handed. On raw WGS 84 that unit is *degrees*, and at 51° N a degree of
longitude covers ~63 % of the ground distance of a degree of latitude — so an east-west corridor
would have its offsets systematically compressed against a north-south one, in a
direction-dependent way no aggregate would reveal. Everything is projected through
`to_metric_crs()` into ETRS89 / UTM 32N (EPSG:25832), the official German national grid, first.
Selection (`ST_DWithin`) stays on `geography`, where metres are true metres on the spheroid.

**The 22 kW / 50 kW trap.** `ChargingCategory.fast` begins at **22 kW**; `is_fast_charger` begins
at **50 kW** (both `BUILD_SPEC.md` §2). A 22-49 kW site is therefore categorised `fast` and is
*not* a fast charger. This is the most confusable pair of definitions in the schema and the two
are asserted as one-directional implications, not as an equivalence. Coverage and gap analysis
use the 50 kW flag — a 22 kW stop is a hotel, not a refuelling.

**Thresholds duplicated from Python.** `macros/enums.sql` re-implements
`EnergyIntensity.from_ratio`, `ChargingCategory.from_power`, `RoadClass.ordinal` and
`TrafficSeverity.delay_factor` in SQL. The duplication is deliberate and bounded: the warehouse
must bucket a value without a round-trip through Python, and the only acceptable way to do that
is one documented SQL copy of the rule. If a threshold changes in `autotwin_contracts.enums`, it
changes here in the same commit.

**Reconciled power.** The Ladesäulenregister describes a site's power twice — a site-level rating
and a list of connector ratings — and the two disagree often enough to matter.
`int_charging_station_power` computes both, reconciles them, and publishes `power_basis` so every
downstream number can say what it rests on, plus `has_category_disagreement` so the disagreement
is countable rather than arguable.

**Two accounts of trip energy.** `fct_trips` carries the consumer's running totals (`reported_*`)
and the same quantities recomputed from raw telemetry (`measured_*`), plus the delta. They come
from different code paths in different transactions and can drift; publishing both makes the
drift a number rather than a surprise. Downstream analytics should use `measured_*` — those are
reproducible from points still in the database.

**Penalties are attributions, not predictions.** `weather_penalty_percent` and
`traffic_penalty_percent` in `mart_route_energy` decompose a prediction the Python energy model
already made (`BUILD_SPEC.md` §10), reconstructing the HVAC and auxiliary terms from the same
documented anchors. They are not independent measurements and not a second model. Where an input
is unknown they are NULL, never zero — an unknown penalty and a zero penalty are different
statements.

**`is_active_now` is a snapshot.** `fct_traffic_events` freezes it at build time against
`evaluated_at`, which is published beside it. It exists so that `mart_traffic_impact` can
aggregate it. For a live answer, the API queries `traffic_events` directly.

---

## Known rough edge

dbt 1.10 introduced an `arguments:` block for generic-test parameters and deprecated the flat
form. This project keeps the flat form on purpose: `require-dbt-version` is `>=1.8.0, <2.0.0` per
`BUILD_SPEC.md` §16, and `arguments:` does not parse on 1.8 or 1.9. The result is a
`MissingArgumentsPropertyInGenericTestDeprecation` summary on every run — 94 occurrences, all
warnings, none of them errors. Raising the floor to 1.10 would silence it and narrow the
supported range; the spec sets the range, so the warnings stay.

---

## Attribution

Data in this layer carries obligations from its sources (`DATA_LICENSES.md`):

- Charging infrastructure: Bundesnetzagentur Ladesäulenregister, **CC BY 4.0**.
- Weather: **„Quelle: Deutscher Wetterdienst"** is required on anything derived from
  `stg_weather_observations`.
- Route geometry: **© OpenStreetMap contributors (ODbL)**, routed by OSRM.
- Traffic: Autobahn GmbH, **licence undeclared** — cached locally, never redistributed.
