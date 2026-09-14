"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback, useMemo } from "react";

import type { ChargingFilters } from "@/lib/api/endpoints";
import type { ChargingCategory } from "@/types/domain";

/**
 * Filter state for the charging explorer, held entirely in the URL.
 *
 * Nothing about the current view lives in React state: `/charging?fast=true&minPower=150&state=HE`
 * is the whole view, so a colleague can be sent a link to *exactly* the gap someone is arguing
 * about instead of a screenshot plus instructions. Back and forward work, reload works, and
 * the query key that TanStack Query caches on is derived from the same object.
 */

export const CHARGING_TABS = ["map", "table", "charts"] as const;
export type ChargingTab = (typeof CHARGING_TABS)[number];

export const CHARGING_CATEGORIES: readonly ChargingCategory[] = [
  "normal",
  "fast",
  "ultra_fast",
] as const;

/** The slider's domain. 350 kW is the top of what German HPC sites are rated at today. */
export const POWER_MIN_KW = 0;
export const POWER_MAX_KW = 350;
export const POWER_STEP_KW = 10;

/**
 * Used only until `/charging/statistics` reports the real commissioning years — the slider is
 * then bounded by the data rather than by a guess that goes stale.
 */
export const FALLBACK_YEAR_RANGE: readonly [number, number] = [2010, 2026];

export const TABLE_PAGE_SIZE = 25;

export interface ChargingFilterState {
  bundesland: string | null;
  operator: string | null;
  minPowerKw: number;
  fastOnly: boolean;
  category: ChargingCategory | null;
  commissionedFrom: number | null;
  commissionedTo: number | null;
  q: string;
  page: number;
  tab: ChargingTab;
  /** The station whose detail drawer is open — in the URL so a single site is linkable too. */
  stationId: string | null;
}

/** URL parameter names. Short and readable, because people paste these into chat. */
const PARAM = {
  bundesland: "state",
  operator: "operator",
  minPowerKw: "minPower",
  fastOnly: "fast",
  category: "category",
  commissionedFrom: "from",
  commissionedTo: "to",
  q: "q",
  page: "page",
  tab: "tab",
  stationId: "station",
} as const satisfies Record<keyof ChargingFilterState, string>;

/** Changing any of these invalidates the current page number. */
const PAGE_RESETTING_KEYS: readonly (keyof ChargingFilterState)[] = [
  "bundesland",
  "operator",
  "minPowerKw",
  "fastOnly",
  "category",
  "commissionedFrom",
  "commissionedTo",
  "q",
];

function readInt(value: string | null, { min, max }: { min: number; max: number }): number | null {
  if (value == null) return null;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isFinite(parsed) || parsed < min || parsed > max) return null;
  return parsed;
}

function readCategory(value: string | null): ChargingCategory | null {
  return CHARGING_CATEGORIES.find((category) => category === value) ?? null;
}

function readTab(value: string | null): ChargingTab {
  return CHARGING_TABS.find((tab) => tab === value) ?? "map";
}

export function parseChargingFilters(params: URLSearchParams): ChargingFilterState {
  return {
    bundesland: params.get(PARAM.bundesland) || null,
    operator: params.get(PARAM.operator) || null,
    minPowerKw: readInt(params.get(PARAM.minPowerKw), { min: POWER_MIN_KW, max: POWER_MAX_KW }) ?? 0,
    fastOnly: params.get(PARAM.fastOnly) === "true",
    category: readCategory(params.get(PARAM.category)),
    commissionedFrom: readInt(params.get(PARAM.commissionedFrom), { min: 1990, max: 2100 }),
    commissionedTo: readInt(params.get(PARAM.commissionedTo), { min: 1990, max: 2100 }),
    q: params.get(PARAM.q)?.trim() ?? "",
    page: readInt(params.get(PARAM.page), { min: 1, max: 100_000 }) ?? 1,
    tab: readTab(params.get(PARAM.tab)),
    stationId: params.get(PARAM.stationId) || null,
  };
}

