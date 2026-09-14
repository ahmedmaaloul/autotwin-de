# ADR 007 — DuckDB + Polars + Parquet instead of Spark

**Status:** Accepted · **Date:** 2026-09-14

## Context

The project keeps a lake-style raw zone (`data/raw` → `bronze` → `silver` → `gold`) so that
ingestion is reproducible and analytical work does not hammer the operational database. It needs
an engine to do that work.

The largest dataset is the charging registry: on the order of 10⁵ rows and a few tens of
megabytes. Telemetry accumulates faster, but a demo run is single-digit millions of rows.

## Decision

**Polars** for transformation, **DuckDB** for analytical SQL over Parquet, **PyArrow/Parquet**
as the storage format. No distributed compute.

## Alternatives rejected

- **Apache Spark.** Designed for data that does not fit on one machine. Here it would add a JVM,
  a cluster abstraction and minutes of start-up latency to process a file that Polars reads in
  under a second. Choosing it would be résumé-driven design, and an experienced reviewer reads
  it as exactly that.
- **pandas.** Perfectly adequate, but Polars' lazy engine and stricter typing catch schema drift
  at the transformation boundary, which is precisely where ingestion bugs originate.

## Consequences

- The analytical layer runs anywhere Python runs, including CI, with no infrastructure.
- If telemetry volume ever outgrew a laptop, the Parquet layout is already the input format a
  distributed engine would want — the migration path exists without paying for it now.
- Kubernetes and Spark are documented in `docs/adr/009-rejected-technologies.md` as the
  production-scale path, so the judgement is visible rather than assumed.
