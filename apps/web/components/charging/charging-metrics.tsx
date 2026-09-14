"use client";

import { Filter, Gauge, MapPin, Zap } from "lucide-react";
import { useMemo } from "react";

import { MetricCard, MetricRow } from "@/components/shared/metric-card";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { ChargingStatistics } from "@/types/domain";

/**
 * The four numbers above the explorer.
 *
 * The first one is the current filter's result count; the other three are the nationwide totals
 * the statistics endpoint reports. Keeping both on screen is the point: a filtered number only
 * means something next to the whole it was cut from, and each card says which of the two it is.
 */
export function ChargingMetrics({
  matches,
  matchesLoading,
  isFiltered,
  statistics,
  statisticsLoading,
}: {
  matches: number | null;
  matchesLoading: boolean;
  isFiltered: boolean;
  statistics: ChargingStatistics | undefined;
  statisticsLoading: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();

  // `null`, not `0`, when the statistics have not arrived: a zero here would read as "Germany
  // has no charging infrastructure" rather than "we could not ask". `formatNumber` renders the
  // dash. That distinction is the whole reason this dashboard exists.
  const totals = useMemo(() => {
    const states = statistics?.by_bundesland;
    if (!states) return { stations: null, fastPoints: null, megawatts: null };
    return {
      stations: states.reduce((sum, entry) => sum + entry.stations, 0),
      fastPoints: states.reduce((sum, entry) => sum + entry.fast_points, 0),
      megawatts: states.reduce((sum, entry) => sum + entry.total_kw, 0) / 1000,
    };
  }, [statistics]);

  return (
    <MetricRow columns={4}>
      <MetricCard
        label={t.charging.matches}
        icon={Filter}
        hint={t.charging.matchesHint}
        loading={matchesLoading && matches === null}
        value={formatNumber(matches, locale)}
        unit={t.units.stations}
        footer={isFiltered ? t.charging.filteredLabel : t.charging.nationwide}
      />
      <MetricCard
        label={t.charging.totalStations}
        icon={MapPin}
        loading={statisticsLoading && !statistics}
        value={formatNumber(totals.stations, locale)}
        unit={t.units.stations}
        footer={t.charging.nationwide}
      />
      <MetricCard
        label={t.charging.fastPoints}
        icon={Zap}
        loading={statisticsLoading && !statistics}
        value={formatNumber(totals.fastPoints, locale)}
        unit={t.charging.chargingPoints}
        footer={t.charging.nationwide}
      />
      <MetricCard
        label={t.charging.installedPower}
        icon={Gauge}
        loading={statisticsLoading && !statistics}
        value={formatNumber(totals.megawatts, locale, { decimals: 0 })}
        unit={t.charging.unitMw}
        footer={t.charging.nationwide}
      />
    </MetricRow>
  );
}
