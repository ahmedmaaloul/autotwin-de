# ADR 001 — PostGIS is the geospatial engine, not Python

**Status:** Accepted · **Date:** 2026-09-14

## Context

AutoTwin DE answers questions that are geometric at their core: *which chargers lie within
5 km of the A5 corridor*, *what is the longest stretch of this route without a 150 kW charger*,
*how many fast-charging points per 100 km does each Bundesland have*. The naive implementation
stores latitude and longitude as two floats and loops in Python.

With ~90 000 charging stations and a 200 km route cut into ~40 segments, a Python
nearest-neighbour loop is O(n·m) and re-implements — badly — indexing that a spatial database
already does well.

## Decision

Store geography in PostGIS and push spatial work into SQL.

- Point features use `geography(Point, 4326)` so `ST_Distance` and `ST_DWithin` return metres
  without a projection step.
- Route geometry uses `geometry(LineString, 4326)`, cast to `::geography` when a real length
  is needed — cheaper for rendering and simplification.
- Every spatial column has an explicit GIST index, declared in the model rather than left to
  GeoAlchemy2's implicit `spatial_index=True` (which would silently create a duplicate).
- Corridor coverage, gap detection and the underserved-corridor analysis are SQL, using
  `ST_Buffer`, `ST_DWithin`, `ST_LineLocatePoint` and window functions over the resulting
  offsets.

## Alternatives rejected

- **Lat/lon floats + Python (shapely/scipy KD-tree).** Works, but re-implements indexing,
  loses the ability to filter spatially inside a paginated query, and makes the analytics
  impossible to express in dbt.
- **A dedicated spatial engine (GeoMesa, SpatiaLite).** Additional operational surface for no
  benefit at this data volume.
- **Geohash bucketing in a plain table.** Approximate, and awkward once corridors and buffers
  enter the picture.

## Consequences

- The demo requires the `postgis/postgis` image rather than plain `postgres`; the first
  migration runs `CREATE EXTENSION postgis`.
- Alembic autogenerate needs an `include_object` filter so it does not try to drop PostGIS's
  own `spatial_ref_sys` / `geometry_columns` objects. This is handled in `alembic/env.py`.
- Reading a geometry in Python requires a WKB conversion. It is wrapped once in
  `autotwin_core.db.types` so no other module touches WKB.
- The analytical queries are portable to any PostGIS deployment, which is what a real
  infrastructure-planning team would already be running.
