# AutoTwin DE — Design System

> Binding for everything under `apps/web`. Read with [`BUILD_SPEC.md`](BUILD_SPEC.md) §12.

## 1. Design thesis

AutoTwin DE is a **mobility control centre**, not a marketing site. The reader is an engineer
who needs to know, in one glance, how much energy a journey costs and where the infrastructure
is thin. Every pixel either carries a measurement or gets out of the way.

The identity is drawn from the subject's own material: **German road infrastructure**.
Not the flag, not stock-car imagery — the actual standardised vocabulary of the Autobahn:
RAL signal colours, kilometre markers, and the *Streckenband* (route strip diagram) that German
road and rail engineers have used for a century to show what happens along a corridor.

**Signature element: the Streckenband.** A horizontal strip that maps the whole journey onto one
scale — energy intensity as a continuous colour band, charging opportunities as ticks below the
line, traffic disruptions as markers above it, SOC as a line across it. It is the flagship
component on `/routes`, appears compressed on the dashboard, and is the thing a visitor
remembers. Everything else stays quiet so it can be loud.

---

## 2. Colour

The palette is derived from **RAL colours used on German roads**. These are the real standards —
`Verkehrsrot`, `Verkehrsgrün`, `Signalgelb`, `Verkehrsgrau` — which makes the semantics honest
rather than decorative: red means the same thing here as it does on a Baustelle sign.

| token | role | light | dark | origin |
|---|---|---|---|---|
| `--background` | page ground | `#FAFAFA` | `#0C0E12` | Verkehrsgrau A / night |
| `--card` | surface | `#FFFFFF` | `#14171D` | |
| `--foreground` | primary text | `#18181B` | `#E7E9ED` | |
| `--muted-foreground` | secondary text | `#55585F` | `#98A0AC` | |
| `--border` | hairline | `#E3E4E8` | `#262B34` | |
| `--primary` | infrastructure / interactive | `#00548C` | `#4BA3E3` | RAL 5005 Signalblau |
| `--success` | healthy / efficient | `#00754C` | `#2FB47C` | RAL 6024 Verkehrsgrün |
| `--warning` | attention / delayed | `#B57500` | `#EDB13A` | RAL 1003 Signalgelb |
| `--danger` | disruption / critical | `#B8121C` | `#F05F54` | RAL 3020 Verkehrsrot |

**Energy-intensity ramp** (route segments, map, legends) — ordered, readable in both themes:

| level | light | dark | meaning |
|---|---|---|---|
| `low` | `#00754C` | `#2FB47C` | below nominal consumption |
| `medium` | `#8A8F16` | `#B9C24A` | around nominal |
| `high` | `#C97A00` | `#EDA33A` | clearly above nominal |
| `critical` | `#B8121C` | `#F05F54` | far above nominal |

Rules:
- Colour is **never** the only channel. Pair with a label, an icon, or a position.
- No gradients except one: the Streckenband's segment fill, and even there it is a stepped
  ramp, not a smooth blend.
- No glassmorphism. No glow. No neon. No colour-tinted page backgrounds.
- Map surfaces use the same tokens so a chart and the map agree on what "high" looks like.

Implement as OKLCH custom properties in `app/globals.css` under `:root` and `.dark`,
registered into Tailwind v4 via `@theme inline`.

---

## 3. Typography

**IBM Plex** — a rationalist, grid-built family designed for technical documentation. It is
neither the Inter/Geist default nor a decorative serif, and it has the condensed and mono cuts
this product actually needs.

| role | family | usage |
|---|---|---|
| UI / body | **IBM Plex Sans** | everything prose and label |
| data | **IBM Plex Mono** | every number, ID, coordinate, timestamp, kWh, km, % |
| dense headers | **IBM Plex Sans Condensed** (600) | section eyebrows, table headers, metric labels |

Load via `next/font/google` with `display: "swap"` and a real fallback stack
(`ui-sans-serif, system-ui, "Segoe UI", sans-serif` / `ui-monospace, "SF Mono", monospace`).
Expose as `--font-sans`, `--font-mono`, `--font-condensed`.

**Scale** (rem): `0.6875` eyebrow · `0.75` caption · `0.8125` label · `0.875` body ·
`1` lead · `1.125` card title · `1.375` page title · `2` hero metric · `2.75` flagship metric.

Rules:
- Every numeric display uses `font-mono` + `tabular-nums` + `tracking-tight`. Numbers must
  align vertically down a column. This is non-negotiable and is the single most visible signal
  that the dashboard was built by someone who reads data.
- Eyebrows / section labels: Plex Sans Condensed, `uppercase`, `tracking-[0.08em]`,
  `text-muted-foreground`, `text-[0.6875rem]`.
- Units are always set smaller and muted next to their value: `17.8` in foreground,
  `kWh/100 km` in muted at 0.75rem. Never bake the unit into the number string.
- Sentence case for everything a human reads. German is the default voice.

---

## 4. Structure & spacing

- 4px base grid. Card padding `1rem`, section gaps `1.5rem`, page gutter `1.5rem`.
- Radius: `--radius: 0.25rem`. Small. Engineering, not consumer.
- Borders do the work that shadows do elsewhere: `1px solid var(--border)`.
  Exactly one elevation exists (`shadow-sm`) and it is reserved for overlays
  (popover, dropdown, sheet, dialog). Cards never float.
- **Tick motif** — the one structural flourish. Section headers carry a 2px × 12px vertical
  rule in `--primary` before the eyebrow, echoing a kilometre marker. Used only on section
  headers, nowhere else. This is the "remove one accessory" survivor.
