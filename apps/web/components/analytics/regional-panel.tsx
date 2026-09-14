"use client";

import { useState } from "react";

import { SectionHeader } from "@/components/shared/section-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Card } from "@/components/ui/card";
import { useRegionalAnalytics } from "@/hooks/use-autotwin";
import { useTranslations } from "@/providers/locale-provider";

import { metricLabel, type RegionMetric, type RegionSortKey } from "./region-metrics";
import { RegionalChart } from "./regional-chart";
import { RegionalTable, type SortDirection } from "./regional-table";

/**
 * The federal states compared on one metric at a time.
 *
 * Chart and table share a single sort: the header you click is the metric the chart draws. Two
 * independent controls for the same question would let the panel contradict itself.
 */
export function RegionalPanel() {
  const t = useTranslations();
  const query = useRegionalAnalytics();

  const [sortKey, setSortKey] = useState<RegionSortKey>("stations");
  const [direction, setDirection] = useState<SortDirection>("desc");

  const rows = query.data ?? [];
  const chartMetric: RegionMetric = sortKey === "bundesland" ? "stations" : sortKey;

  const handleSort = (key: RegionSortKey) => {
    if (key === sortKey) {
      setDirection((current) => (current === "desc" ? "asc" : "desc"));
      return;
    }
    setSortKey(key);
    setDirection(key === "bundesland" ? "asc" : "desc");
  };

  if (query.isPending) return <LoadingSkeleton rows={6} />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
  }
  if (rows.length === 0) return <EmptyState />;

  return (
    <div className="grid gap-4 xl:grid-cols-2">
      <RegionalChart rows={rows} metric={chartMetric} />

      <Card className="gap-0 rounded border p-0 shadow-none">
        <div className="border-border border-b px-4 py-3">
          <SectionHeader
            eyebrow={t.analytics.regional}
            title={t.analytics.regionalSubtitle}
            description={`${t.analytics.sortedBy}: ${metricLabel(chartMetric, t)}`}
          />
        </div>
        <RegionalTable
          rows={rows}
          sortKey={sortKey}
          direction={direction}
          onSort={handleSort}
        />
      </Card>
    </div>
  );
}
