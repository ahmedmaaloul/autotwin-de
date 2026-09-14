import type { Messages } from "@/lib/i18n/messages/de";

/**
 * The four comparable quantities of the regional view.
 *
 * `stations_per_1000km2` is the one that makes the comparison fair: Berlin will always lose an
 * absolute count against Bavaria and win a density comparison, and showing only one of the two
 * would be an argument rather than a measurement.
 */
export const REGION_METRICS = [
  "stations",
  "fast_points",
  "total_kw",
  "stations_per_1000km2",
] as const;

export type RegionMetric = (typeof REGION_METRICS)[number];
export type RegionSortKey = RegionMetric | "bundesland";

export interface RegionRow {
  bundesland: string;
  stations: number;
  fast_points: number;
  total_kw: number;
  stations_per_1000km2: number;
}

export function metricLabel(metric: RegionMetric, t: Messages): string {
  switch (metric) {
    case "stations":
      return t.analytics.stations;
    case "fast_points":
      return t.analytics.fastPoints;
    case "total_kw":
      return t.analytics.totalPower;
    case "stations_per_1000km2":
      return t.analytics.stationsPerArea;
  }
}

export function metricUnit(metric: RegionMetric, t: Messages): string | undefined {
  return metric === "total_kw" ? t.units.kw : undefined;
}

export function metricDecimals(metric: RegionMetric): number {
  return metric === "stations_per_1000km2" ? 1 : 0;
}
