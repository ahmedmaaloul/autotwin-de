"use client";

import { useEffect } from "react";

import { useMapContext } from "@/components/maps/map-canvas";

export interface FocusRequest {
  /** Incremented by the caller on every explicit request, so repeating a target re-triggers. */
  token: number;
  center: [number, number];
  zoom?: number;
}

/**
 * Declarative "fly the map here".
 *
 * It is a child of `<MapCanvas>` and follows the same contract as the layer components: the
 * page passes data, this component talks to MapLibre (docs/DESIGN_SYSTEM.md §8). The page
 * itself never holds the map instance.
 *
 * Deliberately driven by a token rather than by the vehicle's position: a map that re-centres
 * on every telemetry tick cannot be panned by the person using it. Centring happens when it is
 * asked for, once.
 */
export function MapFocus({ request }: { request: FocusRequest | null }) {
  const { map, ready } = useMapContext();
  const token = request?.token ?? null;
  const longitude = request?.center[0] ?? null;
  const latitude = request?.center[1] ?? null;
  const zoom = request?.zoom;

  useEffect(() => {
    if (!map || !ready || token == null || longitude == null || latitude == null) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    map.easeTo({
      center: [longitude, latitude],
      zoom: zoom ?? Math.max(map.getZoom(), 9),
      duration: reduced ? 0 : 600,
    });
    // Position and zoom are read at the moment of the request; only the token re-triggers it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, ready, token]);

  return null;
}
