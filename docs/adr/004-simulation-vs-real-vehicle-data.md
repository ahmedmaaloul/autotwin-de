# ADR 004 — Vehicle telemetry is simulated, and labelled as such everywhere

**Status:** Accepted · **Date:** 2026-09-14

## Context

A digital twin of electric mobility needs connected-vehicle telemetry: position, speed, battery
state of charge, battery temperature, instantaneous power. No such feed is publicly available —
OEM telematics is proprietary and personal-data-bearing.

There are two honest options: build nothing that needs telemetry, or simulate it. There is a
third, dishonest one: simulate it and present it as real. That one is disqualifying in a
portfolio aimed at engineers who work with real fleet data.

## Decision

Simulate vehicle telemetry with a **physically-grounded model**, and make the simulation
visible at every layer:

- The database carries `data_origin` on every record: `official`, `simulated`, or `derived`.
- Kafka envelopes carry `data_origin` in the envelope, not the payload.
- The API never returns a simulated record without the field set.
- The UI renders a `SIMULIERT` badge on every surface backed by simulated data, and the
  `/live` page carries a persistent "SIMULIERTE FAHRZEUGTELEMETRIE" header.
- The ML model registry records `training_data_origin`, and the `/ml` page states plainly that
  the model is trained on simulated labels and is therefore a methodology demonstration, not a
  validated production energy model.

The simulator is not a random-number generator. Speed follows the road class and traffic state;
consumption follows a road-load model (rolling resistance, aerodynamic drag, gradient, inertia,
auxiliary and HVAC load); SOC is the integral of consumed energy; battery temperature responds
to load and ambient. Randomness is bounded noise around deterministic physics, seeded so the
demo is reproducible.

## Alternatives rejected

- **Replaying a public driving-cycle dataset.** Better provenance, but the cycles are
  chassis-dynamometer traces with no German geography, so they cannot drive a map.
- **Pure random walks.** Cheaper, and immediately obvious to anyone who plots consumption
  against speed. It would undermine the credibility of everything else in the project.

## Consequences

- Every metric derived from telemetry — the fleet average consumption on the dashboard, the ML
  training set, the trip histories — is a property of the simulation's assumptions. Those
  assumptions are documented in `docs/ml/energy-model.md` and `docs/data/simulation.md`.
- The energy model can be *validated against its own generator*, which is circular. This is
  stated explicitly wherever metrics are reported, and it is why the physical baseline is always
  shown alongside the ML model: the interesting claim is the methodology, not the R².
