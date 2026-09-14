/**
 * The reference corridors AutoTwin DE seeds.
 *
 * These are **form presets**, not data: a preset only fills the origin/destination fields and
 * the `route_slug` the API is asked about. They exist as a constant so the page still offers a
 * one-click starting point when `GET /routes` is unreachable — a chip that says
 * "Frankfurt am Main → Stuttgart" makes no claim about distance, energy or infrastructure, and
 * nothing is rendered from these values once a real `RouteSummary` arrives.
 *
 * Place names are proper nouns and identical in both catalogues, which is why they are not
 * routed through `t.…`.
 */
export interface CorridorPreset {
  slug: string;
  origin: string;
  destination: string;
}

export const CORRIDOR_PRESETS: readonly CorridorPreset[] = [
  { slug: "frankfurt-stuttgart", origin: "Frankfurt am Main", destination: "Stuttgart" },
  { slug: "frankfurt-muenchen", origin: "Frankfurt am Main", destination: "München" },
  { slug: "stuttgart-muenchen", origin: "Stuttgart", destination: "München" },
  { slug: "muenchen-ingolstadt", origin: "München", destination: "Ingolstadt" },
  { slug: "wolfsburg-berlin", origin: "Wolfsburg", destination: "Berlin" },
] as const;

/** Frankfurt am Main → Stuttgart: the corridor the whole project is demonstrated on. */
export const DEFAULT_CORRIDOR: CorridorPreset = CORRIDOR_PRESETS[0];

export function corridorLabel(origin: string, destination: string): string {
  return `${origin} → ${destination}`;
}
