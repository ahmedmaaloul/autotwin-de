"use client";

import type { FeatureCollection } from "geojson";

import { ChargingLayer } from "@/components/maps/layers/charging-layer";
import { TrafficLayer } from "@/components/maps/layers/traffic-layer";
import { VehicleLayer } from "@/components/maps/layers/vehicle-layer";
import { MapCanvas } from "@/components/maps/map-canvas";
import type { VehicleLive } from "@/types/domain";

import { MapLegend } from "./map-legend";

export interface MapSurfaceProps {
  charging: FeatureCollection | undefined;
  traffic: FeatureCollection | undefined;
  vehicles: VehicleLive[];
  layers: { charging: boolean; traffic: boolean; vehicles: boolean };
  ariaLabel: string;
}

/**
 * The composed Germany map.
 *
 * A page never touches the MapLibre instance (docs/DESIGN_SYSTEM.md §8): this is `<MapCanvas>`
 * plus three declarative layer children, and the layers own every `addSource`/`setData` call.
 * Loaded through `next/dynamic` by `OverviewMap` — MapLibre is the heaviest dependency in the
 * bundle and there is no useful server render of a WebGL canvas.
 */
export function MapSurface({ charging, traffic, vehicles, layers, ariaLabel }: MapSurfaceProps) {
  return (
    <MapCanvas ariaLabel={ariaLabel}>
      <ChargingLayer data={charging} visible={layers.charging} />
      <TrafficLayer data={traffic} visible={layers.traffic} />
      <VehicleLayer vehicles={vehicles} visible={layers.vehicles} />
      <MapLegend
        showCharging={layers.charging}
        showTraffic={layers.traffic}
        showVehicles={layers.vehicles}
      />
    </MapCanvas>
  );
}

export default MapSurface;
