"use client";

import type { Feature, FeatureCollection, Geometry } from "geojson";
import dynamic from "next/dynamic";
import { useMemo, useState } from "react";

import { SectionHeader } from "@/components/shared/section-header";
import { EmptyState, ErrorState } from "@/components/shared/states";
import { StatusBadge, type StatusTone } from "@/components/shared/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useChargingGeoJson, useTrafficEvents } from "@/hooks/use-autotwin";
import { useTelemetryStream } from "@/hooks/use-telemetry-stream";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { TrafficEvent } from "@/types/domain";

import { LayerToggles, type LayerState } from "./layer-toggles";
import { WrappingTitle } from "./panel";

const MapSurface = dynamic(() => import("./map-surface").then((module) => module.MapSurface), {
  ssr: false,
  loading: () => <Skeleton className="size-full rounded-none" />,
});

/**
 * Traffic events as map features.
 *
 * The API also serves `/traffic/events/geojson`, but the dashboard already holds the event
 * list for the "Aktuelle Verkehrslage" panel, and a second request for the same rows in a
 * different shape would be a round trip spent on nothing. Events that carry a LineString are
 * drawn along the affected stretch; the rest fall back to their point.
 */
function toTrafficFeatures(events: TrafficEvent[]): FeatureCollection {
  const features: Feature<Geometry>[] = events.map((event) => ({
    type: "Feature",
    id: event.id,
    geometry: event.geometry ?? {
      type: "Point",
      coordinates: [event.longitude, event.latitude],
    },
    properties: {
      id: event.id,
      severity: event.severity,
      event_type: event.event_type,
      is_blocked: event.is_blocked,
      title: event.title,
      road_name: event.road_name,
    },
  }));
  return { type: "FeatureCollection", features };
}

const STREAM_TONE: Record<string, StatusTone> = {
  open: "healthy",
  connecting: "neutral",
  error: "degraded",
  closed: "neutral",
};

export function OverviewMap() {
  const t = useTranslations();
  const locale = useLocale();

  const [layers, setLayers] = useState<LayerState>({
    charging: true,
    traffic: true,
    vehicles: true,
  });
  const [fastOnly, setFastOnly] = useState(false);

  const charging = useChargingGeoJson(fastOnly ? { fast_only: true } : {}, layers.charging);
  const traffic = useTrafficEvents({ active_only: true, page_size: 250 });
  const stream = useTelemetryStream({ enabled: layers.vehicles });

  const trafficEvents = useMemo(
    () => traffic.data?.data.items ?? [],
    [traffic.data],
  );
  const trafficFeatures = useMemo(() => toTrafficFeatures(trafficEvents), [trafficEvents]);

  const stationCount = charging.data?.features.length ?? 0;
  const isLoading = charging.isLoading || traffic.isLoading;
  const failure = charging.error ?? traffic.error;
  const isBlank =
    !isLoading &&
    failure == null &&
    stationCount === 0 &&
    trafficEvents.length === 0 &&
    stream.vehicles.length === 0;

  const streamLabel =
    stream.status === "open"
      ? t.live.connected
      : stream.status === "connecting"
        ? t.live.reconnecting
        : stream.status === "error"
          ? t.live.reconnecting
          : t.live.disconnected;

  return (
    <section className="flex min-w-0 flex-col gap-3" aria-label={t.overview.mapTitle}>
      <SectionHeader
        eyebrow={t.overview.mapTitle}
        title={<WrappingTitle>{t.overview.mapSubtitle}</WrappingTitle>}
        actions={
          layers.vehicles ? (
            <StatusBadge
              status={STREAM_TONE[stream.status] ?? "neutral"}
              label={streamLabel}
              pulse={stream.status === "open"}
            />
          ) : null
        }
      />

      <LayerToggles
        layers={layers}
        onLayersChange={setLayers}
        fastOnly={fastOnly}
        onFastOnlyChange={setFastOnly}
        status={
          isLoading ? (
            <Skeleton className="h-4 w-40" />
          ) : (
            <p className="text-muted-foreground truncate text-xs">
              <span className="font-mono tabular-nums">{formatNumber(stationCount, locale)}</span>{" "}
              {t.units.stations}
              <span className="mx-1.5 opacity-40" aria-hidden>
                ·
              </span>
              <span className="font-mono tabular-nums">
                {formatNumber(trafficEvents.length, locale)}
              </span>{" "}
              {t.units.events}
              <span className="mx-1.5 opacity-40" aria-hidden>
                ·
              </span>
              <span className="font-mono tabular-nums">
                {formatNumber(stream.vehicles.length, locale)}
              </span>{" "}
              {t.units.vehicles}
            </p>
          )
        }
      />

      {failure ? (
        <ErrorState
          error={failure}
          onRetry={() => {
            void charging.refetch();
            void traffic.refetch();
          }}
        />
      ) : null}

      <div className="border-border relative h-[420px] min-w-0 overflow-hidden rounded border sm:h-[520px] xl:h-[560px]">
        <MapSurface
          charging={charging.data}
          traffic={trafficFeatures}
          vehicles={layers.vehicles ? stream.vehicles : []}
          layers={layers}
          ariaLabel={t.overview.mapAriaLabel}
        />
        {isBlank ? (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center p-4">
            <EmptyState
              className="bg-card/95 pointer-events-auto max-w-sm backdrop-blur"
              title={t.states.emptyTitle}
              description={t.states.emptyBody}
            />
          </div>
        ) : null}
      </div>
    </section>
  );
}
