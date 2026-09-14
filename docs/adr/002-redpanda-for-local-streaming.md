# ADR 002 — Redpanda for Kafka-compatible local streaming

**Status:** Accepted · **Date:** 2026-09-14

## Context

The digital twin produces continuous vehicle telemetry. Writing it straight into Postgres from
the simulator would work, but it would hide the part of the architecture that matters most in a
connected-vehicle context: a durable, partitioned, replayable event log between the vehicles and
everything that consumes them.

The platform must nonetheless start on a laptop with `docker compose up` and no JVM tuning.

## Decision

Use **Redpanda** as the broker, speaking the Kafka protocol.

- Four versioned topics: `vehicle.telemetry.v1`, `vehicle.trip-events.v1`,
  `traffic.events.v1`, `charging.events.v1`, keyed so that a vehicle's events stay ordered.
- A single-binary, single-node `dev-container` mode — no ZooKeeper, no KRaft configuration,
  ~1 GB of memory.
- Redpanda Console on port 8088 so the topics are visible during a demo, which makes the
  architecture legible instead of merely claimed.

Crucially, streaming is **optional**. The simulator writes through a `TelemetrySink`
interface with two implementations: `KafkaTelemetrySink` and `DatabaseTelemetrySink`.
`AUTOTWIN_KAFKA_ENABLED=false` swaps the transport without changing a line of simulation code.

## Alternatives rejected

- **Apache Kafka.** Same protocol, materially heavier locally (JVM heap, KRaft setup).
  Nothing in this project depends on a Kafka-only feature.
- **Redis Streams / NATS.** Lighter still, but they are not the interface a connected-vehicle
  data platform is actually built on, so they would teach the wrong architecture.
- **Direct Postgres writes only.** Simpler, but removes the replay and back-pressure
  properties that justify a streaming layer at all.

## Consequences

- The consumer is at-least-once: offsets are committed only after the batch insert succeeds.
  `telemetry` is append-only and the API reads the latest row per vehicle, so a duplicate row
  after a crash is harmless. Exactly-once was not worth a transactional outbox here.
- Because the transport is swappable, `make demo` works on a machine where the broker failed
  to start — the demo degrades to direct writes and says so in the simulation status endpoint.
