"use client";

import { Info, MapPin } from "lucide-react";
import type { FeatureCollection } from "geojson";

import { ChargingLayer } from "@/components/maps/layers/charging-layer";
import { MapCanvas } from "@/components/maps/map-canvas";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { MapLegend } from "./map-legend";

/** The server caps the GeoJSON response at 20 000 features (BUILD_SPEC §7). */
const GEOJSON_CAP = 20_000;

/**
 * The map view of the current filter.
 *
 * The page never touches MapLibre: it composes `<MapCanvas>` with the declarative
 * `<ChargingLayer>` and lets that layer own clustering, paint and hit-testing
 * (docs/DESIGN_SYSTEM.md §8).
 *
 * The one thing this component insists on saying out loud is the server-side cap. When a
 * filter still matches more than 20 000 sites the map is showing a *sample*, and a map that
 * silently drops two thirds of its data is worse than no map at all.
 */
export function ChargingMap({
  data,
  isLoading,
  error,
  selectedId,
  onSelect,
  onRetry,
}: {
  data: FeatureCollection | undefined;
  isLoading: boolean;
  error: unknown;
  selectedId: string | null;
  onSelect: (stationId: string) => void;
  onRetry: () => void;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const featureCount = data?.features.length ?? 0;
  const capped = featureCount >= GEOJSON_CAP;

  if (error) {
    return <ErrorState error={error} onRetry={onRetry} />;
  }

  if (isLoading && !data) {
    return <LoadingSkeleton rows={1} className="[&>*]:h-[32rem]" />;
  }

  if (data && featureCount === 0) {
    return (
      <EmptyState
        icon={MapPin}
        title={t.charging.noStationsTitle}
        description={t.charging.noStationsBody}
        className="h-[32rem]"
      />
    );
  }

  return (
    <div className="space-y-2">
      <div className="border-border relative h-[32rem] overflow-hidden rounded border lg:h-[calc(100vh-24rem)] lg:min-h-[32rem]">
        <MapCanvas ariaLabel={t.charging.mapAria}>
          <ChargingLayer data={data} selectedId={selectedId} onSelect={onSelect} />
        </MapCanvas>
        <MapLegend />
      </div>

      <div className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="inline-flex items-baseline gap-1">
          <span className="text-foreground font-mono tabular-nums">
            {formatNumber(featureCount, locale)}
          </span>
          <span>{t.charging.stations}</span>
        </span>
        {capped ? (
          <span className="text-warning inline-flex items-center gap-1.5">
            <Info className="size-3.5 shrink-0" aria-hidden />
            {t.charging.mapCapped}
          </span>
        ) : null}
      </div>
    </div>
  );
}
