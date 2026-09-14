# AutoTwin DE — Internal Build Specification

> **This file is the contract.** Every module in this repository is written against it.
> If you are implementing part of AutoTwin DE, read this file completely first and follow it
> exactly. Where it is silent, choose the most maintainable option and add a note to
> `docs/adr/`. Never redefine a name that is defined here.

---

## 0. Ground rules

1. **Zero cost.** No paid API, no cloud account, no credit card, ever.
2. **Honesty.** Real data and simulated data are never mixed invisibly. Every row carries
   provenance. Simulated telemetry is labelled `SIMULIERT` in the UI.
3. **Degrade, never crash.** If an external source is down, the provider raises a typed
   `ProviderError`, the caller falls back to cached/fixture data, and the response is marked
   `degraded`. The app keeps working.
4. **No stub theatre.** Do not write a function that returns a hardcoded value and call it an
   integration. Either implement it, or raise `NotImplementedError` and say so in the docs.
5. **Style.** `ruff` (line length 100) and `mypy --strict` must pass. TypeScript is `strict`.
   Fix lint errors; never add blanket `# noqa` / `// @ts-ignore`.

---

## 1. Repository layout & package ownership

```
packages/contracts/src/autotwin_contracts/   # autotwin_contracts — pure Pydantic, zero deps on DB
packages/core/src/autotwin_core/             # autotwin_core     — settings, DB, models, quality, geo
services/api/src/autotwin_api/               # autotwin_api      — FastAPI
services/ingestion/src/autotwin_ingestion/   # autotwin_ingestion— providers + pipelines
services/simulator/src/autotwin_simulator/   # autotwin_simulator— vehicle physics + runner
services/streaming/src/autotwin_streaming/   # autotwin_streaming— Kafka producer/consumer
services/ml/src/autotwin_ml/                 # autotwin_ml       — features, training, inference, SHAP
apps/web/                                    # Next.js 15 App Router
alembic/                                     # migrations (targets autotwin_core.db.base.Base)
```

**Dependency direction (strict, no cycles):**

```
contracts  ←  core  ←  { ingestion, simulator, streaming, ml }  ←  api
```

`contracts` imports nothing from the project. `core` imports only `contracts`.
`api` may import everything. Nothing imports `api`.

Python packages use a `src/` layout; each workspace member has its own `pyproject.toml`
declaring only the dependencies it actually uses.

---

## 2. Canonical enums — `autotwin_contracts.enums`

Every enum is `class X(str, Enum)` so it serialises as its value.
**These exact member values are used in the database, the API, and the frontend.**

```python
DataOrigin        = official | simulated | derived
SourceSystem      = bundesnetzagentur | dwd | autobahn | mobilithek | osm | osrm | simulator | derived
IngestionStatus   = healthy | delayed | degraded | failed | simulation
IngestionOutcome  = success | partial | failed
ChargingCategory  = normal | fast | ultra_fast     # <22 kW | 22–149 kW | >=150 kW
CurrentType       = ac | dc | unknown
ConnectorType     = type2 | ccs | chademo | schuko | tesla | cee | other | unknown
Bundesland        = BW BY BE BB HB HH HE MV NI NW RP SL SN ST SH TH   # ISO 3166-2:DE codes, no "DE-" prefix
RoadClass         = motorway | trunk | primary | secondary | tertiary | residential | service | unknown
TrafficEventType  = roadworks | closure | incident | warning | congestion | other
TrafficSeverity   = low | moderate | high | severe
WeatherCondition  = clear | clouds | rain | snow | fog | storm | unknown
VehicleClass      = compact | sedan | performance | suv | van
SimulationState   = pending | running | paused | stopping | stopped | completed | failed
VehicleState      = idle | driving | charging | stopped | completed
EnergyIntensity   = low | medium | high | critical
ProviderMode      = live | cache | fixture       # how a provider answered
```

`Bundesland` also exposes `.label_de` (e.g. `BW → "Baden-Württemberg"`).

---

## 3. Database schema (PostgreSQL 16 + PostGIS 3.4)

**Conventions**

- All tables are in schema `public`. Table names are plural `snake_case`.
- Primary keys: `id UUID PRIMARY KEY` (application-generated `uuid4`) unless stated.
- All timestamps are `TIMESTAMP WITH TIME ZONE`, always stored in **UTC**.
- Point geometry: `geography(Point, 4326)` — lets PostGIS return metres from `ST_Distance`.
  Line geometry: `geometry(LineString, 4326)` — cheaper for rendering/simplification;
  cast to `::geography` when a length in metres is needed.
- Every externally-sourced table embeds the **provenance block** (§3.1).
- Money/energy/physical floats are `DOUBLE PRECISION`. Percentages are `0–100` floats.
- Use `server_default=func.now()` for `created_at`; `onupdate` for `updated_at`.

### 3.1 Provenance block (mixin `ProvenanceMixin` in `autotwin_core.db.mixins`)

Present on `charging_stations`, `charging_points`, `weather_observations`,
`traffic_events`, `routes`, `vehicles`, `trips`.

