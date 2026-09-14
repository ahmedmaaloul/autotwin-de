"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  type TooltipProps,
  XAxis,
  YAxis,
} from "recharts";

import { SourceBadge } from "@/components/shared/source-badge";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { AXIS_LINE, AXIS_TICK, axisLabel, ChartEmpty, ChartPanel, ChartTooltip, GRID_STROKE } from "./chart-kit";
import {
  metricDecimals,
  metricLabel,
  metricUnit,
  type RegionMetric,
  type RegionRow,
} from "./region-metrics";

/**
 * The sixteen federal states on one metric, ranked.
 *
 * Horizontal bars because the category labels are long German state names, and a ranked
 * horizontal bar is the one chart form that reads without rotating anything.
 */
export function RegionalChart({
  rows,
  metric,
}: {
  rows: RegionRow[];
  metric: RegionMetric;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const label = metricLabel(metric, t);
  const unit = metricUnit(metric, t);
  const decimals = metricDecimals(metric);

  const data = rows
    .slice()
    .sort((a, b) => b[metric] - a[metric])
    .map((row) => ({ bundesland: row.bundesland, value: row[metric] }));

  const renderTooltip = (props: TooltipProps<number, string>) => {
    if (!props.active || !props.payload?.length) return null;
    const row = props.payload[0]?.payload as { bundesland: string; value: number } | undefined;
    if (!row) return null;
    return (
      <ChartTooltip
        title={row.bundesland}
        rows={[
          {
            label,
            value: formatNumber(row.value, locale, { decimals }),
            unit,
            swatch: "var(--primary)",
          },
        ]}
      />
    );
  };

  return (
    <ChartPanel
      eyebrow={t.analytics.regional}
      title={label}
      question={t.analytics.regionalQuestion}
      height={480}
      actions={<SourceBadge origin="official" source="bundesnetzagentur" compact />}
    >
      {data.length === 0 ? (
        <ChartEmpty message={t.common.noData} />
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 4, right: 16, bottom: 26, left: 4 }}
            barCategoryGap={4}
          >
            <CartesianGrid stroke={GRID_STROKE} strokeDasharray="2 4" horizontal={false} />
            <XAxis
              type="number"
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={AXIS_LINE}
              tickFormatter={(value: number) => formatNumber(value, locale, { compact: true })}
              label={axisLabel(unit ? `${label} (${unit})` : label, "x")}
            />
            <YAxis
              type="category"
              dataKey="bundesland"
              tick={{ ...AXIS_TICK, fontFamily: "var(--font-sans)" }}
              tickLine={false}
              axisLine={AXIS_LINE}
              width={128}
            />
            <Tooltip content={renderTooltip} cursor={{ fill: "var(--muted)", opacity: 0.6 }} />
            <Bar dataKey="value" fill="var(--primary)" isAnimationActive={false} maxBarSize={18} />
          </BarChart>
        </ResponsiveContainer>
      )}
    </ChartPanel>
  );
}
