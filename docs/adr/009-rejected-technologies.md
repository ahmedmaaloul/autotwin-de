# ADR 009 — Technologies deliberately not used

**Status:** Accepted · **Date:** 2026-09-14

## Context

It is easy to make an architecture diagram impressive by adding boxes. It is harder — and more
useful — to say why each box is absent. This record exists so the absences read as judgement
rather than ignorance.

## Decisions

**Kubernetes.** Docker Compose expresses everything this system needs: five services, one
network, health checks, optional profiles. Kubernetes would add a control plane, manifests and a
local cluster to schedule containers that are already scheduled. *The production path is
documented*: the images are already non-root, configured entirely by environment, and stateless
apart from Postgres — so a Helm chart is a deployment concern, not an architectural one.

**Service mesh.** Three internal callers. mTLS, retries and observability between them are
solved by the same-process function call they currently are.

**Microservices per feature.** The services here are split along the axis that actually
differs — ingestion (scheduled, batch), simulation (continuous, CPU-bound), API (request/
response), ML (offline training). Splitting further would mean distributed transactions across
what is one consistency domain.

**Redis.** The cache has two users: external provider responses (which need to survive a
restart, so they are on disk with a TTL) and routing results (same). Neither benefits from an
in-memory store, and adding one would introduce a second source of truth for freshness.

**Elasticsearch.** Search here means "filter charging stations by operator and Bundesland",
which is a `pg_trgm` index on a table that already exists.

**GraphQL.** One consumer, generated TypeScript types from the OpenAPI schema, no
over-fetching problem to solve. The cost — a resolver layer, N+1 risk, a second auth surface —
buys nothing.

**Event sourcing / CQRS.** The telemetry stream *is* an event log, and the read model is a
table built from it, which captures the useful half. Full event sourcing would mean rebuilding
charging-station state from events that arrive as a CSV snapshot — the wrong shape for the data.

**Apache Flink.** Stream processing here is "validate, batch, insert". That is 200 lines of
Python with a consumer group, not a job manager. See ADR 007 for the same argument about Spark.

**Airflow as a required dependency.** Airflow *is* used — scheduled ingestion is a real
requirement — but behind a Compose profile. Requiring a webserver, a scheduler and a metadata
database to open a dashboard would be a poor trade for a reader who wants to see the product in
two minutes.

## Consequences

Each of these can be revisited, and the seams are in place: the providers are abstracted, the
transport is swappable, the images are cloud-ready. What this project asserts is not that these
technologies are bad — it is that adding them *here* would be unjustified, and that knowing the
difference is the skill being demonstrated.
