"use client";

import type { Feature, FeatureCollection, Point } from "geojson";
import { useEffect, useMemo, useRef, useState } from "react";

import type { VehicleLive } from "@/types/domain";

import { useMapContext } from "../map-canvas";
import { useMapLayer, useMapLayerInteraction, useMapSourceData } from "../use-map-layer";

const SOURCE_ID = "vehicles";
export const VEHICLE_LAYER_IDS = {
  halo: "vehicle-halo",
  body: "vehicle-body",
  selected: "vehicle-selected",
} as const;

const EMPTY: FeatureCollection = { type: "FeatureCollection", features: [] };

/**
 * Simulated vehicles.
 *
 * Telemetry arrives roughly once a second, but a marker that jumps once a second reads as
 * broken rather than as movement. Positions are therefore interpolated between the last two
 * readings on an animation frame — the only continuously running animation in the product, and
 * it is a property of the data rather than decoration (docs/DESIGN_SYSTEM.md §5).
 *
 * `prefers-reduced-motion` disables the interpolation and the markers snap instead.
 *
 * Everything is one GeoJSON source updated with `setData`. One `maplibregl.Marker` per vehicle
 * would put 300 DOM nodes into the compositor on every frame.
 */
export function VehicleLayer({
  vehicles,
  visible = true,
  selectedId = null,
  onSelect,
  interpolate = true,
}: {
  vehicles: VehicleLive[];
  visible?: boolean;
  selectedId?: string | null;
  onSelect?: (vehicleId: string) => void;
  interpolate?: boolean;
}) {
  const { palette } = useMapContext();
  const [frame, setFrame] = useState<FeatureCollection | null>(null);

  // previous and target positions per vehicle, plus the timestamp of the last update
  const tracksRef = useRef(
    new Map<string, { from: [number, number]; to: [number, number]; startedAt: number }>(),
  );
  const vehiclesRef = useRef<VehicleLive[]>([]);
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    const now = performance.now();
    const tracks = tracksRef.current;
    for (const vehicle of vehicles) {
      const target: [number, number] = [vehicle.longitude, vehicle.latitude];
      const existing = tracks.get(vehicle.vehicle_id);
      tracks.set(vehicle.vehicle_id, {
        from: existing ? currentPosition(existing, now) : target,
        to: target,
        startedAt: now,
      });
    }
    // Drop vehicles that are no longer reported, so the map does not accumulate ghosts.
    const live = new Set(vehicles.map((v) => v.vehicle_id));
    for (const id of tracks.keys()) if (!live.has(id)) tracks.delete(id);
    vehiclesRef.current = vehicles;
  }, [vehicles]);

  useEffect(() => {
    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const shouldAnimate = interpolate && !reduced;

    const render = () => {
      const now = performance.now();
      const features: Feature<Point>[] = vehiclesRef.current.map((vehicle) => {
        const track = tracksRef.current.get(vehicle.vehicle_id);
        const position: [number, number] = !track
          ? [vehicle.longitude, vehicle.latitude]
          : shouldAnimate
            ? currentPosition(track, now)
            : track.to;
        return {
          type: "Feature",
          geometry: { type: "Point", coordinates: position },
          properties: {
            vehicle_id: vehicle.vehicle_id,
            soc: vehicle.battery_soc_percent,
            speed: vehicle.speed_kmh,
            heading: vehicle.heading_deg ?? 0,
            state: vehicle.state,
          },
        };
      });
      setFrame({ type: "FeatureCollection", features });
      rafRef.current = shouldAnimate ? requestAnimationFrame(render) : null;
    };

    rafRef.current = requestAnimationFrame(render);
    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
  }, [interpolate]);

  const source = useMemo(() => ({ type: "geojson" as const, data: EMPTY }) as never, []);

  const layers = useMemo(
    () => [
      {
        id: VEHICLE_LAYER_IDS.halo,
        type: "circle" as const,
        source: SOURCE_ID,
        paint: {
          "circle-radius": 9,
          "circle-color": palette.primary,
          "circle-opacity": 0.18,
        },
      },
      {
        id: VEHICLE_LAYER_IDS.body,
        type: "circle" as const,
        source: SOURCE_ID,
        paint: {
          // Battery state is the vehicle's most important property, so it is what the marker
          // encodes: green when comfortable, red when the car is in trouble.
          "circle-color": [
            "interpolate",
            ["linear"],
            ["get", "soc"],
            10,
            palette.danger,
            25,
            palette.warning,
            50,
            palette.success,
          ],
          "circle-radius": 4.5,
          "circle-stroke-width": 1.25,
          "circle-stroke-color": palette.surface,
        },
      },
      {
        id: VEHICLE_LAYER_IDS.selected,
        type: "circle" as const,
        source: SOURCE_ID,
        filter: ["==", ["get", "vehicle_id"], selectedId ?? "__none__"],
        paint: {
          "circle-radius": 11,
          "circle-color": "transparent",
          "circle-stroke-width": 2,
          "circle-stroke-color": palette.foreground,
        },
      },
    ],
    [palette, selectedId],
  );

  useMapLayer(SOURCE_ID, source, layers as never, { enabled: visible });
  useMapSourceData(SOURCE_ID, visible ? (frame ?? EMPTY) : EMPTY);

  useMapLayerInteraction([VEHICLE_LAYER_IDS.body, VEHICLE_LAYER_IDS.halo], {
    onClick: (feature) => {
      const id = feature.properties?.vehicle_id;
      if (typeof id === "string") onSelect?.(id);
    },
  });

  return null;
}

/** Ease between the last two reported positions over one telemetry interval. */
function currentPosition(
  track: { from: [number, number]; to: [number, number]; startedAt: number },
  now: number,
): [number, number] {
  const TELEMETRY_INTERVAL_MS = 1000;
  const progress = Math.min(1, (now - track.startedAt) / TELEMETRY_INTERVAL_MS);
  // ease-out: a vehicle arriving at its reported position decelerates into it, which reads as
  // motion rather than as a linear slide.
  const eased = 1 - (1 - progress) ** 2;
  return [
    track.from[0] + (track.to[0] - track.from[0]) * eased,
    track.from[1] + (track.to[1] - track.from[1]) * eased,
  ];
}
