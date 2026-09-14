"use client";

import { useEffect, useMemo } from "react";

import { useRouteAnalysis, useVehicleProfiles } from "@/hooks/use-autotwin";
import type { DataMode } from "@/lib/api/client";
import type { RouteAnalysis } from "@/types/domain";

/** Rough degrees-per-kilometre of latitude; good enough to pad a query bounding box. */
const DEG_PER_KM = 1 / 111;

export interface CorridorGeometry {
  analysis: RouteAnalysis | null;
  dataMode: DataMode | null;
  /** `minLon,minLat,maxLon,maxLat` around the corridor, for filtering the station query. */
  bbox: string | null;
  isPending: boolean;
  isError: boolean;
}

/**
 * The geometry of the selected corridor, for the coverage map.
 *
 * `CorridorCoverage` (BUILD_SPEC §7.2) returns the *gaps* with geometry but not the route line
 * they sit on, and the route list carries no geometry either. `/routes/analyze` is therefore the
 * endpoint that owns the corridor's shape, so this asks it once per selected route and reuses
 * the answer for three things: the coloured route line, the bounding box that keeps the station
 * query to the corridor instead of all 90 000 sites, and the `X-AutoTwin-Data-Mode` header that
 * tells the reader whether the routing provider answered live.
 *
 * It degrades quietly: when this fails, the map still draws the gaps, which are the point.
 */
export function useCorridorGeometry(
  routeSlug: string | null,
  bufferKm: number,
): CorridorGeometry {
  const profiles = useVehicleProfiles();
  const vehicleCode = profiles.data?.[0]?.code ?? null;
  const { mutate, data, isPending, isError } = useRouteAnalysis();

  useEffect(() => {
    if (!routeSlug || !vehicleCode) return;
    mutate({
      route_slug: routeSlug,
      vehicle_code: vehicleCode,
      // A full battery and a conventional reserve: this request exists for the geometry, and
      // these are the defaults the route page starts from, so the backend can serve it warm.
      start_soc_percent: 90,
      min_arrival_soc_percent: 10,
    });
  }, [routeSlug, vehicleCode, mutate]);

  const analysis = data?.data ?? null;

  const bbox = useMemo(() => {
    const coordinates = analysis?.route.geometry.coordinates;
    if (!coordinates || coordinates.length === 0) return null;

    let [west, south] = coordinates[0] as [number, number];
    let [east, north] = coordinates[0] as [number, number];
    for (const [lon, lat] of coordinates as [number, number][]) {
      west = Math.min(west, lon);
      east = Math.max(east, lon);
      south = Math.min(south, lat);
      north = Math.max(north, lat);
    }

    const pad = Math.max(bufferKm, 1) * DEG_PER_KM;
    return [west - pad, south - pad, east + pad, north + pad]
      .map((value) => value.toFixed(4))
      .join(",");
  }, [analysis, bufferKm]);

  return {
    analysis,
    dataMode: data?.dataMode ?? null,
    bbox,
    isPending: isPending || profiles.isPending,
    isError: isError || profiles.isError,
  };
}
