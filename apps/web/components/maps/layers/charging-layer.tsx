"use client";

import type { FeatureCollection } from "geojson";
import { useMemo } from "react";

import { useMapContext } from "../map-canvas";
import { useMapLayer, useMapLayerInteraction, useMapSourceData } from "../use-map-layer";

const SOURCE_ID = "charging-stations";
export const CHARGING_LAYER_IDS = {
  clusters: "charging-clusters",
  clusterCount: "charging-cluster-count",
  points: "charging-points",
  highlight: "charging-highlight",
} as const;

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

/**
 * Charging infrastructure layer.
 *
 * ~90 000 stations cannot be DOM markers, so this is one clustered GeoJSON source. Clusters
 * carry a `sum_fast` accumulator, which lets the cluster circle be coloured by *how much fast
 * charging is inside it* rather than merely how many dots — the question a planner is actually
 * asking when they look at Germany zoomed out.
 *
 * Individual stations are sized by power and coloured by category, with the same ramp the
 * charts use (docs/DESIGN_SYSTEM.md §2).
 */
export function ChargingLayer({
  data,
  visible = true,
  selectedId = null,
  onSelect,
}: {
  data: FeatureCollection | undefined;
  visible?: boolean;
  selectedId?: string | null;
  onSelect?: (stationId: string) => void;
}) {
  const { palette } = useMapContext();

  const source = useMemo(
    () =>
      ({
        type: "geojson" as const,
        data: EMPTY,
        cluster: true,
        clusterRadius: 55,
        // Above zoom 9 the individual sites matter; below it, only density does.
        clusterMaxZoom: 9,
        clusterProperties: {
          sum_fast: ["+", ["case", ["get", "is_fast_charger"], 1, 0]],
          max_power: ["max", ["coalesce", ["get", "max_power_kw"], 0]],
        },
      }) as never,
    [],
  );

  const layers = useMemo(
    () => [
      {
        id: CHARGING_LAYER_IDS.clusters,
        type: "circle" as const,
        source: SOURCE_ID,
        filter: ["has", "point_count"],
        paint: {
          "circle-color": [
            "interpolate",
            ["linear"],
            ["/", ["get", "sum_fast"], ["max", ["get", "point_count"], 1]],
            0,
            palette.muted,
            0.3,
            palette.primary,
            0.6,
            palette.success,
          ],
          "circle-radius": [
            "interpolate",
            ["linear"],
            ["get", "point_count"],
            2,
            12,
            50,
            18,
            500,
            26,
            5000,
            36,
          ],
          "circle-opacity": 0.85,
          "circle-stroke-width": 1,
          "circle-stroke-color": palette.surface,
        },
      },
      {
        id: CHARGING_LAYER_IDS.clusterCount,
        type: "symbol" as const,
        source: SOURCE_ID,
        filter: ["has", "point_count"],
        layout: {
          "text-field": ["number-format", ["get", "point_count"], { "max-fraction-digits": 0 }],
          "text-font": ["Noto Sans Regular"],
          "text-size": 11,
          "text-allow-overlap": true,
        },
        paint: { "text-color": palette.surface },
      },
      {
        id: CHARGING_LAYER_IDS.points,
        type: "circle" as const,
        source: SOURCE_ID,
        filter: ["!", ["has", "point_count"]],
        paint: {
          "circle-color": [
            "match",
            ["get", "charging_category"],
            "ultra_fast",
            palette.success,
            "fast",
            palette.primary,
            palette.muted,
          ],
          "circle-radius": [
            "interpolate",
            ["linear"],
            ["zoom"],
            9,
            3,
            12,
            ["interpolate", ["linear"], ["coalesce", ["get", "max_power_kw"], 11], 11, 4, 350, 9],
            16,
            ["interpolate", ["linear"], ["coalesce", ["get", "max_power_kw"], 11], 11, 6, 350, 14],
          ],
          "circle-stroke-width": 1,
          "circle-stroke-color": palette.surface,
          "circle-opacity": 0.9,
        },
      },
      {
        id: CHARGING_LAYER_IDS.highlight,
        type: "circle" as const,
        source: SOURCE_ID,
        filter: ["==", ["get", "id"], selectedId ?? "__none__"],
        paint: {
          "circle-radius": 12,
          "circle-color": "transparent",
          "circle-stroke-width": 2,
          "circle-stroke-color": palette.foreground,
        },
      },
    ],
    [palette, selectedId],
  );

  useMapLayer(SOURCE_ID, source, layers as never, { enabled: visible });
  useMapSourceData(SOURCE_ID, visible ? (data ?? EMPTY) : EMPTY);

  useMapLayerInteraction([CHARGING_LAYER_IDS.points], {
    onClick: (feature) => {
      const id = feature.properties?.id;
      if (typeof id === "string") onSelect?.(id);
    },
  });

  return null;
}