- Tables: hairline row separators, no zebra striping, sticky header, right-aligned numerics,
  `h-9` rows. Column headers use the condensed face.
- Dense by default. A metric row shows six values across on a laptop without feeling cramped,
  because the type is small, the numerals are tabular, and there is no decoration between them.

---

## 5. Motion

Deliberate and minimal — this is an instrument panel.
- Data transitions (a number changing, a bar resizing): 200 ms `ease-out`.
- Overlays: Radix defaults, 150 ms.
- Vehicle markers on `/live` interpolate their position between telemetry ticks so movement
  reads as continuous rather than teleporting. **This is the only animation that runs
  continuously**, and it is a data property, not decoration.
- `@media (prefers-reduced-motion: reduce)`: disable marker interpolation (snap instead),
  disable all transitions. Implement once in `globals.css`.

---

## 6. Component vocabulary (`components/shared/`)

Built on shadcn primitives, never instead of them.

| component | purpose |
|---|---|
| `MetricCard` | label + big mono value + unit + optional delta + optional sparkline |
| `MetricRow` | horizontal strip of `MetricCard`s with hairline dividers, no gaps |
| `SectionHeader` | tick motif + eyebrow + title + optional right-slot actions |
| `StatusBadge` | `healthy \| delayed \| degraded \| failed \| simulation` with dot + label |
| `SourceBadge` | `Offizielle Daten · Bundesnetzagentur` / `SIMULIERT` — the honesty component |
| `DataFreshness` | relative age (`vor 18 Min. aktualisiert`) + tooltip with the absolute UTC time |
| `DataModeNotice` | inline alert when `X-AutoTwin-Data-Mode` is `cache`/`fixture` |
| `Streckenband` | **the signature**: route strip diagram (see §7) |
| `EnergyLegend` / `MapLegend` | the shared intensity ramp legend |
| `VehicleTelemetryCard` | live vehicle panel with sparklines |
| `RouteSummary`, `EnergyImpactCard`, `ChargingStopCard` | route-analysis surfaces |
| `ModelMetricCard`, `IngestionRunTable`, `SystemHealthIndicator` | quality/ML surfaces |
| `EmptyState`, `ErrorState`, `LoadingSkeleton` | required on every page |

`SourceBadge` is mandatory anywhere simulated data is displayed. It renders
`SIMULIERT` in `--warning` with a dotted underline and a tooltip explaining what is simulated
and why. Honesty is a design requirement, not a footnote.

---

## 7. The Streckenband

```
 Verkehr      ▲A5 Baustelle          ▲Stau
             ─┬──────────────────────┬───────────────────────────────────
 Energie      ████████▓▓▓▓▓▓░░░░▓▓▓▓▓▓████▓▓▓▓░░░░░░░░░░▓▓▓▓████████████
 SOC          ╲___________________________________________________
             ─┼──────────┼──────────┼──────────┼──────────┼───────┼─────
 Laden        │          ⚡300 kW               ⚡150 kW
 km           0         40         80        120        160     204
```

- Pure SVG, no charting library — it is a bespoke instrument, and Recharts cannot express it.
- Renders from `RouteAnalysis.segments`; width is responsive, height fixed (~132 px).
- Energy band: one rect per segment, filled from the intensity ramp, hairline gaps.
- SOC line: a path across the band, in `--foreground` at 70% opacity.
- Charging stops: ticks below the axis with power labels.
- Traffic events: markers above with `--danger` / `--warning` by severity.
- Fully interactive: hover a segment → tooltip with distance, speed, temperature, traffic,
  predicted consumption; click → selects the segment and flies the map to it.
- Keyboard accessible: the band is a `role="group"` of focusable segments with `aria-label`s
  stating the segment's numbers, so it is usable without a mouse and readable by a screen reader.
- Horizontal-scroll container on narrow viewports; never squashes below legibility.

---

## 8. Maps

- MapLibre GL JS. Keyless vector tiles (see `lib/map/style.ts`); the tile source is one
  configurable constant with a documented swap procedure and a required attribution string
  that is always rendered.
- Two styles, light and dark, both desaturated so data layers carry all the colour.
- All layers are declarative descriptors in `components/maps/layers/*.ts`. A page composes
  layer ids; a page never touches the `maplibregl.Map` instance directly.
- Charging stations cluster below zoom 9. Vehicles are a single GeoJSON source updated in
  place via `source.setData` — never re-created, never one `Marker` per vehicle.
- Every map has a `MapLegend` and a scale control.

---

## 9. Language

German first. Every string goes through `t()`. No user-facing English in a `.tsx` file.
German technical register — the vocabulary German engineers actually use:

| DE | EN |
|---|---|
| Ladeinfrastruktur | Charging infrastructure |
| Schnellladepunkt | Fast charging point |
| Ladevorgang | Charging session |
| Verkehrslage | Traffic conditions |
| Verkehrsstörung | Traffic disruption |
| Baustelle | Roadworks |
| Streckenabschnitt | Route segment |
| Reichweite | Range |
| Energieverbrauch | Energy consumption |
| Ladezustand (SoC) | State of charge |
| Datenquelle / Datenqualität / Datenaktualität | Data source / quality / freshness |

Keep English loanwords German engineers actually use (`Live`, `Dashboard` → *Übersicht*, but
`Streaming`, `Simulation`, `Telemetrie` stay). Do not translate code identifiers.

---

## 10. Quality floor

Non-negotiable on every page: visible keyboard focus (`focus-visible:ring-2 ring-primary`),
semantic landmarks (`<nav> <main> <aside>`), labelled controls, AA contrast in both themes,
`prefers-reduced-motion` respected, no horizontal page scroll at 360 px, and a real
loading / empty / error state.
