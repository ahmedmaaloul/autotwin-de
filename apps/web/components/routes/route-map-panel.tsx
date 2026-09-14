"use client";

import dynamic from "next/dynamic";

import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { ChargingStop, RouteAnalysis } from "@/types/domain";

import { EnergyLegend } from "./energy-legend";

/**
 * MapLibre needs a browser: it is loaded only on the client, and the skeleton it is swapped for
 * has the map's own height so the page does not jump when the canvas arrives.
 */
const RouteMapCanvas = dynamic(() => import("./route-map-canvas"), {
  ssr: false,
  loading: () => <Skeleton className="size-full rounded-none" />,
});

export function RouteMapPanel({
  analysis,
  stops,
  selectedOrdinal,
  onSelectSegment,
  selectedStationId,
  onSelectStation,
  className,
}: {
  analysis: RouteAnalysis;
  stops: ChargingStop[];
  selectedOrdinal: number | null;
  onSelectSegment: (ordinal: number | null) => void;
  selectedStationId: string | null;
  onSelectStation: (stationId: string) => void;
  className?: string;
}) {
  const t = useTranslations();

  return (
    <Card className={cn("gap-0 overflow-hidden rounded p-0 shadow-none", className)}>
      <div className="border-border border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.routes.mapTitle}
          title={`${analysis.route.origin.name} – ${analysis.route.destination.name}`}
          description={t.routes.mapSubtitle}
          actions={<SourceBadge origin="official" source="osm" compact />}
        />
      </div>

      <div className="h-[22rem] w-full lg:h-[26rem]">
        <RouteMapCanvas
          analysis={analysis}
          stops={stops}
          selectedOrdinal={selectedOrdinal}
          onSelectSegment={onSelectSegment}
          selectedStationId={selectedStationId}
          onSelectStation={onSelectStation}
        />
      </div>

      {/* Only the ramp here: the map's charging pins and traffic lines look nothing like the
          Streckenband's ticks and triangles, so repeating that register key would mislabel them.
          The route line encodes exactly one thing, and this is it. */}
      <div className="border-border border-t px-4 py-2.5">
        <EnergyLegend />
      </div>
    </Card>
  );
}
