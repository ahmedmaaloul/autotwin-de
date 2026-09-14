"""Tests for the ingestion adapters (BUILD_SPEC §4, ADR 005).

Everything in here runs with no network and no database. HTTP is mocked with ``respx``; the
payloads handed to the mocks are the *committed fixtures* in
``services/ingestion/src/autotwin_ingestion/fixtures/``, which are real captured bytes — the
UTF-8 BOM of the Bundesnetzagentur CSV, the ISO-8859-1 of the DWD product files, the
``"long"`` coordinate key of the Autobahn API. Feeding a mock anything else would test the
mock rather than the adapter.

Three things are asserted throughout, and they are the reason these tests exist:

* **Derived expectations.** Distances, timestamps and record counts are computed a second,
  obviously-correct way inside the test (a raw ``csv.reader``, a hand-written haversine, a
  published city distance), never copied from what the parser returned on the day it was
  written.
* **The documented traps.** Each adapter's module docstring lists the properties of its source
  that a naive reader gets wrong. Every one of those has a test that fails if the trap is
  stepped on again.
* **Degradation.** ``live → cache → fixture`` is exercised with the upstream simulated down,
  because the mode a provider *reports* is what the UI shows the user (BUILD_SPEC §0.2).
"""
