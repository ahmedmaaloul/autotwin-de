"use client";

import type { Feature, FeatureCollection, LineString } from "geojson";
import { useEffect, useMemo } from "react";

import { useMapContext } from "@/components/maps/map-canvas";
import { useMapLayer, useMapSourceData } from "@/components/maps/use-map-layer";
import type { CoverageGap } from "@/types/domain";

const SOURCE_ID = "coverage-gaps";

export const COVERAGE_GAP_LAYER_IDS = {
  casing: "coverage-gaps-casing",
  line: "coverage-gaps-line",
  focus: "coverage-gaps-focus",
  label: "coverage-gaps-label",
} as const;

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

export interface CoverageGapFeatureProps {
  index: number;
  label: string;
  gap_km: number;
}

/**
 * The stretches of a corridor where no charging station of the required power lies inside the
 * buffer — drawn on top of the route, in the same red the platform uses for a disruption.
 *
 * Drawing the *absence* is the whole point of this layer. A map of charging stations shows
 * density; a planner has to see the hole, and the hole has no geometry of its own unless the
 * backend computes it (BUILD_SPEC §7.2). Each gap carries its length as a label so the map can
 * be read without the table beside it.
 */
export function CoverageGapLayer({
  gaps,
  formatLabel,
  focusIndex = null,
}: {
  gaps: CoverageGap[];
  /** Locale-aware label for a gap, e.g. `84 km`. Formatting cannot happen inside MapLibre. */
  formatLabel: (gap: CoverageGap) => string;
  /** Index of the gap the user asked to see; the map flies to it. */
  focusIndex?: number | null;
}) {
  const { map, ready, palette } = useMapContext();

  const data = useMemo<FeatureCollection>(() => {
    const features: Feature<LineString, CoverageGapFeatureProps>[] = gaps
      .map((gap, index) => ({ gap, index }))
      .filter((entry) => entry.gap.geometry != null)
      .map(({ gap, index }) => ({
        type: "Feature" as const,
        geometry: gap.geometry as LineString,
        properties: { index, label: formatLabel(gap), gap_km: gap.gap_km },
      }));
    return { type: "FeatureCollection", features };
  }, [gaps, formatLabel]);

  const source = useMemo(() => ({ type: "geojson" as const, data: EMPTY }) as never, []);

  const layers = useMemo(
    () => [
      {
        id: COVERAGE_GAP_LAYER_IDS.casing,
        type: "line" as const,
        source: SOURCE_ID,
        layout: { "line-cap": "butt", "line-join": "round" },
        paint: {
          "line-color": palette.surface,
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 9, 12, 16],
          "line-opacity": 0.75,
        },
      },
      {
        id: COVERAGE_GAP_LAYER_IDS.line,
        type: "line" as const,
        source: SOURCE_ID,
        layout: { "line-cap": "butt", "line-join": "round" },
        paint: {
          "line-color": palette.danger,
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 5, 12, 10],
          // A dash reads as "missing" rather than "present", which is what a gap is.
          "line-dasharray": [1.5, 0.9],
        },
      },
      {
        id: COVERAGE_GAP_LAYER_IDS.focus,
        type: "line" as const,
        source: SOURCE_ID,
        filter: ["==", ["get", "index"], focusIndex ?? -1],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": palette.foreground,
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 12, 12, 22],
          "line-opacity": 0.22,
        },
      },
      {
        id: COVERAGE_GAP_LAYER_IDS.label,
        type: "symbol" as const,
        source: SOURCE_ID,
        layout: {
          "symbol-placement": "line-center",
          "text-field": ["get", "label"],
          "text-font": ["Noto Sans Regular"],
          "text-size": 11,
          "text-offset": [0, -1.1],
        },
        paint: {
          "text-color": palette.danger,
          "text-halo-color": palette.surface,
          "text-halo-width": 1.4,
        },
      },
    ],
    [palette, focusIndex],
  );

  useMapLayer(SOURCE_ID, source, layers as never);
  useMapSourceData(SOURCE_ID, data);

  // Asking to see a gap should move the map to it — the list and the map are one instrument.
  useEffect(() => {
    if (!map || !ready || focusIndex == null) return;
    const geometry = gaps[focusIndex]?.geometry;
    if (!geometry || geometry.coordinates.length === 0) return;

    let [west, south] = geometry.coordinates[0] as [number, number];
    let [east, north] = geometry.coordinates[0] as [number, number];
    for (const [lon, lat] of geometry.coordinates as [number, number][]) {
      west = Math.min(west, lon);
      east = Math.max(east, lon);
      south = Math.min(south, lat);
      north = Math.max(north, lat);
    }
    map.fitBounds(
      [
        [west, south],
        [east, north],
      ],
      { padding: 96, maxZoom: 10, duration: 600 },
    );
  }, [map, ready, focusIndex, gaps]);

  return null;
}