| column | type | note |
|---|---|---|
| `source` | `SourceSystem` enum, not null | who produced the row |
| `source_identifier` | `text`, nullable | the id in the source system |
| `source_url` | `text`, nullable | document/endpoint it came from |
| `source_timestamp` | `timestamptz`, nullable | when the *source* says it was valid |
| `data_origin` | `DataOrigin` enum, not null | `official` / `simulated` / `derived` |
| `ingestion_run_id` | `uuid`, FK → `data_ingestion_runs.id`, nullable, ON DELETE SET NULL |
| `ingested_at` | `timestamptz`, not null |  |

### 3.2 Tables

**`data_ingestion_runs`** — one row per pipeline execution.
`id`, `source` (SourceSystem), `pipeline` (text, e.g. `bnetza_charging_stations`),
`started_at`, `finished_at` (null while running), `status` (IngestionOutcome),
`provider_mode` (ProviderMode), `rows_received` int, `rows_accepted` int,
`rows_rejected` int, `rows_duplicate` int, `bytes_downloaded` bigint null,
`source_url` text null, `source_file_sha256` text null, `error_message` text null,
`quality_report` JSONB null (see §6), `created_at`.
Index: `(source, started_at DESC)`.

**`charging_stations`** — one physical site.
`id`, `external_id` text (natural key from source, e.g. `bnetza:<hash>`) **UNIQUE**,
`operator` text null, `street`, `house_number`, `postal_code`, `city`, `bundesland` (enum null),
`country` text default `'DE'`, `location geography(Point,4326)` not null,
`commissioned_on` date null, `charging_points_count` int not null default 1,
`max_power_kw` double null, `total_power_kw` double null,
`charging_category` (ChargingCategory) not null, `is_fast_charger` bool not null (generated in app,
`max_power_kw >= 50`), `raw` JSONB null (original source record, trimmed), + provenance.
Indexes: `GIST(location)`, `(bundesland)`, `(charging_category)`, `(max_power_kw DESC)`,
`(operator)`, `UNIQUE(external_id)`.

**`charging_points`** — connectors at a station.
`id`, `station_id` FK → charging_stations ON DELETE CASCADE, `ordinal` int,
`connector_type` (ConnectorType), `current_type` (CurrentType), `power_kw` double null,
`public_key` text null. Index `(station_id)`.

**`weather_stations`** — DWD stations. `id`, `dwd_station_id` text UNIQUE, `name`,
`location geography(Point,4326)`, `elevation_m` double null, `bundesland` null,
`valid_from` date null, `valid_to` date null. GIST index.

**`weather_observations`**
`id`, `weather_station_id` FK null, `observed_at` timestamptz not null,
`location geography(Point,4326)` not null, `temperature_c` double null,
`precipitation_mm` double null, `wind_speed_ms` double null, `wind_gust_ms` double null,
`humidity_percent` double null, `pressure_hpa` double null,
`condition` (WeatherCondition) not null default `unknown`, + provenance.
Indexes: `GIST(location)`, `(observed_at DESC)`, `UNIQUE(weather_station_id, observed_at)`.

**`traffic_events`**
`id`, `external_id` text UNIQUE null, `event_type` (TrafficEventType), `severity` (TrafficSeverity),
`road_name` text null (e.g. `A5`), `direction` text null, `title` text, `description` text null,
`location geography(Point,4326)` not null, `geometry geometry(LineString,4326)` null,
`starts_at` timestamptz null, `ends_at` timestamptz null, `is_blocked` bool default false,
`delay_minutes` double null, `raw` JSONB null, + provenance.
Indexes: `GIST(location)`, `(event_type)`, `(starts_at DESC)`, `(road_name)`.

**`routes`** — a planned/known corridor.
`id`, `slug` text UNIQUE null (e.g. `frankfurt-stuttgart`), `name` text,
`origin_name`, `destination_name`, `origin geography(Point,4326)`, `destination geography(Point,4326)`,
`geometry geometry(LineString,4326)` not null, `distance_m` double, `duration_s` double,
`routing_profile` text default `'driving'`, `is_demo` bool default false, + provenance.
Index: `GIST(geometry)`, `UNIQUE(slug)`.

**`route_segments`** — the route cut into analysis chunks.
`id`, `route_id` FK CASCADE, `ordinal` int, `geometry geometry(LineString,4326)`,
`start_offset_m` double, `distance_m` double, `road_class` (RoadClass),
`speed_limit_kmh` double null, `assumed_speed_kmh` double null,
`elevation_gain_m` double null, `temperature_c` double null,
`traffic_severity` (TrafficSeverity) null, `predicted_kwh_per_100km` double null,
`predicted_kwh` double null, `energy_intensity` (EnergyIntensity) null.
Indexes: `(route_id, ordinal)` UNIQUE, `GIST(geometry)`.

