"use client";

import type { Feature, FeatureCollection, Geometry } from "geojson";
import { useMemo } from "react";

import { ChargingLayer } from "@/components/maps/layers/charging-layer";
import { RouteLayer } from "@/components/maps/layers/route-layer";
import { TrafficLayer } from "@/components/maps/layers/traffic-layer";
import { MapCanvas } from "@/components/maps/map-canvas";
import { useTranslations } from "@/providers/locale-provider";
import type { ChargingStop, RouteAnalysis } from "@/types/domain";

/**
 * The map half of the analysis.
 *
 * Loaded through `next/dynamic` by `route-map-panel.tsx`, which is why this file is the only
 * one importing the map layers: MapLibre needs a real WebGL context, so nothing here may be
 * pulled into the server render.
 *
 * The traffic and charging sources are built from the analysis itself rather than fetched
 * again. Both are already in the payload, scoped to this corridor, and re-querying them by
 * bounding box would put events on the map that the strip and the list below do not know about.
 */
export default function RouteMapCanvas({
  analysis,
  stops,
  selectedOrdinal,
  onSelectSegment,
  selectedStationId,
  onSelectStation,
}: {
  analysis: RouteAnalysis;
  stops: ChargingStop[];
  selectedOrdinal: number | null;
  onSelectSegment: (ordinal: number | null) => void;
  selectedStationId: string | null;
  onSelectStation: (stationId: string) => void;
}) {
  const t = useTranslations();

  const traffic = useMemo<FeatureCollection>(
    () => ({
      type: "FeatureCollection",
      features: analysis.traffic_events.map(
        (event): Feature<Geometry> => ({
          type: "Feature",
          id: event.id,
          // A closure is 4 km of the A5, not a pin at kilometre 143 — draw the line when the
          // API gives us one, and fall back to the reported point when it does not.
          geometry: event.geometry ?? {
            type: "Point",
            coordinates: [event.longitude, event.latitude],
          },
          properties: {
            id: event.id,
            severity: event.severity,
            is_blocked: event.is_blocked,
            title: event.title,
          },
        }),
      ),
    }),
    [analysis.traffic_events],
  );

  const charging = useMemo<FeatureCollection>(
    () => ({
      type: "FeatureCollection",
      features: stops.map(
        (stop): Feature<Geometry> => ({
          type: "Feature",
          id: stop.station.id,
          geometry: {
            type: "Point",
            coordinates: [stop.station.longitude, stop.station.latitude],
          },
          properties: {
            id: stop.station.id,
            operator: stop.station.operator,
            max_power_kw: stop.station.max_power_kw,
            charging_category: stop.station.charging_category,
            is_fast_charger: stop.station.is_fast_charger,
            city: stop.station.city,
          },
        }),
      ),
    }),
    [stops],
  );

  return (
    <MapCanvas ariaLabel={t.routes.mapAriaLabel}>
      <RouteLayer
        analysis={analysis}
        selectedOrdinal={selectedOrdinal}
        onSelectSegment={onSelectSegment}
      />
      <TrafficLayer data={traffic} />
      <ChargingLayer data={charging} selectedId={selectedStationId} onSelect={onSelectStation} />
    </MapCanvas>
  );
}
