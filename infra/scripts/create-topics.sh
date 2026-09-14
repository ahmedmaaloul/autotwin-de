#!/bin/sh
# AutoTwin DE — create the Kafka topics defined in BUILD_SPEC §8.
#
# Runs as a one-shot compose service against the in-compose broker, or from the host:
#
#     REDPANDA_BROKERS=localhost:19092 ./infra/scripts/create-topics.sh
#
# Idempotent: an existing topic is reported and skipped, never recreated. Retention is set
# explicitly rather than inherited from the cluster default, because the retention window is
# part of the contract — the telemetry topic is deliberately short-lived, since Postgres is
# the system of record and the log is a transport, not an archive.
set -eu

BROKERS="${REDPANDA_BROKERS:-redpanda:9092}"
PARTITIONS="${TOPIC_PARTITIONS:-3}"
REPLICAS="${TOPIC_REPLICAS:-1}"
WAIT_ATTEMPTS="${WAIT_ATTEMPTS:-60}"
WAIT_INTERVAL="${WAIT_INTERVAL:-2}"

RETENTION_6H=21600000
RETENTION_24H=86400000

log() {
  printf '%s\n' "$*"
}

fail() {
  printf 'create-topics: %s\n' "$*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# Wait for the broker. `rpk cluster info` only succeeds once the Kafka API is actually
# accepting connections, which is what producers need; a plain TCP probe would succeed
# several seconds earlier and make the first run flaky.
# ---------------------------------------------------------------------------
log "waiting for the broker at ${BROKERS} ..."
attempt=0
while ! rpk cluster info --brokers "$BROKERS" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge "$WAIT_ATTEMPTS" ]; then
    fail "broker at ${BROKERS} not reachable after $((WAIT_ATTEMPTS * WAIT_INTERVAL))s"
  fi
  sleep "$WAIT_INTERVAL"
done
log "broker at ${BROKERS} is up"

# create_topic <name> <retention_ms>
create_topic() {
  topic="$1"
  retention_ms="$2"

  if output=$(rpk topic create "$topic" \
    --brokers "$BROKERS" \
    --partitions "$PARTITIONS" \
    --replicas "$REPLICAS" \
    --topic-config "retention.ms=${retention_ms}" \
    --topic-config "cleanup.policy=delete" 2>&1); then
    log "  created  ${topic}  (p=${PARTITIONS} r=${REPLICAS} retention.ms=${retention_ms})"
    return 0
  fi

  # rpk exits non-zero on TOPIC_ALREADY_EXISTS, which is the normal path on every restart.
  if printf '%s' "$output" | grep -qi 'already exists'; then
    log "  exists   ${topic}"
    return 0
  fi

  fail "could not create ${topic}: ${output}"
}

log "creating AutoTwin topics on ${BROKERS}"
create_topic "vehicle.telemetry.v1" "$RETENTION_6H"
create_topic "vehicle.trip-events.v1" "$RETENTION_24H"
create_topic "traffic.events.v1" "$RETENTION_24H"
create_topic "charging.events.v1" "$RETENTION_24H"

log ""
rpk topic list --brokers "$BROKERS"