**`vehicle_models`** — EV profiles (§9). `id`, `code` text UNIQUE (e.g. `compact_ev`),
`display_name`, `vehicle_class` (VehicleClass), `battery_capacity_kwh` double,
`usable_capacity_kwh` double, `nominal_consumption_kwh_100km` double,
`max_dc_power_kw` double, `max_ac_power_kw` double, `mass_kg` double,
`drag_coefficient` double, `frontal_area_m2` double, `rolling_resistance` double,
`is_generic` bool default true.

**`vehicles`** — simulated vehicle instances.
`id`, `vehicle_id` text UNIQUE (human id, e.g. `ATW-0042`), `vehicle_model_id` FK,
`simulation_run_id` FK null, `state` (VehicleState), `created_at`, + provenance
(always `data_origin = simulated`).

**`trips`**
`id`, `trip_id` text UNIQUE, `vehicle_id` FK → vehicles, `route_id` FK → routes null,
`started_at`, `ended_at` null, `start_soc_percent`, `end_soc_percent` null,
`distance_m` double default 0, `energy_kwh` double default 0,
`avg_consumption_kwh_100km` double null, `state` (VehicleState), + provenance.
Index: `(vehicle_id, started_at DESC)`.

**`telemetry`** — high-volume. **No provenance mixin** (always simulated); has `data_origin` only.
`id` bigserial PK (not UUID — volume), `vehicle_id` text not null, `trip_id` text null,
`route_id` uuid null, `recorded_at` timestamptz not null,
`location geography(Point,4326)` not null, `speed_kmh` double, `acceleration_ms2` double,
`heading_deg` double null, `battery_soc_percent` double, `battery_temperature_c` double,
`outside_temperature_c` double, `instantaneous_power_kw` double,
`energy_consumption_kwh_100km` double, `cumulative_energy_kwh` double,
`estimated_range_km` double, `road_class` (RoadClass), `speed_limit_kmh` double null,
`odometer_m` double, `state` (VehicleState), `data_origin` (DataOrigin) default `simulated`,
`ingested_at` timestamptz default now().
Indexes: `(vehicle_id, recorded_at DESC)`, `(recorded_at DESC)`, `(trip_id)`, `GIST(location)`.

**`simulation_runs`**
`id`, `name` text, `state` (SimulationState), `vehicle_count` int, `speed_factor` double,
`weather_mode` text, `traffic_intensity` text, `vehicle_mix` JSONB, `route_slugs` JSONB,
`seed` int, `started_at` null, `stopped_at` null, `events_emitted` bigint default 0,
`errors` int default 0, `config` JSONB, `created_at`.

**`ml_models`** — registry. `id`, `name` text, `version` text, `algorithm` text,
`trained_at`, `training_rows` int, `feature_names` JSONB, `metrics` JSONB
(`{mae, rmse, r2, baseline_mae, baseline_rmse, baseline_r2}`), `artifact_path` text,
`is_active` bool, `training_data_origin` (DataOrigin), `notes` text.
UNIQUE `(name, version)`.

**`ml_predictions`** — audit of served predictions. `id`, `ml_model_id` FK null,
`route_id` uuid null, `trip_id` text null, `created_at`, `features` JSONB,
`prediction_kwh_100km` double, `baseline_kwh_100km` double null,
`shap_values` JSONB null, `context` JSONB null.

### 3.3 Migrations

Alembic, `alembic/versions/`. First migration `0001_initial_schema`:
`CREATE EXTENSION IF NOT EXISTS postgis;` then all tables and indexes.
Second migration `0002_seed_vehicle_models` inserts the five generic EV profiles (§9).
**Never** create tables from application startup code.

---

## 4. Provider architecture — `autotwin_core.providers`

Abstract bases live in `autotwin_core.providers.base`; concrete adapters live in
`autotwin_ingestion.providers.*` (except routing, which the API also needs at request time —
`OSRMRoutingProvider` lives in `autotwin_ingestion.providers.routing` and is imported by the API).

```python
class ProviderError(Exception):            # base
class ProviderUnavailable(ProviderError)   # network/5xx/DNS
class ProviderTimeout(ProviderError)
class InvalidSourceData(ProviderError)     # parsed but wrong shape
class RateLimited(ProviderError)
class ConfigurationMissing(ProviderError)  # credentials/paths not configured
```

Every provider result is wrapped:

```python
@dataclass(frozen=True, slots=True)
class ProviderResult[T]:
    data: T
    mode: ProviderMode          # live | cache | fixture
    source_url: str | None
    fetched_at: datetime
    warnings: list[str] = field(default_factory=list)
```

Abstract interfaces (all `Protocol`-free plain ABCs, all async where I/O-bound):

```python
class ChargingInfrastructureProvider(ABC):
    async def fetch_stations(self) -> ProviderResult[list[ChargingStationRecord]]
class WeatherProvider(ABC):
    async def fetch_observations(self, points, at=None) -> ProviderResult[list[WeatherRecord]]
class TrafficProvider(ABC):
    async def fetch_events(self, bbox=None) -> ProviderResult[list[TrafficEventRecord]]
class RoutingProvider(ABC):
    async def route(self, origin: Coordinate, destination: Coordinate,
                    *, profile="driving") -> ProviderResult[RouteResult]
class GeocodingProvider(ABC):
    async def geocode(self, query: str) -> ProviderResult[list[Place]]
class LLMProvider(ABC):
    async def complete(self, messages, tools=None) -> LLMResponse
```

