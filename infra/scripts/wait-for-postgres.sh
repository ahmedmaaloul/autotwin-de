#!/bin/sh
# AutoTwin DE — block until PostgreSQL accepts queries, or fail with a message that says
# what to do about it.
#
#     ./infra/scripts/wait-for-postgres.sh [timeout_seconds]
#
# Configured through the standard libpq variables so it composes with anything else that
# speaks to the database:
#
#     PGHOST (localhost)  PGPORT (5433)  PGUSER (autotwin)
#     PGPASSWORD (autotwin)  PGDATABASE (autotwin)
#
# Three probes are tried in order of fidelity, because a developer laptop may have none of
# the libpq client tools installed:
#
#   1. pg_isready        — the purpose-built probe
#   2. psql 'SELECT 1'   — proves the database also answers queries, not just connections
#   3. docker compose exec postgres pg_isready — no host-side client needed at all
#
# `pg_isready` reporting "accepting connections" is not quite the same as the database being
# usable: during recovery it answers while still rejecting queries. That is why the psql
# probe is preferred when psql exists, and why `make demo` runs this before Alembic.
set -eu

TIMEOUT="${1:-${WAIT_TIMEOUT:-60}}"
INTERVAL="${WAIT_INTERVAL:-1}"

PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5433}"
PGUSER="${PGUSER:-autotwin}"
PGDATABASE="${PGDATABASE:-autotwin}"
PGPASSWORD="${PGPASSWORD:-autotwin}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-postgres}"
# Resolved so the docker fallback works no matter which directory the caller is in.
REPO_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
export PGHOST PGPORT PGUSER PGDATABASE PGPASSWORD

if command -v psql >/dev/null 2>&1; then
  PROBE="psql"
  PROBE_LABEL="psql SELECT 1"
elif command -v pg_isready >/dev/null 2>&1; then
  PROBE="pg_isready"
  PROBE_LABEL="pg_isready"
elif command -v docker >/dev/null 2>&1; then
  PROBE="docker"
  PROBE_LABEL="docker compose exec ${COMPOSE_SERVICE} pg_isready"
else
  printf 'wait-for-postgres: no psql, no pg_isready and no docker on PATH.\n' >&2
  printf '  Install the PostgreSQL client tools or Docker, then retry.\n' >&2
  exit 1
fi

probe() {
  case "$PROBE" in
  psql)
    psql --quiet --no-psqlrc --tuples-only --command 'SELECT 1' >/dev/null 2>&1
    ;;
  pg_isready)
    pg_isready --quiet --host "$PGHOST" --port "$PGPORT" --username "$PGUSER" \
      --dbname "$PGDATABASE" >/dev/null 2>&1
    ;;
  docker)
    docker compose --project-directory "$REPO_ROOT" exec -T "$COMPOSE_SERVICE" \
      pg_isready --quiet --username "$PGUSER" --dbname "$PGDATABASE" >/dev/null 2>&1
    ;;
  esac
}

printf 'waiting for postgres at %s:%s/%s via %s (timeout %ss) ...\n' \
  "$PGHOST" "$PGPORT" "$PGDATABASE" "$PROBE_LABEL" "$TIMEOUT"

waited=0
while ! probe; do
  if [ "$waited" -ge "$TIMEOUT" ]; then
    printf '\nwait-for-postgres: %s:%s/%s did not become ready within %ss.\n' \
      "$PGHOST" "$PGPORT" "$PGDATABASE" "$TIMEOUT" >&2
    printf '  Is the container up?   docker compose ps postgres\n' >&2
    printf '  What does it say?      docker compose logs --tail=50 postgres\n' >&2
    printf '  Start it:              make up\n' >&2
    exit 1
  fi
  sleep "$INTERVAL"
  waited=$((waited + INTERVAL))
done

printf 'postgres is ready (after %ss)\n' "$waited"
