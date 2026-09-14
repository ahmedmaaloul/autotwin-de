# ADR 005 — Every external source sits behind a provider interface

**Status:** Accepted · **Date:** 2026-09-14

## Context

AutoTwin DE depends on five external systems it does not control: the Bundesnetzagentur
charging registry, DWD open data, Autobahn GmbH traffic data, an OSRM routing service, and a
geocoder. Public open-data endpoints move, rename columns, rate-limit, and go down for
maintenance. A portfolio project that only works on the day it was built is worthless.

## Decision

Every external system is reached through an abstract provider in
`autotwin_core.providers.base`, with concrete adapters in `autotwin_ingestion.providers`.

Two mechanisms make this real rather than decorative:

1. **A typed error hierarchy.** `ProviderUnavailable`, `ProviderTimeout`, `InvalidSourceData`,
   `RateLimited`, `ConfigurationMissing`. The HTTP client translates transport failures into
   these, and the API maps them onto stable error codes and status codes. No provider failure
   ever surfaces as a 500 with a stack trace.

2. **A three-stage fallback chain: live → on-disk cache → bundled fixture.** Every call returns
   a `ProviderResult[T]` carrying `mode: live | cache | fixture`. The mode is propagated to the
   API as the `X-AutoTwin-Data-Mode` header and rendered in the UI as
   *"Live-Quelle nicht verfügbar — zwischengespeicherte Daten"*. The application degrades; it
   does not fail, and it does not lie about which it did.

Fixtures are committed to the repository and are derived from the real source schema, so the
parsers under test are the same parsers used in production.

## Alternatives rejected

- **Calling `httpx` directly from the pipelines.** Faster to write; impossible to test without
  the network, and every outage becomes a 500.
- **Mocking at the HTTP layer only.** Tests would still be coupled to URL shapes and would not
  exercise the fallback logic, which is where the interesting bugs live.

## Consequences

- Tests mock the **interface**, not the network, which keeps them fast and meaningful.
- Swapping OSRM's public demo server for a locally-hosted instance, or the Autobahn API for a
  Mobilithek DATEX II feed, is a configuration change plus one adapter — not a refactor.
- There is a real cost: two layers of indirection between a pipeline and an HTTP call. It is
  paid back the first time a source changes shape.