**Fallback chain (mandatory):** `live → on-disk cache (data/raw + TTL) → bundled fixture`.
Every adapter sets `mode` truthfully. The API surfaces `mode` so the UI can say
*"Live source unavailable — showing cached data"*.

**Settings switch:** `AUTOTWIN_DATA_MODE = live | cached | fixture` (default `cached`:
try live, fall back silently to cache/fixture). In tests it is always `fixture`.

---

## 5. Settings — `autotwin_core.config.Settings` (pydantic-settings)

Env prefix `AUTOTWIN_`, loaded from `.env`. Key fields:

```
ENV=local|ci|docker           DEBUG=false           LOG_LEVEL=INFO      LOG_FORMAT=json|console
DATABASE_URL=postgresql+psycopg://autotwin:autotwin@localhost:5433/autotwin
DATABASE_URL_SYNC (derived)   DB_POOL_SIZE=10
KAFKA_BOOTSTRAP_SERVERS=localhost:19092   KAFKA_ENABLED=true   KAFKA_CLIENT_ID=autotwin
DATA_MODE=cached              DATA_DIR=./data       CACHE_TTL_SECONDS=21600
OSRM_BASE_URL=https://router.project-osrm.org      OSRM_TIMEOUT_S=20
BNETZA_DOWNLOAD_URL=...       DWD_BASE_URL=...      AUTOBAHN_BASE_URL=...
NOMINATIM_BASE_URL=https://nominatim.openstreetmap.org
HTTP_TIMEOUT_S=20             HTTP_USER_AGENT="AutoTwinDE/0.1 (portfolio project; +github)"
CORS_ORIGINS=["http://localhost:3000"]
ML_MODEL_DIR=./models         ML_ACTIVE_MODEL=energy_consumption
OLLAMA_BASE_URL=http://localhost:11434    OLLAMA_MODEL=llama3.2    LLM_ENABLED=false
SIM_DEFAULT_VEHICLES=40       SIM_TICK_SECONDS=1.0   SIM_SPEED_FACTOR=10.0   SIM_SEED=20260214
```

`get_settings()` is `@lru_cache`d. Never read `os.environ` outside this module.

**Ports (fixed):** web `3000`, API `8000`, Postgres `5433`, Redpanda `19092`,
Redpanda admin `9644`, Redpanda Console `8088`, OSRM (optional) `5001`,
Prometheus `9090`, Grafana `3001`, Airflow `8082`.

---

## 6. Data quality — `autotwin_core.quality`

A tiny, dependency-free validation framework (no Great Expectations — overkill here).

```python
@dataclass(frozen=True) class Violation: rule: str; field: str | None; message: str; row_index: int | None
@dataclass class QualityReport:
    rows_received: int; rows_accepted: int; rows_rejected: int; rows_duplicate: int
    violations: list[Violation]; rule_stats: dict[str, int]
    def as_dict(self) -> dict   # stored in data_ingestion_runs.quality_report
```

Rules implemented as small callables in `autotwin_core.quality.rules`:
`latitude_in_range`, `longitude_in_range`, `within_germany_bbox`
(`lat 47.2–55.1`, `lon 5.8–15.1`), `positive_power`, `soc_in_range`,
`timestamp_not_future`, `timestamp_present`, `required_field`, `no_duplicate_key`,
`monotonic_timestamps`.

Each pipeline builds a `QualityReport`, persists it on the `data_ingestion_runs` row and
**rejects** bad rows rather than writing them. `GET /api/v1/data/quality` reads it back.

---

## 7. API contract — `autotwin_api`

Base path `/api/v1`. FastAPI with `lifespan`, OpenAPI at `/openapi.json`, docs at `/docs`.

**Envelope for lists** (`autotwin_contracts.api.Page[T]`):
```json
{ "items": [...], "total": 1234, "page": 1, "page_size": 50, "has_next": true }
```
Query params: `page` (≥1, default 1), `page_size` (1–500, default 50).

**Error envelope** (every 4xx/5xx, via exception handlers):
```json
{ "error": { "code": "provider_unavailable", "message": "DWD open data unreachable",
             "details": {...}, "request_id": "01JG..." } }
```
`code` is a stable snake_case string; map `ProviderError` subclasses onto
`provider_unavailable | provider_timeout | invalid_source_data | rate_limited | configuration_missing`
with status `503 | 504 | 502 | 429 | 500`. Validation → `422 validation_error`.
Not found → `404 not_found`.

**Data-freshness header** on data endpoints: `X-AutoTwin-Data-Mode: live|cache|fixture`.

**Routes** (every one must exist and return real data):

