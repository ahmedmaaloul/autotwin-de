# Architecture Decision Records

Short records of the decisions that shaped AutoTwin DE. Each one states the context, the
decision, the alternatives that were rejected, and the consequences — including the bad ones.

An ADR is written when a choice would otherwise be invisible in the code and expensive to
reverse. It is never edited after being accepted; it is superseded by a new record.

| # | Decision | Status |
|---|---|---|
| [001](001-postgis-for-geospatial-storage.md) | PostGIS is the geospatial engine, not Python | Accepted |
| [002](002-redpanda-for-local-streaming.md) | Redpanda for Kafka-compatible local streaming | Accepted |
| [003](003-maplibre-for-mapping.md) | MapLibre GL JS with keyless vector tiles | Accepted |
| [004](004-simulation-vs-real-vehicle-data.md) | Vehicle telemetry is simulated and labelled as such | Accepted |
| [005](005-provider-abstraction.md) | Every external source sits behind a provider interface | Accepted |
| [006](006-json-events-no-schema-registry.md) | JSON events with a versioned envelope, no schema registry | Accepted |
| [007](007-duckdb-polars-over-spark.md) | DuckDB + Polars + Parquet instead of Spark | Accepted |
| [008](008-uv-workspace-monorepo.md) | uv workspace + pnpm monorepo, no Nx/Turborepo | Accepted |
| [009](009-rejected-technologies.md) | Technologies deliberately not used | Accepted |
