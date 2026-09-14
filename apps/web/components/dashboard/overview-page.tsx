"use client";

import { useMemo } from "react";

import { DataFreshness } from "@/components/shared/data-freshness";
import { PageHeader } from "@/components/shared/section-header";
import { DataModeNotice, ErrorState } from "@/components/shared/states";
import { useDashboardSummary } from "@/hooks/use-autotwin";
import { useTranslations } from "@/providers/locale-provider";
import type { DashboardSummary } from "@/types/domain";

import { ChargingMixCard } from "./charging-mix-card";
import { CorridorTeaser } from "./corridor-teaser";
import { DataSourcesPanel } from "./data-sources-panel";
import { EmptyDatabaseState } from "./empty-database";
import { EnergyTrendCard } from "./energy-trend-card";
import { OverviewMap } from "./overview-map";
import { OverviewMetrics } from "./overview-metrics";
import { TrafficFeed } from "./traffic-feed";
import { WeatherCard } from "./weather-card";

/** The freshest ingestion on record — what the page header's age indicator reports. */
function newestRun(summary: DashboardSummary | null): string | null {
  const timestamps = (summary?.data_freshness ?? [])
    .map((source) => source.last_run_at)
    .filter((value): value is string => Boolean(value))
    .map((value) => new Date(value).getTime())
    .filter((value) => Number.isFinite(value));
  if (timestamps.length === 0) return null;
  return new Date(Math.max(...timestamps)).toISOString();
}

/**
 * Whether the platform has anything to show at all.
 *
 * Three counts at zero together is not a coincidence — it is a database nobody has seeded.
 * Any one of them at zero on its own is a legitimate reading (no disruptions is good news),
 * so the check is deliberately conjunctive.
 */
function isDatabaseEmpty(summary: DashboardSummary | null): boolean {
  if (!summary) return false;
  return (
    summary.charging_stations_total === 0 &&
    summary.vehicles_active === 0 &&
    summary.traffic_events_active === 0
  );
}

/**
 * The Übersicht — the first screen a visitor sees.
 *
 * Reading order is deliberate: six counts that say how big the twin is, then the map that says
 * where things are, then the corridor that says what the platform is for, then the two panels
 * that say what is happening and where the numbers came from. Every panel owns its own
 * loading, empty and error state, so a single failing source degrades one card rather than
 * blanking the dashboard.
 */
export function OverviewPage() {
  const t = useTranslations();
  const query = useDashboardSummary();

  const summary = query.data?.data ?? null;
  const dataMode = query.data?.dataMode ?? null;
  const loading = query.isLoading;
  const latest = useMemo(() => newestRun(summary), [summary]);
  const empty = isDatabaseEmpty(summary);

  return (
    <div className="min-w-0 space-y-6">
      <PageHeader
        title={t.overview.title}
        description={t.overview.subtitle}
        actions={<DataFreshness timestamp={latest} />}
      />

      <DataModeNotice mode={dataMode} />

      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      <OverviewMetrics summary={summary} loading={loading} />

      {empty ? (
        <EmptyDatabaseState />
      ) : query.isError ? null : (
        <>
          <div className="grid min-w-0 gap-4 xl:grid-cols-3">
            <div className="min-w-0 xl:col-span-2">
              <OverviewMap />
            </div>
            <div className="flex min-w-0 flex-col gap-4">
              <WeatherCard summary={summary} loading={loading} />
              <EnergyTrendCard summary={summary} loading={loading} />
              <ChargingMixCard summary={summary} loading={loading} />
            </div>
          </div>

          <CorridorTeaser />

          <div className="grid min-w-0 gap-4 lg:grid-cols-2">
            <TrafficFeed summary={summary} loading={loading} />
            <DataSourcesPanel summary={summary} loading={loading} />
          </div>
        </>
      )}
    </div>
  );
}