```
GET  /health                              -> {status, version, uptime_s}
GET  /ready                               -> {status, checks:{database, kafka, model}}
GET  /metrics                             -> Prometheus text

GET  /api/v1/dashboard/summary            -> DashboardSummary (§7.1)

GET  /api/v1/charging/stations            ?bbox&bundesland&operator&min_power_kw&fast_only
                                           &category&commissioned_from&commissioned_to&q&page&page_size
GET  /api/v1/charging/stations/geojson    ?same filters, capped at 20000 -> GeoJSON FeatureCollection
GET  /api/v1/charging/stations/{id}       -> ChargingStationDetail (incl. points + provenance)
GET  /api/v1/charging/statistics          -> by_bundesland[], by_power_class[], by_operator[], growth[]
GET  /api/v1/charging/coverage            ?route_id|route_slug&buffer_km&min_power_kw
                                           -> CorridorCoverage (§7.2)
GET  /api/v1/charging/underserved         ?min_power_kw&max_gap_km&corridor_buffer_km&route_slug
                                           -> UnderservedReport (§7.2)

GET  /api/v1/weather                      ?bbox|lat&lon&radius_km&at -> Page[WeatherObservation]
GET  /api/v1/weather/latest               ?lat&lon -> WeatherObservation

GET  /api/v1/traffic/events               ?bbox&event_type&severity&road&active_only&page&page_size
GET  /api/v1/traffic/events/geojson       -> GeoJSON FeatureCollection

GET  /api/v1/routes                       -> Page[RouteSummary]   (demo corridors first)
GET  /api/v1/routes/{id}                  -> RouteDetail
POST /api/v1/routes/plan                  RoutePlanRequest  -> RoutePlanResponse    (geometry only)
POST /api/v1/routes/analyze               RouteAnalyzeRequest -> RouteAnalysis (§7.3)  ★ flagship
POST /api/v1/routes/optimize-charging     ChargingOptimizeRequest -> ChargingPlan (§7.4)
GET  /api/v1/routes/geocode               ?q -> list[Place]

GET  /api/v1/vehicles                     ?state&page&page_size -> Page[VehicleSummary]
GET  /api/v1/vehicles/live                -> list[VehicleLive]  (latest telemetry per vehicle, ≤1000)
GET  /api/v1/vehicles/{vehicle_id}        -> VehicleDetail
GET  /api/v1/vehicles/{vehicle_id}/telemetry ?since&limit -> list[TelemetryPoint]

GET  /api/v1/trips                        ?vehicle_id&page&page_size -> Page[TripSummary]
GET  /api/v1/trips/{trip_id}              -> TripDetail (incl. soc/speed series)

GET  /api/v1/simulations                  -> Page[SimulationRunSummary]
POST /api/v1/simulations                  SimulationCreate -> SimulationRunDetail
GET  /api/v1/simulations/{id}             -> SimulationRunDetail
POST /api/v1/simulations/{id}/start       -> SimulationRunDetail
POST /api/v1/simulations/{id}/pause       -> SimulationRunDetail
POST /api/v1/simulations/{id}/resume      -> SimulationRunDetail
POST /api/v1/simulations/{id}/stop        -> SimulationRunDetail
POST /api/v1/simulations/{id}/reset       -> SimulationRunDetail
GET  /api/v1/simulations/status           -> SimulatorStatus {state, vehicles_active, events_per_second,
                                              messages_processed, db_writes, errors, kafka: {...}}

GET  /api/v1/ml/models                    -> list[MLModelInfo]
GET  /api/v1/ml/metrics                   ?model=&version= -> MLMetrics (incl. baseline comparison,
                                              predicted_vs_actual[], residuals[], feature_importance[])
POST /api/v1/ml/predict                   PredictRequest -> PredictResponse (+ SHAP contributions)
GET  /api/v1/ml/explain/global            -> global SHAP feature importance

GET  /api/v1/data/quality                 -> list[SourceQuality] (one per SourceSystem)
GET  /api/v1/data/ingestions              ?source&page&page_size -> Page[IngestionRun]
GET  /api/v1/data/sources                 -> list[DataSourceInfo] (name, licence, url, last_run, origin)

GET  /api/v1/stream/telemetry             -> text/event-stream (SSE), see §8
GET  /api/v1/analytics/energy             ?dimension=temperature|speed|traffic -> scatter buckets
GET  /api/v1/analytics/regions            -> per-Bundesland infrastructure comparison

POST /api/v1/copilot/ask                  (optional; 503 when LLM_ENABLED=false)
```

### 7.1 `DashboardSummary`
```ts
{ vehicles_active, charging_stations_total, fast_charging_points_total,
  traffic_events_active, avg_consumption_kwh_100km, data_freshness:
  [{source, last_run_at, status, age_minutes, data_origin, mode}],
  energy_trend: [{bucket, kwh_100km}], recent_traffic: TrafficEvent[5],
  weather_snapshot: {temperature_c, condition, observed_at, station},
  charging_by_category: [{category, count}] }
```

