import type { Messages } from "@/lib/i18n/messages/de";
import type {
  EnergyIntensity,
  RoadClass,
  TrafficEventType,
  TrafficSeverity,
} from "@/types/domain";

/**
 * Enum → catalogue lookups.
 *
 * The API speaks the canonical enums of BUILD_SPEC §2 (`motorway`, `severe`, `ultra_fast`).
 * Those values are identifiers, never display strings, so every one of them is translated here
 * rather than being rendered raw anywhere in the page.
 */

export function roadClassLabel(value: RoadClass, t: Messages): string {
  return t.routes.roadClasses[value] ?? t.common.unknown;
}

export function trafficSeverityLabel(value: TrafficSeverity | null, t: Messages): string | null {
  return value ? (t.routes.trafficSeverities[value] ?? t.common.unknown) : null;
}

export function eventTypeLabel(value: TrafficEventType, t: Messages): string {
  return t.routes.eventTypes[value] ?? t.routes.eventTypes.other;
}

export function intensityLabel(value: EnergyIntensity, t: Messages): string {
  return {
    low: t.routes.intensityLow,
    medium: t.routes.intensityMedium,
    high: t.routes.intensityHigh,
    critical: t.routes.intensityCritical,
  }[value];
}

export function vehicleClassLabel(vehicleClass: string, t: Messages): string | null {
  const labels: Record<string, string> = {
    compact: t.routes.vehicleClasses.compact,
    sedan: t.routes.vehicleClasses.sedan,
    performance: t.routes.vehicleClasses.performance,
    suv: t.routes.vehicleClasses.suv,
    van: t.routes.vehicleClasses.van,
  };
  return labels[vehicleClass] ?? null;
}

/** Severity drives the tone of a badge, never the badge's only distinguishing feature. */
export function severityTone(
  severity: TrafficSeverity,
): "failed" | "delayed" | "degraded" | "neutral" {
  if (severity === "severe") return "failed";
  if (severity === "high") return "degraded";
  if (severity === "moderate") return "delayed";
  return "neutral";
}
