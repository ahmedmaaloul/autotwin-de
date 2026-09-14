"use client";

import type { Feature, FeatureCollection } from "geojson";
import type { AddLayerObject, MapMouseEvent, SourceSpecification } from "maplibre-gl";
import { useEffect } from "react";

import { useMapContext } from "./map-canvas";

/**
 * Declaratively attach a GeoJSON source and its layers to the map.
 *
 * This hook is the reason pages never touch MapLibre directly. It handles the three things
 * that make hand-written map code fragile:
 *
 *  - **Style reloads.** Switching light/dark calls `setStyle()`, which drops every source and
 *    layer the app added. `styleEpoch` is a dependency here, so layers re-attach themselves.
 *  - **Ordering.** Layers are added `beforeId` a known label layer so data never hides place
 *    names — passed through from the caller.
 *  - **Cleanup.** Layers are removed before their source, which MapLibre requires.
 */
export function useMapLayer(
  sourceId: string,
  source: SourceSpecification,
  layers: AddLayerObject[],
  { beforeId, enabled = true }: { beforeId?: string; enabled?: boolean } = {},
): void {
  const { map, ready, styleEpoch } = useMapContext();

  useEffect(() => {
    if (!map || !ready || !enabled) return;

    if (!map.getSource(sourceId)) {
      map.addSource(sourceId, source);
    }

    for (const layer of layers) {
      if (!map.getLayer(layer.id)) {
        map.addLayer(layer, beforeId && map.getLayer(beforeId) ? beforeId : undefined);
      }
    }

    return () => {
      // The map may already be torn down during unmount; guard every removal.
      if (!map.getStyle()) return;
      for (const layer of layers) {
        if (map.getLayer(layer.id)) map.removeLayer(layer.id);
      }
      if (map.getSource(sourceId)) map.removeSource(sourceId);
    };
    // `source`/`layers` are recreated on each render by callers; depending on them by identity
    // would thrash the map. Callers pass data through `useMapSourceData` instead.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, ready, styleEpoch, sourceId, enabled, beforeId]);
}

/**
 * Push new data into an existing source without re-creating it.
 *
 * This is what keeps 300 moving vehicles smooth: one `setData` call per tick instead of one
 * DOM marker per vehicle (ADR 003).
 */
export function useMapSourceData(
  sourceId: string,
  data: FeatureCollection | Feature | null | undefined,
): void {
  const { map, ready, styleEpoch } = useMapContext();

  useEffect(() => {
    if (!map || !ready || !data) return;
    const source = map.getSource(sourceId);
    if (source && "setData" in source && typeof source.setData === "function") {
      source.setData(data);
    }
  }, [map, ready, styleEpoch, sourceId, data]);
}

/** Register a click/hover handler scoped to specific layers, cleaned up automatically. */
export function useMapLayerInteraction(
  layerIds: string[],
  handlers: {
    onClick?: (feature: Feature, lngLat: [number, number]) => void;
    onHover?: (feature: Feature | null) => void;
  },
): void {
  const { map, ready, styleEpoch } = useMapContext();
  const { onClick, onHover } = handlers;

  useEffect(() => {
    if (!map || !ready) return;
    const present = layerIds.filter((id) => map.getLayer(id));
    if (present.length === 0) return;

    const handleClick = (event: MapMouseEvent) => {
      const features = map.queryRenderedFeatures(event.point, { layers: present });
      if (features[0] && onClick) {
        onClick(features[0] as unknown as Feature, [event.lngLat.lng, event.lngLat.lat]);
      }
    };

    const handleMove = (event: MapMouseEvent) => {
      const features = map.queryRenderedFeatures(event.point, { layers: present });
      map.getCanvas().style.cursor = features.length > 0 ? "pointer" : "";
      onHover?.((features[0] as unknown as Feature) ?? null);
    };

    const handleLeave = () => {
      map.getCanvas().style.cursor = "";
      onHover?.(null);
    };

    if (onClick) map.on("click", handleClick);
    if (onHover) {
      map.on("mousemove", handleMove);
      map.on("mouseout", handleLeave);
    }

    return () => {
      map.off("click", handleClick);
      map.off("mousemove", handleMove);
      map.off("mouseout", handleLeave);
    };
  }, [map, ready, styleEpoch, layerIds, onClick, onHover]);
}