### 7.2 Coverage / underserved
`CorridorCoverage`: `{route_id, buffer_km, min_power_kw, stations_in_corridor,
fast_stations_in_corridor, stations_per_100km, max_gap_km, mean_gap_km,
gaps: [{start_offset_km, end_offset_km, gap_km, geometry}], coverage_score}` (0–100).

`UnderservedReport`: `{parameters, corridors: [{route_slug, name, worst_gap_km, segments:[...]}],
generated_at, methodology}` — `methodology` is a human-readable sentence so the UI can show
exactly how the number was produced.

### 7.3 `RouteAnalysis` — the flagship payload
```ts
{ route: {id, distance_m, duration_s, geometry(GeoJSON LineString), origin, destination},
  vehicle: {code, display_name, battery_capacity_kwh, ...},
  start_soc_percent, arrival_soc_percent, min_soc_percent_required,
  energy_kwh_total, avg_consumption_kwh_100km,
  baseline_kwh_100km, model_kwh_100km, model_name, model_version,
  weather_penalty_percent, traffic_penalty_percent, total_penalty_percent,
  charging_required: bool,
  segments: [{ordinal, start_offset_km, distance_km, road_class, speed_limit_kmh,
              assumed_speed_kmh, temperature_c, traffic_severity, kwh, kwh_per_100km,
              soc_at_end_percent, energy_intensity, geometry}],
  traffic_events: TrafficEvent[], weather_points: [...],
  explanation: {headline, drivers: [{factor, delta_percent, direction, label_de, label_en}]},
  data_modes: {routing, weather, traffic},
  generated_at }
```

### 7.4 `ChargingPlan`
```ts
{ feasible: bool, reason: string|null,
  stops: [{station: ChargingStationSummary, arrival_soc_percent, departure_soc_percent,
           detour_km, charge_time_min, energy_added_kwh, max_power_kw, avg_power_kw,
           offset_km, rationale_de, rationale_en}],
  total_time_min, driving_time_min, charging_time_min, detour_km_total,
  arrival_soc_percent, alternatives_considered: int, objective: string }
```

---

## 8. Streaming

Broker: **Redpanda** (Kafka API). Topics (created by `infra/scripts/create-topics.sh`):

| topic | key | retention | payload |
|---|---|---|---|
| `vehicle.telemetry.v1` | `vehicle_id` | 6 h | `TelemetryEvent` |
| `vehicle.trip-events.v1` | `trip_id` | 24 h | `TripEvent` (`started`/`finished`/`charging_started`/`charging_finished`) |
| `traffic.events.v1` | `external_id` | 24 h | `TrafficEventMessage` |
| `charging.events.v1` | `station_id` | 24 h | `ChargingEvent` |

**Envelope** (`autotwin_contracts.events.EventEnvelope`), every message:
```json
{ "event_id": "uuid", "event_type": "vehicle.telemetry", "schema_version": 1,
  "occurred_at": "ISO-8601 UTC", "producer": "autotwin-simulator",
  "data_origin": "simulated", "payload": { ... } }
```
Serialisation: JSON (UTF-8). Documented ADR: JSON over Avro because the schema registry
would add operational weight without a consumer that needs it.

**Consumer** (`autotwin_streaming.consumer`): reads `vehicle.telemetry.v1`, validates with
Pydantic, batches (default 500 rows / 1 s), bulk-inserts into `telemetry`, updates
`vehicles.state` and `trips` aggregates. Commits offsets after a successful DB write
(at-least-once; the `telemetry` table tolerates duplicates because it is append-only
and the API always reads the latest row per vehicle).

**Kafka-optional:** if `KAFKA_ENABLED=false`, the simulator writes straight to Postgres through
the same `TelemetrySink` interface. The architecture is identical; the transport is swapped.
This keeps `make demo` runnable without a broker.

**SSE** `GET /api/v1/stream/telemetry?bbox=&vehicle_ids=`: server pushes a JSON array of changed
vehicles every ~1 s. Event name `telemetry`; also emits `: keep-alive` comments every 15 s.
It reads from the database (latest-per-vehicle query), not from Kafka, so a page refresh is cheap.
Payload is **deltas only** — vehicles whose `recorded_at` advanced since the client's last cursor.

---

## 9. Vehicle profiles (seeded in migration `0002`)

Generic, class-based, **not** reverse-engineered OEM data. Values are documented public-domain
order-of-magnitude figures for the vehicle *class*.

| code | class | battery kWh | usable kWh | nominal kWh/100km | max DC kW | max AC kW | mass kg | cd | area m² | crr |
|---|---|---|---|---|---|---|---|---|---|---|
| `compact_ev` | compact | 58 | 54 | 15.5 | 120 | 11 | 1700 | 0.27 | 2.20 | 0.010 |
| `sedan_ev` | sedan | 77 | 74 | 16.5 | 205 | 11 | 2100 | 0.23 | 2.30 | 0.010 |
| `performance_ev` | performance | 93 | 84 | 20.0 | 270 | 11 | 2200 | 0.25 | 2.35 | 0.011 |
| `suv_ev` | suv | 84 | 78 | 19.5 | 175 | 11 | 2400 | 0.29 | 2.65 | 0.011 |
| `van_ev` | van | 64 | 60 | 23.0 | 110 | 11 | 2500 | 0.33 | 3.30 | 0.012 |

