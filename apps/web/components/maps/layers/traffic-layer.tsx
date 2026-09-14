"use client";

import type { FeatureCollection } from "geojson";
import { useMemo } from "react";

import { useMapContext } from "../map-canvas";
import { useMapLayer, useMapLayerInteraction, useMapSourceData } from "../use-map-layer";

const SOURCE_ID = "traffic-events";
export const TRAFFIC_LAYER_IDS = {
  lines: "traffic-lines",
  points: "traffic-points",
} as const;

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

/**
 * Roadworks, closures and warnings from the Autobahn GmbH API.
 *
 * Severity drives colour and a blocked road is drawn thicker — the two facts a driver needs
 * before anything else. Events that carry a LineString are drawn along the affected stretch
 * rather than as a single pin, because "4.9 km of the A1" is different information from
 * "something at kilometre 143".
 */
export function TrafficLayer({
  data,
  visible = true,
  onSelect,
}: {
  data: FeatureCollection | undefined;
  visible?: boolean;
  onSelect?: (eventId: string) => void;
}) {
  const { palette } = useMapContext();

  const source = useMemo(() => ({ type: "geojson" as const, data: EMPTY }) as never, []);

  const severityColor = useMemo(
    () =>
      [
        "match",
        ["get", "severity"],
        "severe",
        palette.danger,
        "high",
        palette.danger,
        "moderate",
        palette.warning,
        palette.muted,
      ] as never,
    [palette],
  );

  const layers = useMemo(
    () => [
      {
        id: TRAFFIC_LAYER_IDS.lines,
        type: "line" as const,
        source: SOURCE_ID,
        filter: ["==", ["geometry-type"], "LineString"],
        layout: { "line-cap": "round" },
        paint: {
          "line-color": severityColor,
          "line-width": ["case", ["get", "is_blocked"], 6, 3.5],
          "line-opacity": 0.8,
          "line-dasharray": ["case", ["get", "is_blocked"], ["literal", [1, 0]], ["literal", [2, 1.5]]],
        },
      },
      {
        id: TRAFFIC_LAYER_IDS.points,
        type: "circle" as const,
        source: SOURCE_ID,
        filter: ["==", ["geometry-type"], "Point"],
        paint: {
          "circle-color": severityColor,
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 3, 12, 7],
          "circle-stroke-width": 1,
          "circle-stroke-color": palette.surface,
        },
      },
    ],
    [palette, severityColor],
  );

  useMapLayer(SOURCE_ID, source, layers as never, { enabled: visible });
  useMapSourceData(SOURCE_ID, visible ? (data ?? EMPTY) : EMPTY);

  useMapLayerInteraction([TRAFFIC_LAYER_IDS.points, TRAFFIC_LAYER_IDS.lines], {
    onClick: (feature) => {
      const id = feature.properties?.id;
      if (typeof id === "string") onSelect?.(id);
    },
  });

  return null;
}
