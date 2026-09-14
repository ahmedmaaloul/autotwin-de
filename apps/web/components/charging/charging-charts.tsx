"use client";

import { ChartNoAxesColumn } from "lucide-react";

import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useTranslations } from "@/providers/locale-provider";
import type { ChargingStatistics } from "@/types/domain";

import { CategoryShareChart } from "./charts/category-share-chart";
import { PowerClassChart } from "./charts/power-class-chart";
import { RolloutChart } from "./charts/rollout-chart";
import { StationsByStateChart } from "./charts/stations-by-state-chart";
import { TopOperatorsChart } from "./charts/top-operators-chart";

/**
 * The analysis tab.
 *
 * Five charts, each one a question rather than a metric, ordered the way an infrastructure
 * question is usually asked: where is it, how strong is it, who owns it, when was it built,
 * and how much of it is actually fast.
 *
 * The statistics endpoint aggregates the whole register, so these charts deliberately do *not*
 * follow the filter bar — mixing a filtered map with a nationwide chart on one screen without
 * saying so would be the easiest way to mislead someone on this page. The header says which
 * scope applies.
 */
export function ChargingCharts({
  statistics,
  isLoading,
  error,
  onRetry,
}: {
  statistics: ChargingStatistics | undefined;
  isLoading: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const t = useTranslations();

  if (error) return <ErrorState error={error} onRetry={onRetry} />;

  if (isLoading && !statistics) {
    return (
      <div className="grid gap-4 xl:grid-cols-2">
        <LoadingSkeleton rows={6} />
        <LoadingSkeleton rows={6} />
      </div>
    );
  }

  const isEmpty =
    !statistics ||
    (statistics.by_bundesland.length === 0 &&
      statistics.by_power_class.length === 0 &&
      statistics.by_operator.length === 0 &&
      statistics.growth.length === 0);

  if (isEmpty) {
    return (
      <EmptyState
        icon={ChartNoAxesColumn}
        title={t.charging.noStatisticsTitle}
        description={t.charging.noStatisticsBody}
      />
    );
  }

  return (
    <div className="space-y-4">
      {/* The scope line: these five charts aggregate the whole register, so the reader has to
          see that the filter bar above them does not apply here. */}
      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
        <span>{t.charging.nationwide}</span>
        <SourceBadge origin="official" source="bundesnetzagentur" />
      </div>

      <CategoryShareChart data={statistics.by_power_class} />

      {/* `items-start` so the sixteen-row state chart keeps its own height instead of being
          stretched to match the stacked pair beside it, which would leave dead space inside
          its card. */}
      <div className="grid items-start gap-4 xl:grid-cols-2">
        <StationsByStateChart data={statistics.by_bundesland} />
        <div className="space-y-4">
          <PowerClassChart data={statistics.by_power_class} />
          <RolloutChart data={statistics.growth} />
        </div>
      </div>

      <TopOperatorsChart data={statistics.by_operator} />
    </div>
  );
}