---

## 10. Energy model

### 10.1 Physical baseline — `autotwin_ml.baseline.PhysicalEnergyModel`
Longitudinal road-load model, per segment, SI units:

```
F_roll  = crr * m * g * cos(theta)
F_aero  = 0.5 * rho(T) * cd * A * v^2        rho = 1.225 * 288.15/(273.15+T)
F_grade = m * g * sin(theta)
F_inert = m * a * 1.05                        (1.05 = rotating-mass factor)
P_wheel = (F_roll + F_aero + F_grade + F_inert) * v        [W]
P_batt  = P_wheel / eta_drive       if P_wheel >= 0        eta_drive = 0.90
        = P_wheel * eta_regen       if P_wheel <  0        eta_regen = 0.65 (capped at -50 kW)
P_aux   = base 0.35 kW + HVAC(T)   HVAC = 0 at 20 °C, rising to ~3.5 kW at -10 °C and ~2.0 kW at +35 °C
E_kwh   = (P_batt + P_aux) * t / 3.6e6
```
Plus a cold-battery efficiency penalty: internal resistance factor
`1 + max(0, (15 - T_batt)) * 0.008` applied to `P_batt`.
All constants live in one dataclass and are documented in `docs/ml/energy-model.md` as
an **educational engineering approximation**, explicitly not an OEM battery model.

### 10.2 ML model — `autotwin_ml`
LightGBM regressor (fallback to scikit-learn `HistGradientBoostingRegressor` if LightGBM
is unavailable on the platform). Target `energy_consumption_kwh_100km`.
Features (exact order, in `autotwin_ml.features.FEATURE_NAMES`):
```
speed_kmh, avg_speed_kmh, speed_std_kmh, acceleration_abs_mean_ms2, accel_events_per_km,
outside_temperature_c, battery_temperature_c, soc_percent, road_class_ordinal,
speed_limit_kmh, traffic_severity_ordinal, precipitation_mm, wind_speed_ms,
gradient_percent, segment_distance_km, mass_kg, drag_area, nominal_consumption_kwh_100km,
hvac_load_kw, is_motorway
```
Training set: aggregated simulated telemetry windows (**clearly labelled simulated**).
Split: grouped by `trip_id`, 70/15/15 train/val/test, fixed seed. Report MAE, RMSE, R² for
both ML and physical baseline on the same test set. SHAP `TreeExplainer` for global and
per-prediction attribution. Artefacts → `models/energy_consumption_v{N}.joblib` +
`models/energy_consumption_v{N}.metrics.json` (committed, small).

### 10.3 Charging optimisation — `autotwin_ml.charging` (pure Python, no solver dependency)
Simplified charging curve per vehicle:
`P(soc) = P_max * f(soc)`, `f = 1.0 (soc<50) → linear 1.0→0.55 (50–80) → linear 0.55→0.18 (80–100)`,
also derated by battery temperature. Candidate generation: stations within `corridor_buffer_km`
of the route, reachable with SOC ≥ `min_soc`, ranked by
`objective = driving_time + charging_time + 2 * detour_time + range_risk_penalty`.
Beam search over ≤3 stops, beam width 8. Deterministic. Documented assumptions.

---

