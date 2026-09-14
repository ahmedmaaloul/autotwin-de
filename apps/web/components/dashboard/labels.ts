import type { Messages } from "@/lib/i18n/messages/de";
import type { StatusTone } from "@/components/shared/status-badge";
import type {
  ChargingCategory,
  IngestionStatus,
  ProviderMode,
  TrafficEventType,
  TrafficSeverity,
  WeatherCondition,
} from "@/types/domain";

/**
 * Enum → label lookups.
 *
 * The wire format is a snake_case enum; the reader needs a German noun. Keeping the mapping in
 * one module rather than inline in each component is what guarantees that a `severe` traffic
 * event is called the same thing in the list, in the legend and in a tooltip.
 */

export function severityLabel(severity: TrafficSeverity, t: Messages): string {
  return {
    low: t.overview.severityLow,
    moderate: t.overview.severityModerate,
    high: t.overview.severityHigh,
    severe: t.overview.severitySevere,
  }[severity];
}

/**
 * Severity → badge tone.
 *
 * Not a colour-only signal: `StatusBadge` always renders the word beside the dot, so the
 * mapping is a redundancy rather than the message (docs/DESIGN_SYSTEM.md §2).
 */
export function severityTone(severity: TrafficSeverity): StatusTone {
  return {
    low: "neutral",
    moderate: "delayed",
    high: "degraded",
    severe: "failed",
  }[severity] as StatusTone;
}

export function eventTypeLabel(type: TrafficEventType, t: Messages): string {
  return {
    roadworks: t.overview.eventRoadworks,
    closure: t.overview.eventClosure,
    incident: t.overview.eventIncident,
    warning: t.overview.eventWarning,
    congestion: t.overview.eventCongestion,
    other: t.overview.eventOther,
  }[type];
}

export function conditionLabel(condition: WeatherCondition, t: Messages): string {
  return {
    clear: t.overview.conditionClear,
    clouds: t.overview.conditionClouds,
    rain: t.overview.conditionRain,
    snow: t.overview.conditionSnow,
    fog: t.overview.conditionFog,
    storm: t.overview.conditionStorm,
    unknown: t.common.unknown,
  }[condition];
}

export function categoryLabel(category: ChargingCategory, t: Messages): string {
  return {
    normal: t.charging.categoryNormal,
    fast: t.charging.categoryFast,
    ultra_fast: t.charging.categoryUltraFast,
  }[category];
}

/** The charging ramp, in the same order the map layer paints it. */
export const CATEGORY_ORDER: ChargingCategory[] = ["ultra_fast", "fast", "normal"];

export const CATEGORY_COLOR: Record<ChargingCategory, string> = {
  ultra_fast: "var(--success)",
  fast: "var(--primary)",
  normal: "var(--muted-foreground)",
};

export function ingestionTone(status: IngestionStatus): StatusTone {
  return status;
}

export function ingestionLabel(status: IngestionStatus, t: Messages): string {
  return {
    healthy: t.status.healthy,
    delayed: t.status.delayed,
    degraded: t.status.degraded,
    failed: t.status.failed,
    simulation: t.status.simulation,
  }[status];
}

export function providerModeLabel(mode: ProviderMode | null, t: Messages): string {
  if (!mode) return t.common.unknown;
  return t.provenance.dataMode[mode];
}

/** Source keys come off the wire as `bundesnetzagentur`; render them as the reader knows them. */
export function sourceLabel(source: string): string {
  return (
    {
      bundesnetzagentur: "Bundesnetzagentur",
      dwd: "Deutscher Wetterdienst",
      autobahn: "Autobahn GmbH",
      mobilithek: "Mobilithek",
      osm: "OpenStreetMap",
      osrm: "OSRM",
      simulator: "AutoTwin Simulator",
      derived: "AutoTwin",
    }[source] ?? source
  );
}
