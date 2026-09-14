# autotwin-contracts

The shared vocabulary of AutoTwin DE: canonical enums, geospatial value objects, provider
output records, Kafka event envelopes, API transport DTOs and the generic EV profiles.

Pure Pydantic v2 + stdlib. It imports nothing from the rest of the project, and everything
else in the repository imports it — see `docs/BUILD_SPEC.md` §1 for the dependency direction.