/** True when anything narrows the result set — drives the reset action and the chip row. */
export function hasActiveFilters(state: ChargingFilterState): boolean {
  return (
    state.bundesland !== null ||
    state.operator !== null ||
    state.minPowerKw > 0 ||
    state.fastOnly ||
    state.category !== null ||
    state.commissionedFrom !== null ||
    state.commissionedTo !== null ||
    state.q.length > 0
  );
}

/**
 * The server-facing projection. `page`/`page_size` are omitted for the GeoJSON request, which
 * is capped server-side instead of paged — see `toGeoJsonFilters`.
 */
export function toApiFilters(state: ChargingFilterState): ChargingFilters {
  return {
    bundesland: state.bundesland ?? undefined,
    operator: state.operator ?? undefined,
    min_power_kw: state.minPowerKw > 0 ? state.minPowerKw : undefined,
    fast_only: state.fastOnly ? true : undefined,
    category: state.category ?? undefined,
    commissioned_from: state.commissionedFrom ?? undefined,
    commissioned_to: state.commissionedTo ?? undefined,
    q: state.q || undefined,
    page: state.page,
    page_size: TABLE_PAGE_SIZE,
  };
}

export function toGeoJsonFilters(state: ChargingFilterState): ChargingFilters {
  // The GeoJSON endpoint is capped server-side rather than paged, so carrying the table's page
  // number into it would produce a second cache entry per page for identical map data.
  const filters = toApiFilters(state);
  delete filters.page;
  delete filters.page_size;
  return filters;
}

export interface ChargingFilterController {
  filters: ChargingFilterState;
  /** Merge a partial change into the URL. Resets pagination when the result set changes. */
  setFilters: (patch: Partial<ChargingFilterState>) => void;
  /** Clear every data filter; the tab and the open station stay where they are. */
  resetFilters: () => void;
  isFiltered: boolean;
}

export function useChargingFilters(): ChargingFilterController {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const filters = useMemo(
    () => parseChargingFilters(new URLSearchParams(searchParams.toString())),
    [searchParams],
  );

  const write = useCallback(
    (next: ChargingFilterState) => {
      const params = new URLSearchParams();
      const put = (key: string, value: string | null) => {
        if (value) params.set(key, value);
      };

      put(PARAM.bundesland, next.bundesland);
      put(PARAM.operator, next.operator);
      put(PARAM.minPowerKw, next.minPowerKw > 0 ? String(next.minPowerKw) : null);
      put(PARAM.fastOnly, next.fastOnly ? "true" : null);
      put(PARAM.category, next.category);
      put(PARAM.commissionedFrom, next.commissionedFrom ? String(next.commissionedFrom) : null);
      put(PARAM.commissionedTo, next.commissionedTo ? String(next.commissionedTo) : null);
      put(PARAM.q, next.q || null);
      put(PARAM.page, next.page > 1 ? String(next.page) : null);
      put(PARAM.tab, next.tab !== "map" ? next.tab : null);
      put(PARAM.stationId, next.stationId);

      const query = params.toString();
      // `replace` rather than `push`: dragging a slider must not bury the previous page under
      // twenty history entries. The tab and the station drawer are the only navigations a
      // reader would expect the back button to undo, and they arrive through the same path.
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [pathname, router],
  );

  const setFilters = useCallback(
    (patch: Partial<ChargingFilterState>) => {
      const resetsPage = PAGE_RESETTING_KEYS.some((key) => key in patch);
      write({ ...filters, ...patch, ...(resetsPage ? { page: 1 } : {}) });
    },
    [filters, write],
  );

  const resetFilters = useCallback(() => {
    write({
      ...filters,
      bundesland: null,
      operator: null,
      minPowerKw: 0,
      fastOnly: false,
      category: null,
      commissionedFrom: null,
      commissionedTo: null,
      q: "",
      page: 1,
    });
  }, [filters, write]);

  return {
    filters,
    setFilters,
    resetFilters,
    isFiltered: hasActiveFilters(filters),
  };
}
