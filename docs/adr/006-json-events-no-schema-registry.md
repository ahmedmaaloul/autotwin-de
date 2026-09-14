# ADR 006 — JSON events with a versioned envelope, no schema registry

**Status:** Accepted · **Date:** 2026-09-14

## Context

The telemetry stream needs a serialisation format and a compatibility strategy. The reflexive
answer in a Kafka architecture is Avro or Protobuf plus a Confluent Schema Registry.

## Decision

Serialise events as **UTF-8 JSON** inside a versioned envelope:

```json
{ "event_id": "...", "event_type": "vehicle.telemetry", "schema_version": 1,
  "occurred_at": "...", "producer": "autotwin-simulator",
  "data_origin": "simulated", "payload": { ... } }
```

The schema lives in `autotwin_contracts.events` as Pydantic models — one definition, used by the
producer to serialise, by the consumer to validate, and by the API to type its responses.
Breaking changes bump the topic name (`vehicle.telemetry.v2`), not a registry entry.

## Alternatives rejected

- **Avro + Schema Registry.** Correct at organisational scale, where independent teams evolve
  producers and consumers on different release cycles. Here there is one producer and one
  consumer in the same repository, sharing the same Pydantic model: the registry would add a
  container, a serialiser dependency and a failure mode to enforce a contract the type checker
  already enforces at build time.
- **Protobuf.** Smaller and faster, but the payloads are ~400 bytes at a few hundred events per
  second. The bottleneck is the database write, not the wire format.
- **MessagePack.** Same reasoning, and it costs the ability to read a topic in Redpanda Console
  during a demo — which has real value here.

## Consequences

- Larger messages and slower parsing than a binary format. Measured and irrelevant at this
  volume; if it ever mattered, the envelope is the seam where a codec would be swapped.
- Compatibility discipline moves into code review and the `schema_version` field. A consumer
  must tolerate unknown fields (Pydantic's default) and must reject an unexpected
  `schema_version` loudly rather than guessing.
