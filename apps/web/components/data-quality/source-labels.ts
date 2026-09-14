import type { IngestionOutcome, IngestionStatus } from "@/types/domain";
import type { StatusTone } from "@/components/shared/status-badge";

/**
 * Publisher names, which are proper nouns and therefore identical in every locale — translating
 * "Bundesnetzagentur" would be wrong, not merely unnecessary. Unknown keys fall through to the
 * raw source identifier rather than to a placeholder, so a new provider shows up as itself.
 */
const SOURCE_TITLES: Record<string, string> = {
  bundesnetzagentur: "Bundesnetzagentur",
  dwd: "Deutscher Wetterdienst",
  autobahn: "Autobahn GmbH",
  mobilithek: "Mobilithek",
  osm: "OpenStreetMap",
  osrm: "OSRM",
  nominatim: "Nominatim",
  simulator: "AutoTwin Simulator",
  derived: "AutoTwin",
};

export function sourceTitle(source: string): string {
  return SOURCE_TITLES[source] ?? source;
}

export function statusTone(status: IngestionStatus): StatusTone {
  return status;
}

export function outcomeTone(outcome: IngestionOutcome): StatusTone {
  switch (outcome) {
    case "success":
      return "healthy";
    case "partial":
      return "delayed";
    case "failed":
      return "failed";
  }
}

/**
 * Acceptance rates arrive either as a fraction or as a percentage depending on the pipeline that
 * wrote them; a progress bar that renders 0.97 % instead of 97 % is worse than no bar at all.
 */
export function toPercent(value: number | null | undefined): number | null {
  if (value == null || !Number.isFinite(value)) return null;
  const percent = value <= 1 ? value * 100 : value;
  return Math.min(100, Math.max(0, percent));
}
