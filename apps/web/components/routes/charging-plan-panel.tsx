"use client";

import { BatteryCharging, CircleCheck, TriangleAlert } from "lucide-react";

import { ChargingStopCard } from "@/components/shared/charging-stop-card";
import { MetricCard, MetricRow } from "@/components/shared/metric-card";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Card } from "@/components/ui/card";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { ChargingPlan } from "@/types/domain";

/**
 * The charging answer, in the three shapes it can take.
 *
 * "No stop needed" is a result, not an absence, so it gets a stated, success-toned panel rather
 * than an empty region. An infeasible plan states the optimiser's own reason: a planner who is
 * told "not possible" without being told *why* will simply distrust the tool.
 */
export function ChargingPlanPanel({
  required,
  plan,
  pending,
  error,
  onRetry,
  selectedStationId,
  onSelectStation,
  className,
}: {
  required: boolean;
  plan: ChargingPlan | undefined;
  pending: boolean;
  error: unknown;
  onRetry: () => void;
  selectedStationId: string | null;
  onSelectStation: (stationId: string | null) => void;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  return (
    <section className={cn("space-y-3", className)} aria-label={t.routes.chargingPlanning}>
      <SectionHeader
        eyebrow={t.routes.chargingPlanning}
        title={required ? t.routes.chargingRequired : t.routes.chargingNotRequired}
        description={required ? t.routes.chargingPlanningSubtitle : undefined}
        actions={
          required ? <SourceBadge origin="official" source="bundesnetzagentur" compact /> : null
        }
      />

      {!required ? (
        <Card className="border-success/30 bg-success/5 flex-row items-start gap-3 rounded p-4 shadow-none">
          <CircleCheck className="text-success mt-0.5 size-4 shrink-0" aria-hidden />
          <p className="text-muted-foreground text-sm">{t.routes.chargingNotRequiredBody}</p>
        </Card>
      ) : pending ? (
        <LoadingSkeleton rows={4} />
      ) : error ? (
        <ErrorState error={error} onRetry={onRetry} />
      ) : !plan ? (
        <EmptyState title={t.common.noData} />
      ) : !plan.feasible ? (
        <Card className="border-warning/40 bg-warning/5 flex-row items-start gap-3 rounded p-4 shadow-none">
          <TriangleAlert className="text-warning mt-0.5 size-4 shrink-0" aria-hidden />
          <div className="min-w-0">
            <p className="text-foreground text-sm font-medium">{t.routes.chargingInfeasible}</p>
            {plan.reason ? (
              <p className="text-muted-foreground mt-0.5 text-sm">{plan.reason}</p>
            ) : null}
          </div>
        </Card>
      ) : plan.stops.length === 0 ? (
        <EmptyState title={t.routes.chargingInfeasible} description={plan.reason ?? undefined} />
      ) : (
        <>
          <MetricRow columns={5}>
            <MetricCard
              label={t.routes.totalDuration}
              icon={BatteryCharging}
              value={formatNumber(plan.total_time_min, locale)}
              unit={t.units.minutes}
              footer={
                <span>
                  {t.routes.drivingTime}{" "}
                  <span className="font-mono tabular-nums">
                    {formatNumber(plan.driving_time_min, locale)}
                  </span>{" "}
                  {t.units.minutes}
                </span>
              }
            />
            <MetricCard
              label={t.routes.chargingTime}
              value={formatNumber(plan.charging_time_min, locale)}
              unit={t.units.minutes}
            />
            <MetricCard
              label={t.routes.detourTotal}
              value={formatNumber(plan.detour_km_total, locale, { decimals: 1 })}
              unit={t.units.km}
            />
            <MetricCard
              label={t.routes.arrivalSoc}
              hint={t.routes.arrivalSocTooltip}
              value={formatNumber(plan.arrival_soc_percent, locale)}
              unit={t.units.percent}
            />
            <MetricCard
              label={t.routes.alternativesConsidered}
              value={formatNumber(plan.alternatives_considered, locale)}
              footer={
                <span className="truncate">
                  {t.routes.objective}: {plan.objective}
                </span>
              }
            />
          </MetricRow>

          <ul className="grid gap-3 xl:grid-cols-2">
            {plan.stops.map((stop, index) => (
              <li key={`${stop.station.id}-${stop.offset_km}`} className="min-w-0">
                {/* Selecting a stop is the same selection the map holds, so choosing a card
                    rings the pin and choosing the pin rings the card. */}
                <button
                  type="button"
                  aria-pressed={selectedStationId === stop.station.id}
                  onClick={() =>
                    onSelectStation(
                      selectedStationId === stop.station.id ? null : stop.station.id,
                    )
                  }
                  className={cn(
                    "focus-visible:ring-ring block w-full rounded text-left transition-shadow duration-200 focus-visible:ring-2 focus-visible:outline-none motion-reduce:transition-none",
                    selectedStationId === stop.station.id && "ring-primary ring-2",
                  )}
                >
                  <ChargingStopCard stop={stop} index={index} className="h-full" />
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
