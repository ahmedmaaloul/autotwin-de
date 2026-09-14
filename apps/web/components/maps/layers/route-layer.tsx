"use client";

import type { Feature, FeatureCollection, LineString } from "geojson";
import { useEffect, useMemo } from "react";

import type { RouteAnalysis } from "@/types/domain";

import { useMapContext } from "../map-canvas";
import { useMapLayer, useMapLayerInteraction, useMapSourceData } from "../use-map-layer";

const SOURCE_ID = "route";
export const ROUTE_LAYER_IDS = {
  casing: "route-casing",
  energy: "route-energy",
  selected: "route-selected-segment",
} as const;

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

/**
 * The analysed route, drawn segment by segment and coloured by predicted energy intensity.
 *
 * This is the map half of the Streckenband: the same four-step ramp, the same segment
 * ordinals, so hovering a bar in the strip and hovering a stretch of Autobahn highlight each
 * other. A single line coloured by average consumption would hide exactly the thing the
 * analysis exists to show — that the A5 climb out of Frankfurt costs far more than the flat run
 * into Stuttgart.
 */
export function RouteLayer({
  analysis,
  selectedOrdinal = null,
  onSelectSegment,
  fitOnLoad = true,
}: {
  analysis: RouteAnalysis | null | undefined;
  selectedOrdinal?: number | null;
  onSelectSegment?: (ordinal: number | null) => void;
  fitOnLoad?: boolean;
}) {
  const { map, ready, palette } = useMapContext();

  const data = useMemo<FeatureCollection>(() => {
    if (!analysis) return EMPTY;

    const features: Feature<LineString>[] = analysis.segments
      .filter((segment) => segment.geometry)
      .map((segment) => ({
        type: "Feature",
        geometry: segment.geometry as LineString,
        properties: {
          ordinal: segment.ordinal,
          intensity: segment.energy_intensity,
          kwh_per_100km: segment.kwh_per_100km,
          soc: segment.soc_at_end_percent,
        },
      }));

    // When per-segment geometry is absent, fall back to the whole-route line so the corridor is
    // still visible — degraded, but never blank.
    if (features.length === 0 && analysis.route.geometry) {
      features.push({
        type: "Feature",
        geometry: analysis.route.geometry,
        properties: { ordinal: -1, intensity: "medium" },
      });
    }

    return { type: "FeatureCollection", features };
  }, [analysis]);

  const source = useMemo(() => ({ type: "geojson" as const, data: EMPTY }) as never, []);

  const layers = useMemo(
    () => [
      {
        id: ROUTE_LAYER_IDS.casing,
        type: "line" as const,
        source: SOURCE_ID,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": palette.routeCasing,
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 6, 12, 12],
          "line-opacity": 0.9,
        },
      },
      {
        id: ROUTE_LAYER_IDS.energy,
        type: "line" as const,
        source: SOURCE_ID,
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": [
            "match",
            ["get", "intensity"],
            "low",
            palette.energyLow,
            "medium",
            palette.energyMedium,
            "high",
            palette.energyHigh,
            "critical",
            palette.energyCritical,
            palette.primary,
          ],
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 3.5, 12, 8],
        },
      },
      {
        id: ROUTE_LAYER_IDS.selected,
        type: "line" as const,
        source: SOURCE_ID,
        filter: ["==", ["get", "ordinal"], selectedOrdinal ?? -999],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": palette.foreground,
          "line-width": ["interpolate", ["linear"], ["zoom"], 5, 6, 12, 13],
          "line-opacity": 0.35,
        },
      },
    ],
    [palette, selectedOrdinal],
  );

  useMapLayer(SOURCE_ID, source, layers as never);
  useMapSourceData(SOURCE_ID, data);

  useMapLayerInteraction([ROUTE_LAYER_IDS.energy], {
    onClick: (feature) => {
      const ordinal = feature.properties?.ordinal;
      if (typeof ordinal === "number" && ordinal >= 0) onSelectSegment?.(ordinal);
    },
  });

  // Frame the corridor once, when a new analysis arrives.
  useEffect(() => {
    if (!map || !ready || !fitOnLoad || !analysis?.route.geometry) return;
    const coordinates = analysis.route.geometry.coordinates;
    if (coordinates.length === 0) return;

    let [west, south] = coordinates[0] as [number, number];
    let [east, north] = coordinates[0] as [number, number];
    for (const [lon, lat] of coordinates as [number, number][]) {
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
      { padding: 64, duration: 600 },
    );
  }, [map, ready, fitOnLoad, analysis]);

  return null;
}
