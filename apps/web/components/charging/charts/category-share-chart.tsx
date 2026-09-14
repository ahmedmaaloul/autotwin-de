"use client";

import { useMemo } from "react";
import { Bar, BarChart, Label, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { formatNumber, formatPercent } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { ChargingCategory, ChargingStatistics } from "@/types/domain";

import { categoryBand, categoryLabel, CATEGORY_COLOR, Measure } from "../charging-vocabulary";
import { AXIS_LABEL, AXIS_TICK, ChartFrame, ChartTooltip } from "./chart-frame";

const ORDER: ChargingCategory[] = ["normal", "fast", "ultra_fast"];

interface Stacked {
  label: string;
  normal: number;
  fast: number;
  ultra_fast: number;
  total: number;
}

/**
 * The composition of the stock, as one stacked bar.
 *
 * This is the chart a pie would have been, and it is a bar for the usual reason: comparing
 * three angles is guesswork, comparing three lengths on a shared baseline is reading. A single
 * full-width bar also states the thing a pie hides — that "schnellladefähig" is still the
 * minority of German charging sites.
 */
export function CategoryShareChart({ data }: { data: ChargingStatistics["by_power_class"] }) {
  const t = useTranslations();
  const locale = useLocale();

  const { stacked, legend } = useMemo(() => {
    const counts = ORDER.map(
      (category) => data.find((entry) => entry.category === category)?.count ?? 0,
    );
    const total = counts.reduce((sum, value) => sum + value, 0);
    return {
      stacked: [
        {
          label: t.charging.share,
          normal: counts[0] ?? 0,
          fast: counts[1] ?? 0,
          ultra_fast: counts[2] ?? 0,
          total,
        },
      ] satisfies Stacked[],
      legend: ORDER.map((category, index) => ({
        category,
        count: counts[index] ?? 0,
        sharePercent: total > 0 ? ((counts[index] ?? 0) / total) * 100 : 0,
      })),
    };
  }, [data, t]);

  const row = stacked[0];

  return (
    <ChartFrame
      eyebrow={t.charging.normalVsFast}
      question={t.charging.normalVsFastQuestion}
      height={116}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={stacked} layout="vertical" margin={{ top: 4, right: 8, bottom: 28, left: 8 }}>
          <XAxis
            type="number"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={false}
            tickFormatter={(value: number) => formatNumber(value, locale)}
          >
            <Label
              value={t.charging.stations}
              position="insideBottom"
              offset={-16}
              style={AXIS_LABEL}
            />
          </XAxis>
          <YAxis type="category" dataKey="label" hide />
          <Tooltip
            cursor={{ fill: "var(--muted)", fillOpacity: 0.4 }}
            content={
              <ShareTooltip
                locale={locale}
                title={t.charging.normalVsFast}
                labels={ORDER.map((category) => categoryLabel(category, t))}
                total={row?.total ?? 0}
                totalLabel={t.charging.totalStations}
              />
            }
          />
          {ORDER.map((category, index) => (
            <Bar
              key={category}
              dataKey={category}
              name={categoryLabel(category, t)}
              stackId="share"
              fill={CATEGORY_COLOR[category]}
              maxBarSize={28}
              radius={
                index === 0 ? [2, 0, 0, 2] : index === ORDER.length - 1 ? [0, 2, 2, 0] : undefined
              }
            />
          ))}
        </BarChart>
      </ResponsiveContainer>

      <ul className="border-border -mt-4 grid gap-2 border-t pt-3 sm:grid-cols-3">
        {legend.map((entry) => (
          <li key={entry.category} className="flex items-baseline gap-2">
            <span
              aria-hidden
              className="mt-1 inline-block size-2 shrink-0 rounded-[1px]"
              style={{ backgroundColor: CATEGORY_COLOR[entry.category] }}
            />
            <span className="min-w-0">
              <span className="block truncate text-xs font-medium">
                {categoryLabel(entry.category, t)}
              </span>
              <span className="text-muted-foreground block text-[0.6875rem]">
                {categoryBand(entry.category, t)}
              </span>
            </span>
            <span className="ml-auto text-right">
              <Measure
                value={formatPercent(entry.sharePercent, locale)}
                valueClassName="text-sm font-medium"
              />
              <span className="text-muted-foreground block font-mono text-[0.6875rem] tabular-nums">
                {formatNumber(entry.count, locale)}
              </span>
            </span>
          </li>
        ))}
      </ul>
    </ChartFrame>
  );
}

function ShareTooltip({
  active,
  payload,
  locale,
  title,
  labels,
  total,
  totalLabel,
}: {
  active?: boolean;
  payload?: { dataKey?: string | number; value?: number; color?: string }[];
  locale: ReturnType<typeof useLocale>;
  title: string;
  labels: string[];
  total: number;
  totalLabel: string;
}) {
  if (!payload || payload.length === 0) return null;

  const rows = payload.map((entry, index) => ({
    label: labels[index] ?? String(entry.dataKey ?? ""),
    value: formatNumber(entry.value ?? 0, locale),
    color: entry.color,
  }));

  return (
    <ChartTooltip
      active={active}
      title={title}
      rows={[...rows, { label: totalLabel, value: formatNumber(total, locale) }]}
    />
  );
}
