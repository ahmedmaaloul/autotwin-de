import type { RouteAnalyzeRequest } from "@/lib/api/endpoints";

import { CORRIDOR_PRESETS, DEFAULT_CORRIDOR, type CorridorPreset } from "./demo-corridors";

/**
 * The analysis request, and its round trip through the URL.
 *
 * An analysis is a claim about one specific journey, so it has to be quotable:
 * `?route=frankfurt-stuttgart&vehicle=sedan_ev&soc=70&min=15` is the complete input, which means
 * a result can be pasted into a ticket and re-derived exactly. That is also why the page runs
 * the analysis automatically when it is opened with parameters — a shared link that renders an
 * empty form has not shared anything.
 */

export const DEFAULT_START_SOC = 70;
export const DEFAULT_MIN_ARRIVAL_SOC = 15;

/** The sedan profile seeded in migration `0002` (BUILD_SPEC §9) — the middle of the range. */
export const DEFAULT_VEHICLE_CODE = "sedan_ev";

export interface RouteFormState {
  /** Set when a seeded corridor is selected; the API then resolves the stored geometry. */
  routeSlug: string | null;
  origin: string;
  destination: string;
  vehicleCode: string;
  startSoc: number;
  minArrivalSoc: number;
}

export const DEFAULT_FORM_STATE: RouteFormState = {
  routeSlug: DEFAULT_CORRIDOR.slug,
  origin: DEFAULT_CORRIDOR.origin,
  destination: DEFAULT_CORRIDOR.destination,
  vehicleCode: DEFAULT_VEHICLE_CODE,
  startSoc: DEFAULT_START_SOC,
  minArrivalSoc: DEFAULT_MIN_ARRIVAL_SOC,
};

const PRESET_BY_SLUG = new Map<string, CorridorPreset>(
  CORRIDOR_PRESETS.map((preset) => [preset.slug, preset]),
);

export function clampPercent(value: number, min = 0, max = 100): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.round(value)));
}

/** A request needs either a seeded corridor or both endpoints — nothing else is analysable. */
export function isAnalysable(state: RouteFormState): boolean {
  if (state.routeSlug) return true;
  return state.origin.trim().length > 0 && state.destination.trim().length > 0;
}

export function toAnalyzeRequest(
  state: RouteFormState,
  vehicleCode: string = state.vehicleCode,
): RouteAnalyzeRequest {
  const base = {
    vehicle_code: vehicleCode,
    start_soc_percent: state.startSoc,
    min_arrival_soc_percent: state.minArrivalSoc,
  };
  // The slug wins when present: it addresses a stored corridor with known geometry, which is
  // cheaper and more reproducible than geocoding two strings again.
  return state.routeSlug
    ? { ...base, route_slug: state.routeSlug }
    : { ...base, origin: state.origin.trim(), destination: state.destination.trim() };
}

export function toSearchParams(state: RouteFormState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.routeSlug) {
    params.set("route", state.routeSlug);
  } else {
    params.set("origin", state.origin.trim());
    params.set("destination", state.destination.trim());
  }
  params.set("vehicle", state.vehicleCode);
  params.set("soc", String(state.startSoc));
  params.set("min", String(state.minArrivalSoc));
  return params;
}

/** Structural type so both `URLSearchParams` and Next's `ReadonlyURLSearchParams` fit. */
interface ReadableParams {
  get(name: string): string | null;
}

export interface ParsedSearchParams {
  state: RouteFormState;
  /** True when the URL describes a journey rather than being a bare `/routes` visit. */
  hasRequest: boolean;
}

export function fromSearchParams(params: ReadableParams): ParsedSearchParams {
  const slug = params.get("route");
  const origin = params.get("origin");
  const destination = params.get("destination");
  const vehicle = params.get("vehicle");
  const rawSoc = params.get("soc");
  const rawMin = params.get("min");

  const preset = slug ? (PRESET_BY_SLUG.get(slug) ?? null) : null;
  const hasEndpoints = Boolean(origin && destination);

  return {
    state: {
      routeSlug: slug ?? (hasEndpoints ? null : DEFAULT_FORM_STATE.routeSlug),
      origin: origin ?? preset?.origin ?? DEFAULT_FORM_STATE.origin,
      destination: destination ?? preset?.destination ?? DEFAULT_FORM_STATE.destination,
      vehicleCode: vehicle ?? DEFAULT_FORM_STATE.vehicleCode,
      startSoc: parsePercent(rawSoc, DEFAULT_START_SOC, 1, 100),
      minArrivalSoc: parsePercent(rawMin, DEFAULT_MIN_ARRIVAL_SOC, 0, 90),
    },
    hasRequest: Boolean(slug) || hasEndpoints,
  };
}

/** A hand-edited URL is untrusted input; an out-of-range SOC falls back rather than throwing. */
function parsePercent(raw: string | null, fallback: number, min: number, max: number): number {
  if (raw === null || raw.trim() === "") return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) return fallback;
  return clampPercent(value, min, max);
}