## 11. Deterministic insight generator — `autotwin_ml.insights`
Takes a `RouteAnalysis` and emits `explanation.drivers` **without an LLM**:
compare the model prediction against the vehicle's nominal consumption, decompose the delta
into temperature / speed profile / traffic / gradient / auxiliary contributions (from the
physical model's own terms plus SHAP where available), sort by |delta|, emit bilingual labels.

---

## 12. Frontend contract — `apps/web`

- Next.js 15 App Router, React 19, TypeScript **strict**, Tailwind v4, shadcn/ui, Lucide,
  TanStack Query v5, Zod, Recharts, MapLibre GL JS, `next-intl`-style **custom lightweight i18n**
  (`lib/i18n`), `next-themes`.
- Package manager **pnpm**.
- API types are **generated** from OpenAPI into `types/api.generated.ts` via `openapi-typescript`
  (`pnpm gen:api`). Hand-written types only for things the API does not return.
- Server communication lives in `lib/api/*.ts` (typed fetchers) + `hooks/use-*.ts`
  (TanStack Query wrappers). **Components never call `fetch` directly.**
- Map: `components/maps/` — a single `<MapCanvas>` that owns the MapLibre instance and a
  declarative layer registry (`layers/*.ts`). Pages compose layers; **no `new maplibregl.Marker`
  inside a page component.**
- Every page implements: loading skeleton, empty state, error state (with the
  "Live-Quelle nicht verfügbar" message when `X-AutoTwin-Data-Mode` is `cache`/`fixture`).
- Default locale **de**; `en` available via the top-bar switch, persisted in a cookie and
  reflected in `<html lang>`. No hardcoded user-facing strings — everything through `t()`.
- Routes: `/` `/live` `/routes` `/charging` `/analytics` `/simulation` `/ml` `/data-quality`
  plus `/system` (status) and `/docs` (link-out).

**Design tokens** (`app/globals.css`, OKLCH, light + dark):
neutral base (zinc-like), `--primary` a restrained engineering blue,
semantic `--success` green / `--warning` amber / `--danger` red / `--info` blue.
Never a German-flag palette. Dense, precise, no glassmorphism, no neon.
Tabular numerals (`font-variant-numeric: tabular-nums`) on every metric.

---

## 13. Testing

- Backend `pytest`: unit tests for the energy model (known-value assertions), charging
  optimiser, quality rules, simulator physics invariants, provider parsing against committed
  fixtures (`respx` for HTTP), API endpoints via `httpx.ASGITransport` with a fixture-backed
  repository, geo helpers. Integration tests marked `@pytest.mark.integration`.
- Frontend: Vitest + RTL for components/hooks; Playwright smoke tests for
  dashboard load, route analysis, charging page, simulation controls, language toggle, theme toggle.
- CI (GitHub Actions, free runners only): `lint → typecheck → test` for both stacks + `next build`.

---

## 14. Naming & language conventions

- Python: `snake_case`, modules singular (`energy.py`), classes `PascalCase`.
- German domain words stay German **in the UI only**. Code identifiers are English.
  Exception: `bundesland` is used as an identifier because it is the official field name.
- Dates in code are `datetime` with `tzinfo=UTC`. Never naive datetimes.
- Distances in the database are **metres**; the API exposes kilometres where a human reads it
  (field names then end in `_km`). Be explicit in the field name, always.

---

## 15. Command-line contract

Every service exposes a `python -m <package>.cli` entry point built with `argparse`
(no Click/Typer dependency — the surface is small and argparse keeps the dependency tree
honest). These exact invocations are used by the `Makefile`, the Airflow DAGs, the Docker
images and the docs, so they are a contract.

```
python -m autotwin_ingestion.cli ingest charging   [--mode live|cached|fixture] [--dry-run] [--limit N]
python -m autotwin_ingestion.cli ingest weather    [--mode ...] [--stations N]
python -m autotwin_ingestion.cli ingest traffic    [--mode ...] [--roads A5,A8]
python -m autotwin_ingestion.cli seed routes                     # demo corridors via RoutingProvider
python -m autotwin_ingestion.cli seed demo         [--reset]     # everything `make demo` needs
python -m autotwin_ingestion.cli quality report                  # print the latest per-source report

python -m autotwin_simulator.cli run       [--vehicles N] [--speed-factor F] [--seed S]
                                           [--duration-s D] [--routes slug,slug] [--sink kafka|db]
python -m autotwin_simulator.cli generate-training-data [--trips N] [--out PATH]

python -m autotwin_streaming.cli consume    [--topic ...] [--batch-size N] [--from-beginning]
python -m autotwin_streaming.cli create-topics

python -m autotwin_ml.cli train    [--data PATH] [--version V] [--algorithm lightgbm|sklearn]
python -m autotwin_ml.cli evaluate [--version V]
python -m autotwin_ml.cli explain  [--version V] [--out PATH]
```

Every command:
- exits `0` on success, `1` on a handled failure, `2` on bad arguments;
- logs through `autotwin_core.logging` (never `print`, except for the deliberate human-readable
  summary a command ends with);
- accepts `--log-level` and `--json-logs`;
- writes a `data_ingestion_runs` row where it touches external data — including on failure.

## 16. Analytical layer contract (dbt)

dbt-core with the `dbt-postgres` adapter, project root `dbt/`, profile name `autotwin`,
`profiles.yml` committed at `dbt/profiles.yml` reading from environment variables
(`AUTOTWIN_DB_HOST`, `AUTOTWIN_DB_PORT`, `AUTOTWIN_DB_USER`, `AUTOTWIN_DB_PASSWORD`,
`AUTOTWIN_DB_NAME`), target schema `analytics`.

Sources are declared against the operational tables in `public` (§3.2). Layers:

```
staging/     stg_charging_stations · stg_charging_points · stg_weather_observations
             stg_traffic_events · stg_telemetry · stg_trips · stg_routes · stg_route_segments
intermediate/int_charging_station_power · int_trip_energy · int_corridor_stations
marts/       dim_charging_station · dim_route · dim_vehicle_model
             fct_vehicle_telemetry · fct_trips · fct_traffic_events
             mart_charging_coverage · mart_route_energy · mart_traffic_impact
             mart_charging_infrastructure
```

Tests are mandatory and must be meaningful: `unique` + `not_null` on every key,
`accepted_values` on every enum column (values taken from §2), `relationships` on every FK,
plus at least four singular tests under `dbt/tests/` asserting domain invariants
(SOC within 0–100, coordinates inside the Germany bounding box, no telemetry timestamp in the
future, no charging station with non-positive power).
