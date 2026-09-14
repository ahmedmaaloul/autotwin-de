"use client";

import { X } from "lucide-react";

import { EnergyImpactCard } from "@/components/shared/energy-impact-card";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { DataModeNotice } from "@/components/shared/states";
import { Streckenband } from "@/components/shared/streckenband";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import type { DataMode } from "@/lib/api/client";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { ChargingPlan, RouteAnalysis } from "@/types/domain";

import { AnalysisMeta } from "./analysis-meta";
import { ChargingPlanPanel } from "./charging-plan-panel";
import { EnergyLegend } from "./energy-legend";
import { RouteMapPanel } from "./route-map-panel";
import { RouteSummary } from "./route-summary";
import { SegmentTable } from "./segment-table";
import { TrafficEventList } from "./traffic-event-list";

export interface RouteAnalysisResultProps {
  analysis: RouteAnalysis;
  dataMode: DataMode | null;
  plan: ChargingPlan | undefined;
  planPending: boolean;
  planError: unknown;
  onRetryPlan: () => void;
  selectedOrdinal: number | null;
  onSelectSegment: (ordinal: number | null) => void;
  selectedStationId: string | null;
  onSelectStation: (stationId: string | null) => void;
}

/**
 * The whole answer, in reading order.
 *
 * Provenance, then the six numbers, then the Streckenband, then the map beside the attribution
 * of *why* it costs what it does, then charging, then the auditable detail. A reader who stops
 * after the first screen still has the result; a reader who keeps going can check it.
 *
 * `selectedOrdinal` lives one level up in the page, which is what lets the strip, the map and
 * the table be three views of the same selection rather than three widgets.
 */
export function RouteAnalysisResult({
  analysis,
  dataMode,
  plan,
  planPending,
  planError,
  onRetryPlan,
  selectedOrdinal,
  onSelectSegment,
  selectedStationId,
  onSelectStation,
}: RouteAnalysisResultProps) {
  const t = useTranslations();
  const locale = useLocale();

  const stops = plan?.feasible ? plan.stops : [];
  const selectedSegment =
    selectedOrdinal == null
      ? null
      : (analysis.segments.find((segment) => segment.ordinal === selectedOrdinal) ?? null);

  return (
    <div className="space-y-4">
      <DataModeNotice mode={dataMode} />
      <AnalysisMeta analysis={analysis} />
      <RouteSummary analysis={analysis} />

      {/* The signature component gets the full width of the page. */}
      <Card className="gap-0 rounded p-0 shadow-none">
        <div className="border-border flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3">
          <SectionHeader
            eyebrow={t.routes.strip}
            title={`${analysis.route.origin.name} – ${analysis.route.destination.name}`}
            description={t.routes.stripSubtitle}
          />
          <div className="flex shrink-0 items-center gap-2">
            <SourceBadge origin="simulated" compact />
            {selectedSegment ? (
              <Button type="button" variant="ghost" size="sm" onClick={() => onSelectSegment(null)}>
                <X aria-hidden />
                {t.routes.clearSelection}
              </Button>
            ) : null}
          </div>
        </div>

        <div className="px-4 py-4">
          <Streckenband
            analysis={analysis}
            stops={stops}
            selectedOrdinal={selectedOrdinal}
            onSelectSegment={onSelectSegment}
          />
        </div>

        <div className="border-border border-t px-4 py-2.5">
          <EnergyLegend showRegisters />
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <RouteMapPanel
          className="lg:col-span-2"
          analysis={analysis}
          stops={stops}
          selectedOrdinal={selectedOrdinal}
          onSelectSegment={onSelectSegment}
          selectedStationId={selectedStationId}
          onSelectStation={onSelectStation}
        />
        <EnergyImpactCard
          headline={analysis.explanation.headline}
          drivers={analysis.explanation.drivers}
        />
      </div>

      <ChargingPlanPanel
        required={analysis.charging_required}
        plan={plan}
        pending={planPending}
        error={planError}
        onRetry={onRetryPlan}
        selectedStationId={selectedStationId}
        onSelectStation={onSelectStation}
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="gap-0 overflow-hidden rounded p-0 shadow-none lg:col-span-2">
          <div className="border-border border-b px-4 py-3">
            <SectionHeader
              eyebrow={t.routes.segments}
              title={`${formatNumber(analysis.segments.length, locale)} ${t.routes.segments}`}
              description={t.routes.segmentTableSubtitle}
              actions={<SourceBadge origin="simulated" compact />}
            />
          </div>
          <SegmentTable
            segments={analysis.segments}
            selectedOrdinal={selectedOrdinal}
            onSelectSegment={onSelectSegment}
          />
        </Card>

        <TrafficEventList events={analysis.traffic_events} />
      </div>
    </div>
  );
}
