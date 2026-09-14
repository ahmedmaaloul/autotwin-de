# AutoTwin DE — Implementation Plan

**Status:** milestones 1–9 complete · **Started:** 2026-09-14 · **Updated:** 2026-09-15 · **Spec:** [`docs/BUILD_SPEC.md`](docs/BUILD_SPEC.md)

AutoTwin DE is a digital twin of German electric road mobility: official German
infrastructure and environment data, a physics-based connected-vehicle simulator, geospatial
analytics on PostGIS, an energy-consumption model, and charging optimisation — served through a
FastAPI backend and a Next.js engineering dashboard.

---

## Guiding decisions

| Decision | Choice | Why |
|---|---|---|
| Monorepo layout | uv workspace (Python) + pnpm (web) | One `make setup`, no cross-repo drift |
| Geospatial store | PostgreSQL 16 + PostGIS 3.4 | Corridor/gap analysis belongs in the database, not in Python loops |
| Streaming | Redpanda (Kafka API) | Kafka semantics without ZooKeeper/JVM weight; optional via `KAFKA_ENABLED` |
| Analytical layer | Parquet + DuckDB + Polars | Right size for a laptop; Spark would be résumé-driven design |
| Transformations | dbt-core on Postgres | Real staging→marts lineage + tests, not decoration |
| Orchestration | Airflow, **optional compose profile** | Scheduled ingestion is real, but must not gate `make dev` |
| ML | LightGBM + SHAP, physical baseline for comparison | A model is only credible against a baseline |
| Routing | OSRM behind `RoutingProvider` | Public demo server for dev, local Docker for full control |
| Mapping | MapLibre GL JS + keyless tiles | No Mapbox token, no credit card |
| Serialisation | JSON events, versioned topics | No schema registry to operate; version in the envelope |
| Simulation honesty | `data_origin` on every row; `SIMULIERT` badge in UI | Non-negotiable |

Deferred deliberately: Kubernetes, service mesh, Redis, Elasticsearch, GraphQL, CQRS, Spark,
Flink. Each is documented in `docs/adr/` as *considered and rejected for this scope*.

---

## Milestones

### M1 — Foundation
- [x] Monorepo skeleton, `.gitignore`, `.editorconfig`, uv workspace, ruff/mypy/pytest config
- [x] `docs/BUILD_SPEC.md` — the contract all modules are written against
- [x] `autotwin_contracts`: enums, event envelopes, domain records, API DTOs
- [x] `autotwin_core`: settings, structured logging, errors, DB session, SQLAlchemy models,
      provenance mixin, quality framework, geo helpers, provider ABCs
- [x] Alembic `0001_initial_schema`, `0002_seed_vehicle_models`
- [x] Docker Compose (postgres+postgis, redpanda, console), Makefile, `.env.example`
- [x] FastAPI skeleton: `/health`, `/ready`, `/metrics`, error envelope, request IDs
- [x] Next.js 15 app: shell, sidebar, top bar, theme, i18n, design tokens

### M2 — Charging infrastructure
- [x] `BundesnetzagenturChargingProvider` (live download → cache → fixture)
- [x] Ingestion pipeline: parse → validate → PostGIS upsert → `data_ingestion_runs`
- [x] `/api/v1/charging/*` incl. GeoJSON + statistics
- [x] `/charging` page: filtered map with clustering, detail drawer, distribution charts

### M3 — Routing
- [x] `OSRMRoutingProvider` + `NominatimGeocodingProvider` with on-disk cache
- [x] Route segmentation, demo corridors seeded (Frankfurt→Stuttgart flagship)
- [x] `/api/v1/routes/plan`, `/routes` page with map

### M4 — Vehicle simulation & streaming
- [x] Physics-based telemetry simulator, seeded and deterministic
- [x] Redpanda producer, topics, consumer → PostGIS batch writer
- [x] `TelemetrySink` abstraction so Kafka is optional
- [x] SSE endpoint + `/live` digital-twin page
- [x] `/simulation` control centre

