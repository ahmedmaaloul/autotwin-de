"use client";

import { BatteryCharging, Car, Gauge, RadioTower, TrafficCone, Zap } from "lucide-react";

import { MetricCard, MetricRow } from "@/components/shared/metric-card";
import { SourceBadge } from "@/components/shared/source-badge";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { DashboardSummary } from "@/types/domain";

/**
 * Compact only where compact helps.
 *
 * German grouping already renders 80 432 as `80.432`, which is exactly as readable as a
 * compact form and strictly more precise. Compact notation earns its place above a million,
 * where the digits stop carrying information a reader can hold.
 */
function formatCount(value: number | null | undefined, locale: "de" | "en"): string {
  if (value == null || !Number.isFinite(value)) return "–";
  return formatNumber(value, locale, {
    compact: Math.abs(value) >= 1_000_000,
    decimals: Math.abs(value) >= 1_000_000 ? 1 : 0,
  });
}

/** Minutes up to two hours, then hours — false precision is its own kind of error. */
function ageParts(
  minutes: number | null,
  locale: "de" | "en",
  labels: { minutes: string; hours: string },
): { value: string; unit: string | undefined } {
  if (minutes == null || !Number.isFinite(minutes)) return { value: "–", unit: undefined };
  if (minutes < 120) return { value: formatNumber(minutes, locale), unit: labels.minutes };
  return { value: formatNumber(minutes / 60, locale, { decimals: 1 }), unit: labels.hours };
}

export function OverviewMetrics({
  summary,
  loading,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const freshness = summary?.data_freshness ?? [];
  const operational = freshness.filter(
    (source) => source.status === "healthy" || source.status === "simulation",
  ).length;
  const ages = freshness
    .map((source) => source.age_minutes)
    .filter((age): age is number => age != null && Number.isFinite(age));
  const oldest = ages.length > 0 ? Math.max(...ages) : null;
  const age = ageParts(oldest, locale, { minutes: t.units.minutes, hours: t.units.hours });

  return (
    <MetricRow columns={6}>
      <MetricCard
        label={t.overview.activeVehicles}
        value={formatCount(summary?.vehicles_active, locale)}
        unit={t.units.vehicles}
        icon={Car}
        loading={loading}
        hint={t.overview.simulatedFleetHint}
        footer={
          // The window is part of the number's meaning. Without it the tile appears to
          // contradict the map's vehicle count, which shows the latest position of every
          // vehicle regardless of how long ago it reported.
          <span className="flex flex-col gap-1">
            <SourceBadge origin="simulated" source="simulator" compact />
            <span>{t.overview.activeVehiclesWindow}</span>
          </span>
        }
      />
      <MetricCard
        label={t.overview.chargingStations}
        value={formatCount(summary?.charging_stations_total, locale)}
        unit={t.units.stations}
        icon={Zap}
        loading={loading}
        footer={<SourceBadge origin="official" source="bundesnetzagentur" compact />}
      />
      <MetricCard
        label={t.overview.fastChargingPoints}
        value={formatCount(summary?.fast_charging_points_total, locale)}
        unit={t.charging.chargingPoints}
        icon={BatteryCharging}
        loading={loading}
        footer={<SourceBadge origin="official" source="bundesnetzagentur" compact />}
      />
      <MetricCard
        label={t.overview.trafficEvents}
        value={formatCount(summary?.traffic_events_active, locale)}
        unit={t.units.events}
        icon={TrafficCone}
        loading={loading}
        footer={<SourceBadge origin="official" source="autobahn" compact />}
      />
      <MetricCard
        label={t.overview.avgConsumption}
        value={
          summary?.avg_consumption_kwh_100km == null
            ? "–"
            : formatNumber(summary.avg_consumption_kwh_100km, locale, { decimals: 1 })
        }
        unit={t.units.kwhPer100km}
        icon={Gauge}
        loading={loading}
        hint={t.overview.simulatedFleetHint}
        footer={<SourceBadge origin="simulated" source="simulator" compact />}
      />
      <MetricCard
        label={t.overview.dataFreshness}
        value={age.value}
        unit={age.unit}
        icon={RadioTower}
        loading={loading}
        hint={t.overview.oldestSource}
        footer={
          <span className="inline-flex items-baseline gap-1">
            <span className="font-mono tabular-nums">{formatNumber(operational, locale)}</span>
            <span aria-hidden>/</span>
            <span className="font-mono tabular-nums">{formatNumber(freshness.length, locale)}</span>
            <span>{t.overview.sourcesHealthy}</span>
          </span>
        }
      />
    </MetricRow>
  );
}
