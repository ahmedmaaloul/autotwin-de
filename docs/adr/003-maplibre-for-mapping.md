# ADR 003 — MapLibre GL JS with keyless vector tiles

**Status:** Accepted · **Date:** 2026-09-14

## Context

Maps are the primary interface of this product: charging infrastructure, live vehicles, route
energy, underserved corridors. The project must remain runnable with no account, no token and
no credit card.

## Decision

**MapLibre GL JS** (BSD-3, a community fork of Mapbox GL JS v1) with a **keyless vector tile
style**. The tile source is a single configurable constant in `lib/map/style.ts` with a
documented swap procedure, and the provider's attribution is always rendered.

Layer architecture: `components/maps/` owns exactly one `<MapCanvas>` that holds the
`maplibregl.Map` instance. Everything else is a declarative layer descriptor under
`components/maps/layers/`. A page composes layer ids; a page never instantiates a marker.

- Charging stations use GeoJSON clustering below zoom 9 — ~90 000 points cannot be individual
  DOM markers.
- Vehicles are one GeoJSON source updated in place with `source.setData()` on each telemetry
  tick. Marker objects would thrash the DOM at 300 vehicles.

## Alternatives rejected

- **Mapbox GL JS.** Requires a token and has a proprietary licence since v2.
- **Leaflet.** Raster-first; no vector styling, weaker at 10⁵ points, no GPU rendering.
- **deck.gl.** Excellent at scale, but a second rendering stack on top of MapLibre for
  data volumes that MapLibre handles natively.

## Consequences

- Public tile endpoints have usage policies. The app must ship a correct attribution and must
  not be pointed at a community server under load; the swap procedure is documented in the
  README for exactly that reason.
- Two map styles (light/dark), both desaturated, are maintained so the data layers own all the
  colour in the frame.
