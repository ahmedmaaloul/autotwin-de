"use client";

import { CircleCheck, Route as RouteIcon } from "lucide-react";
import { useState } from "react";

import { MetricCard, MetricRow } from "@/components/shared/metric-card";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { DataModeNotice, EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Card } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { useChargingGeoJson, useCorridorCoverage, useRoutes } from "@/hooks/use-autotwin";
import { DEMO_ROUTE_SLUG } from "@/lib/constants";
import { formatDistanceKm, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { CorridorMap } from "./corridor-map";
import { CoverageGapTable } from "./coverage-gap-table";
import { MethodologyNote } from "./methodology-note";
import { ControlPanel, KmSlider, PowerSelect, RouteSelect } from "./parameter-controls";
import { useCorridorGeometry } from "./use-corridor-geometry";

/**
 * Corridor coverage: how well a single reference route is served, and where it is not.
 *
 * The parameters are the analysis, so they sit above the numbers rather than behind a dialog —
 * and the API's `methodology` sentence sits right under them, because "largest gap 84 km" is a
 * claim until you know it was measured in a 5 km buffer against 150 kW sites.
 */
export function CorridorCoveragePanel() {
  const t = useTranslations();
  const locale = useLocale();

  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [bufferKm, setBufferKm] = useState(5);
  const [minPowerKw, setMinPowerKw] = useState(150);
  const [focusIndex, setFocusIndex] = useState<number | null>(null);

  const routesQuery = useRoutes();
  const routes = routesQuery.data?.items ?? [];

  // Derived during render rather than synced in an effect: the first usable route is a function
  // of the response, not a piece of state of its own.
  const activeSlug =
    selectedSlug ??
    routes.find((route) => route.slug === DEMO_ROUTE_SLUG)?.slug ??
    routes.find((route) => route.slug)?.slug ??
    null;

  const coverageQuery = useCorridorCoverage(
    { route_slug: activeSlug ?? undefined, buffer_km: bufferKm, min_power_kw: minPowerKw },
    Boolean(activeSlug),
  );

  const geometry = useCorridorGeometry(activeSlug, bufferKm);
  const stationsQuery = useChargingGeoJson(
    { bbox: geometry.bbox ?? undefined, min_power_kw: minPowerKw },
    Boolean(geometry.bbox),
  );

  const coverage = coverageQuery.data;
  const gaps = coverage?.gaps ?? [];

  return (
    <div className="space-y-4">
      <ControlPanel
        title={t.analytics.corridorParameters}
        actions={<SourceBadge origin="official" source="bundesnetzagentur" compact />}
      >
        <RouteSelect
          label={t.analytics.selectRoute}
          value={activeSlug}
          onChange={(next) => {
            setSelectedSlug(next);
            setFocusIndex(null);
          }}
          disabled={routes.length === 0}
          options={routes
            .filter((route) => route.slug)
            .map((route) => ({ value: route.slug as string, label: route.name }))}
        />
        <KmSlider
          label={t.analytics.corridorBuffer}
          value={bufferKm}
          onChange={setBufferKm}
          min={1}
          max={25}
          step={0.5}
          decimals={1}
          unit={t.units.km}
        />
        <PowerSelect
          label={t.analytics.minPowerThreshold}
          value={minPowerKw}
          onChange={setMinPowerKw}
        />
      </ControlPanel>

      <DataModeNotice mode={geometry.dataMode} />

      {routesQuery.isError ? (
        <ErrorState error={routesQuery.error} onRetry={() => void routesQuery.refetch()} />
      ) : null}

      {!routesQuery.isPending && routes.length === 0 ? (
        <EmptyState
          icon={RouteIcon}
          title={t.analytics.noRoutes}
          description={t.analytics.noRoutesBody}
        />
      ) : null}

      {coverageQuery.isPending && activeSlug ? <LoadingSkeleton rows={4} /> : null}

      {coverageQuery.isError ? (
        <ErrorState error={coverageQuery.error} onRetry={() => void coverageQuery.refetch()} />
      ) : null}

      {coverage ? (
        <>
          <MetricRow columns={6}>
            <MetricCard
              label={t.analytics.stationsInCorridor}
              value={formatNumber(coverage.stations_in_corridor, locale)}
            />
            <MetricCard
              label={t.analytics.fastStationsInCorridor}
              value={formatNumber(coverage.fast_stations_in_corridor, locale)}
            />
            <MetricCard
              label={t.analytics.stationsPer100km}
              value={formatNumber(coverage.stations_per_100km, locale, { decimals: 1 })}
            />
            <MetricCard
              label={t.analytics.maxGap}
              value={formatDistanceKm(coverage.max_gap_km, locale)}
              unit={t.units.km}
            />
            <MetricCard
              label={t.analytics.meanGap}
              value={formatDistanceKm(coverage.mean_gap_km, locale)}
              unit={t.units.km}
            />
            <MetricCard
              label={t.analytics.coverageScore}
              value={formatNumber(coverage.coverage_score, locale, { decimals: 0 })}
              unit={t.units.percent}
              hint={t.analytics.coverageScoreHint}
              footer={
                <Progress
                  className="mt-1.5"
                  value={Math.min(100, Math.max(0, coverage.coverage_score))}
                />
              }
            />
          </MetricRow>

          <MethodologyNote
            text={coverage.methodology}
            meta={
              <span className="text-muted-foreground font-mono text-[0.6875rem] tabular-nums">
                {formatNumber(coverage.buffer_km, locale, { decimals: 1 })} {t.units.km} ·{" "}
                {formatNumber(coverage.min_power_kw, locale)} {t.units.kw}
              </span>
            }
          />

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
            <CorridorMap
              analysis={geometry.analysis}
              gaps={gaps}
              stations={stationsQuery.data}
              focusIndex={focusIndex}
              geometryPending={geometry.isPending}
            />

            <Card className="gap-0 rounded border p-0 shadow-none">
              <div className="border-border border-b px-4 py-3">
                <SectionHeader
                  eyebrow={t.analytics.coverage}
                  title={t.analytics.gaps}
                  description={t.analytics.gapsSubtitle}
                />
              </div>
              {gaps.length === 0 ? (
                <div className="p-4">
                  <EmptyState
                    icon={CircleCheck}
                    title={t.analytics.noGaps}
                    description={t.analytics.noGapsBody}
                  />
                </div>
              ) : (
                <CoverageGapTable gaps={gaps} focusIndex={focusIndex} onFocus={setFocusIndex} />
              )}
            </Card>
          </div>
        </>
      ) : null}
    </div>
  );
}
