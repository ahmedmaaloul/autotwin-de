"use client";

import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState } from "@/components/shared/states";
import { Skeleton } from "@/components/ui/skeleton";
import { useMounted } from "@/hooks/use-mounted";
import type { Locale } from "@/lib/i18n/config";
import { formatDateTime, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { DashboardSummary } from "@/types/domain";

import { Figure, Panel, PanelBody, PanelHeader, WrappingTitle } from "./panel";

const AXIS_STYLE = { fontSize: 10, fill: "var(--muted-foreground)" } as const;

interface TrendPoint {
  label: string;
  full: string;
  value: number;
}

/**
 * Bucket labels adapt to the span of the series: a 12-hour window is read by the hour, a
 * two-week one by the day. Labelling a fortnight in hours would be noise, and labelling twelve
 * hours by date would collapse every tick onto the same string.
 */
function toPoints(
  trend: DashboardSummary["energy_trend"],
  locale: Locale,
): TrendPoint[] {
  const timestamps = trend.map((entry) => new Date(entry.bucket).getTime());
  const valid = timestamps.filter((value) => Number.isFinite(value));
  const spanHours =
    valid.length > 1 ? (Math.max(...valid) - Math.min(...valid)) / 3_600_000 : 0;
  const byDay = spanHours > 48;

  return trend.map((entry, index) => {
    const time = timestamps[index];
    if (!Number.isFinite(time)) {
      return { label: entry.bucket, full: entry.bucket, value: entry.kwh_100km };
    }
    return {
      label: formatDateTime(
        entry.bucket,
        locale,
        byDay ? { day: "2-digit", month: "2-digit" } : { hour: "2-digit", minute: "2-digit" },
      ),
      full: formatDateTime(entry.bucket, locale, { dateStyle: "medium", timeStyle: "short" }),
      value: entry.kwh_100km,
    };
  });
}

function TrendTooltip({
  active,
  payload,
  unit,
  locale,
}: {
  active?: boolean;
  payload?: { payload: TrendPoint }[];
  unit: string;
  locale: Locale;
}) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return (
    <div className="border-border bg-popover text-popover-foreground rounded border px-2 py-1.5 text-xs shadow-sm">
      <p className="text-muted-foreground mb-0.5 font-mono text-[0.6875rem]">{point.full}</p>
      <Figure
        className="text-sm font-medium"
        value={formatNumber(point.value, locale, { decimals: 1 })}
        unit={unit}
      />
    </div>
  );
}

/**
 * Question: how has the fleet's consumption developed?
 *
 * An area chart rather than a bar chart because the quantity is continuous and the reader is
 * looking for the shape of a trend, not comparing discrete buckets. The series comes from
 * simulated telemetry, so it carries the simulated badge — the chart would otherwise read as a
 * measurement of German traffic (ADR 004).
 */
export function EnergyTrendCard({
  summary,
  loading,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const mounted = useMounted();
  const points = useMemo(
    () => toPoints(summary?.energy_trend ?? [], locale),
    [summary?.energy_trend, locale],
  );

  return (
    <Panel>
      <PanelHeader>
        <SectionHeader
          eyebrow={t.overview.energyTrend}
          title={<WrappingTitle>{t.overview.energyTrendQuestion}</WrappingTitle>}
          actions={<SourceBadge origin="simulated" source="simulator" compact />}
        />
      </PanelHeader>
      <PanelBody className="pt-2 pr-2">
        {loading || !mounted ? (
          <Skeleton className="h-[180px] w-full" />
        ) : points.length === 0 ? (
          <EmptyState className="py-8" title={t.common.noData} description={t.states.emptyBody} />
        ) : (
          <ResponsiveContainer width="100%" height={180}>
            <AreaChart data={points} margin={{ top: 4, right: 4, bottom: 16, left: 0 }}>
              <CartesianGrid stroke="var(--border)" strokeDasharray="2 3" vertical={false} />
              <XAxis
                dataKey="label"
                tick={AXIS_STYLE}
                tickLine={false}
                axisLine={{ stroke: "var(--border)" }}
                minTickGap={28}
                label={{
                  value: t.overview.energyTrendTime,
                  position: "insideBottom",
                  offset: -12,
                  style: { ...AXIS_STYLE, textAnchor: "middle" },
                }}
              />
              <YAxis
                width={54}
                tick={AXIS_STYLE}
                tickLine={false}
                axisLine={false}
                domain={["auto", "auto"]}
                tickFormatter={(value: number) => formatNumber(value, locale, { decimals: 0 })}
                label={{
                  value: t.units.kwhPer100km,
                  angle: -90,
                  position: "insideLeft",
                  offset: 12,
                  style: { ...AXIS_STYLE, textAnchor: "middle" },
                }}
              />
              <Tooltip
                cursor={{ stroke: "var(--muted-foreground)", strokeWidth: 1 }}
                content={<TrendTooltip unit={t.units.kwhPer100km} locale={locale} />}
              />
              <Area
                type="monotone"
                dataKey="value"
                stroke="var(--primary)"
                strokeWidth={1.5}
                fill="var(--primary)"
                fillOpacity={0.12}
                dot={false}
                activeDot={{ r: 3, fill: "var(--primary)", stroke: "var(--card)" }}
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </PanelBody>
    </Panel>
  );
}
