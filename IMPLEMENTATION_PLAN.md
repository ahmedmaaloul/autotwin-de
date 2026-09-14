# AutoTwin DE — Implementation Plan

**Status:** in progress · **Started:** 2026-09-14 · **Spec:** [`docs/BUILD_SPEC.md`](docs/BUILD_SPEC.md)

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
- [ ] `autotwin_contracts`: enums, event envelopes, domain records, API DTOs
- [ ] `autotwin_core`: settings, structured logging, errors, DB session, SQLAlchemy models,
      provenance mixin, quality framework, geo helpers, provider ABCs
- [ ] Alembic `0001_initial_schema`, `0002_seed_vehicle_models`
- [ ] Docker Compose (postgres+postgis, redpanda, console), Makefile, `.env.example`
- [ ] FastAPI skeleton: `/health`, `/ready`, `/metrics`, error envelope, request IDs
- [ ] Next.js 15 app: shell, sidebar, top bar, theme, i18n, design tokens

### M2 — Charging infrastructure
- [ ] `BundesnetzagenturChargingProvider` (live download → cache → fixture)
- [ ] Ingestion pipeline: parse → validate → PostGIS upsert → `data_ingestion_runs`
- [ ] `/api/v1/charging/*` incl. GeoJSON + statistics
- [ ] `/charging` page: filtered map with clustering, detail drawer, distribution charts

### M3 — Routing
- [ ] `OSRMRoutingProvider` + `NominatimGeocodingProvider` with on-disk cache
- [ ] Route segmentation, demo corridors seeded (Frankfurt→Stuttgart flagship)
- [ ] `/api/v1/routes/plan`, `/routes` page with map

### M4 — Vehicle simulation & streaming
- [ ] Physics-based telemetry simulator, seeded and deterministic
- [ ] Redpanda producer, topics, consumer → PostGIS batch writer
- [ ] `TelemetrySink` abstraction so Kafka is optional
- [ ] SSE endpoint + `/live` digital-twin page
- [ ] `/simulation` control centre

### M5 — Weather & traffic
- [ ] `DWDWeatherProvider`, `AutobahnTrafficProvider` (+ Mobilithek adapter documented)
- [ ] Route enrichment: weather per segment, traffic events matched to segments
- [ ] Traffic + weather API and UI surfaces

### M6 — Energy model
- [ ] `PhysicalEnergyModel` (road-load) with unit tests on known values
- [ ] Feature engineering, LightGBM training pipeline, metrics vs baseline, SHAP
- [ ] `/api/v1/ml/*`, `/ml` page

### M7 — Charging optimisation
- [ ] SOC trajectory along the route, charging curve, beam search over stops
- [ ] `/api/v1/routes/optimize-charging`, charging-stop cards in `/routes`

### M8 — Analytics
- [ ] PostGIS corridor coverage, gap detection, underserved-corridor analysis
- [ ] `/analytics` page, research report in `docs/research/`

### M9 — Engineering quality
- [ ] dbt staging/marts + tests, Airflow DAGs (optional profile)
- [ ] Data-quality page, observability (structured logs, Prometheus metrics)
- [ ] pytest / vitest / Playwright suites, GitHub Actions CI
- [ ] README, README.de, ARCHITECTURE, ADRs

### M10 — Optional copilot
- [ ] `OllamaLLMProvider` + tool-calling over platform data; degrades to 503 when disabled

---

## Execution model

Work is parallelised by **disjoint file ownership** — no two concurrent workstreams write the
same path. The shared spine (`contracts` + `core` + migrations) is built first and frozen before
fan-out, because every other module compiles against it.