### M5 — Weather & traffic
- [x] `DWDWeatherProvider`, `AutobahnTrafficProvider` (+ Mobilithek adapter documented)
- [x] Route enrichment: weather per segment, traffic events matched to segments
- [x] Traffic + weather API and UI surfaces

### M6 — Energy model
- [x] `PhysicalEnergyModel` (road-load) with unit tests on known values
- [x] Feature engineering, LightGBM training pipeline, metrics vs baseline, SHAP
- [x] `/api/v1/ml/*`, `/ml` page

### M7 — Charging optimisation
- [x] SOC trajectory along the route, charging curve, beam search over stops
- [x] `/api/v1/routes/optimize-charging`, charging-stop cards in `/routes`

### M8 — Analytics
- [x] PostGIS corridor coverage, gap detection, underserved-corridor analysis
- [x] `/analytics` page, research report in `docs/research/`

### M9 — Engineering quality
- [x] dbt staging/marts + tests, Airflow DAGs (optional profile)
- [x] Data-quality page, observability (structured logs, Prometheus metrics)
- [x] pytest / vitest / Playwright suites, GitHub Actions CI
- [x] README, README.de, ARCHITECTURE, ADRs

### M10 — AI access: delivered as an MCP server, not a chat endpoint
- [x] `services/mcp` — `autotwin_mcp` exposes eight tools over stdio to any MCP client
      (Claude Desktop, Cursor, Zed): route analysis, charging plan, corridor coverage,
      underserved corridors, station search, data quality, routes, vehicle profiles
- [x] Tool results reuse the API's own service functions; geometry omitted unless asked
- [x] 14 tests, 7 of them integration against PostGIS
- [x] `POST /api/v1/copilot/ask` kept as an honest 503 pointing at the MCP server

The brief's optional copilot was a model behind an HTTP endpoint. A tool server is the better
design for the same goal: the model can only *phrase* platform output, never compute a kWh
figure; it costs nothing to run because the platform hosts no model; and it is a smaller,
testable surface. The deterministic "why" a chat wrapper would have verbalised already exists
in `autotwin_ml.insights`.

---

## Execution model

Work is parallelised by **disjoint file ownership** — no two concurrent workstreams write the
same path. The shared spine (`contracts` + `core` + migrations) is built first and frozen before
fan-out, because every other module compiles against it.


---

## Verified state (2026-09-15)

| Check | Result |
|---|---|
| `uv run pytest` | **1 082 passed**, 0 xfailed |
| `uv run pytest -m "not integration and not kafka"` (CI selection) | 1 025 passed, 56 deselected |
| `uv run ruff check .` / `ruff format --check .` | clean, 174 files |
| `uv run mypy` (strict) | **no issues in 114 source files** |
| `pnpm test:run` | **322 passed** |
| `pnpm lint` / `pnpm typecheck` / `pnpm build` | clean (2 React-Compiler notices on TanStack Table) |
| `pnpm exec playwright test smoke` | **20 passed** incl. the full Frankfurt→Stuttgart flow |
| `dbt build` | 9 marts, 1 incremental model, 440+ tests |
| Live ingestion | 116 440 charging stations · 219 821 points · 1 703 traffic events |

**Seven defects were found by the test suites and fixed**, each now pinned by the regression
test that first documented it: a charging-curve floor applied to the wrong term, a
counterfactual rung that misattributed an explicit duration to headwind, an off-by-one in
`iter_records(limit=0)`, a traffic provider that reported cached data as freshly fetched, a
Prometheus label that collapsed five endpoints into `unmatched`, a locale-independent delta
in `MetricCard`, and an `ApiError` classification that treated an upstream 502 as a client
error. Two more were found by manual verification: map layers gated on an event that never
fires in a throttled render loop, and nested `<main>` landmarks.
