import { MAP_ATTRIBUTION } from "@/lib/constants";

/**
 * Basemap configuration.
 *
 * AutoTwin DE must run with no API key and no credit card (ADR 003), so the basemap comes from
 * a keyless community vector-tile provider. **OpenFreeMap** is the default: MIT-licensed
 * project, unmodified OpenMapTiles schema, ODbL data from OpenStreetMap, no request limit and
 * no account.
 *
 * ## Swapping the tile provider
 *
 * Everything provider-specific is in this file. To use a different one, set
 * `NEXT_PUBLIC_MAP_STYLE_LIGHT` / `NEXT_PUBLIC_MAP_STYLE_DARK` to any MapLibre style URL, and
 * `NEXT_PUBLIC_MAP_ATTRIBUTION` to the attribution that provider requires. Candidates that are
 * also keyless: VersaTiles (`https://tiles.versatiles.org/assets/styles/colorful/style.json`)
 * and a self-hosted `tileserver-gl` over a Geofabrik extract, which is what a production
 * deployment should actually do.
 *
 * **The attribution is not optional.** OpenStreetMap data is ODbL; removing the credit is a
 * licence violation, not a design choice. It is rendered by the map control and restyled — not
 * hidden — in `globals.css`.
 */

const DEFAULT_LIGHT_STYLE = "https://tiles.openfreemap.org/styles/positron";
const DEFAULT_DARK_STYLE = "https://tiles.openfreemap.org/styles/dark";

export const MAP_STYLES = {
  light: process.env.NEXT_PUBLIC_MAP_STYLE_LIGHT ?? DEFAULT_LIGHT_STYLE,
  dark: process.env.NEXT_PUBLIC_MAP_STYLE_DARK ?? DEFAULT_DARK_STYLE,
} as const;

export const TILE_ATTRIBUTION = process.env.NEXT_PUBLIC_MAP_ATTRIBUTION ?? MAP_ATTRIBUTION;

export type MapTheme = keyof typeof MAP_STYLES;

export function styleUrlFor(theme: MapTheme): string {
  return MAP_STYLES[theme];
}

/**
 * Colours the map layers read.
 *
 * MapLibre paint properties are evaluated inside a WebGL context that knows nothing about CSS
 * custom properties, so the design tokens have to be resolved to concrete colours here. They
 * are the same values as `globals.css` — the RAL road-signage palette of
 * docs/DESIGN_SYSTEM.md §2 — and the two must be changed together.
 */
export const MAP_COLORS = {
  light: {
    primary: "#00548C",
    success: "#00754C",
    warning: "#B57500",
    danger: "#B8121C",
    foreground: "#18181B",
    muted: "#55585F",
    surface: "#FFFFFF",
    border: "#E3E4E8",
    energyLow: "#00754C",
    energyMedium: "#8A8F16",
    energyHigh: "#C97A00",
    energyCritical: "#B8121C",
    routeCasing: "#FFFFFF",
  },
  dark: {
    primary: "#4BA3E3",
    success: "#2FB47C",
    warning: "#EDB13A",
    danger: "#F05F54",
    foreground: "#E7E9ED",
    muted: "#98A0AC",
    surface: "#14171D",
    border: "#262B34",
    energyLow: "#2FB47C",
    energyMedium: "#B9C24A",
    energyHigh: "#EDA33A",
    energyCritical: "#F05F54",
    routeCasing: "#0C0E12",
  },
} as const;

export type MapPalette = (typeof MAP_COLORS)[MapTheme];

export function paletteFor(theme: MapTheme): MapPalette {
  return MAP_COLORS[theme];
}

/** Ordered intensity ramp, so a map legend and a Recharts axis cannot disagree. */
export function energyRamp(theme: MapTheme): [string, string, string, string] {
  const palette = MAP_COLORS[theme];
  return [
    palette.energyLow,
    palette.energyMedium,
    palette.energyHigh,
    palette.energyCritical,
  ];
}
