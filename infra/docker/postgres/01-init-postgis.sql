-- AutoTwin DE — PostgreSQL bootstrap.
--
-- Runs once, on an empty data directory, via the postgres image entrypoint.
--
-- NOTE: docker-compose bind-mounts this directory over /docker-entrypoint-initdb.d, which
-- shadows the scripts that ship inside postgis/postgis (they would otherwise create the
-- extensions and a template_postgis database). That is deliberate — we want one file that
-- states exactly which extensions this platform depends on — but it means the CREATE
-- EXTENSION statements below are load-bearing, not decorative.
--
-- Everything here is idempotent so that re-running it (or adding a sibling script later)
-- can never break a developer's volume.

\set ON_ERROR_STOP on

-- Geospatial core. `postgis` gives us geography(Point,4326)/geometry(LineString,4326),
-- ST_Distance in metres and the GiST index types the corridor-coverage queries rely on.
CREATE EXTENSION IF NOT EXISTS postgis;

-- `postgis_topology` is not used by the current schema; it is enabled because enabling it
-- later on a populated database requires a maintenance window, and it is free until used.
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- Trigram index support for the operator/city free-text filter (`?q=` on /charging/stations).
-- ILIKE '%…%' cannot use a B-tree; pg_trgm makes it an index scan.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- uuid-ossp is not needed for row ids (those are application-generated uuid4, see BUILD_SPEC
-- §3) but is required by ad-hoc SQL and dbt seeds that mint identifiers server-side.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- Airflow metadata database.
--
-- Airflow is an optional compose profile, but its metadata database must exist before the
-- profile is first started; creating it here costs nothing and keeps the operator from
-- having to run a manual psql command. CREATE DATABASE cannot appear inside a transaction
-- block or a DO block, so we generate the statement and let psql execute it with \gexec —
-- which also makes the whole thing a no-op when the database already exists.
-- ---------------------------------------------------------------------------
SELECT 'CREATE DATABASE airflow OWNER autotwin'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'airflow')\gexec
