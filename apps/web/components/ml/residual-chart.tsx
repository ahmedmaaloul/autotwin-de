"use client";

import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  type TooltipProps,
  XAxis,
  YAxis,
} from "recharts";

import {
  AXIS_LINE,
  AXIS_TICK,
  axisLabel,
  ChartEmpty,
  ChartPanel,
  ChartTooltip,
  GRID_STROKE,
} from "@/components/analytics/chart-kit";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { MLMetrics } from "@/types/domain";

/**
 * Distribution of the prediction errors.
 *
 * A model can hit a respectable MAE while being systematically wrong — always optimistic in the
 * cold, say. That shows up here and nowhere else: a histogram leaning to one side of zero is
 * bias, a wide symmetric one is noise, and the two call for entirely different fixes. The bar
 * containing zero is drawn in the success colour so the centre of the distribution is findable
 * without reading the axis.
 */
export function ResidualChart({ residuals }: { residuals: MLMetrics["residuals"] }) {
  const t = useTranslations();
  const locale = useLocale();

  const rows = useMemo(
    () =>
      residuals
        .slice()
        .sort((a, b) => a.bucket - b.bucket)
        .map((entry) => ({
          label: formatNumber(entry.bucket, locale, { decimals: 1 }),
          bucket: entry.bucket,
          count: entry.count,
        })),
    [residuals, locale],
  );

  // The bin whose centre is closest to zero — the one an unbiased model should peak in.
  const centreLabel = useMemo(() => {
    if (rows.length === 0) return null;
    return rows.reduce((closest, row) =>
      Math.abs(row.bucket) < Math.abs(closest.bucket) ? row : closest,
    ).label;
  }, [rows]);

  const renderTooltip = (props: TooltipProps<number, string>) => {
    if (!props.active || !props.payload?.length) return null;
    const row = props.payload[0]?.payload as (typeof rows)[number] | undefined;
    if (!row) return null;
    return (
      <ChartTooltip
        title={`${t.ml.axisResidual} ${row.label} ${t.units.kwhPer100km}`}
        rows={[
          {
            label: t.ml.axisCount,
            value: formatNumber(row.count, locale),
            swatch: "var(--primary)",
          },
        ]}
      />
    );
  };

  return (
    <ChartPanel
      eyebrow={t.ml.metrics}
      title={t.ml.residuals}
      question={t.ml.residualsQuestion}
      height={320}
    >
      {rows.length === 0 ? (
        <ChartEmpty message={t.common.noData} />
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={rows} margin={{ top: 8, right: 12, bottom: 26, left: 4 }}>
            <CartesianGrid stroke={GRID_STROKE} strokeDasharray="2 4" vertical={false} />
            <XAxis
              dataKey="label"
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={AXIS_LINE}
              interval="preserveStartEnd"
              label={axisLabel(`${t.ml.axisResidual} (${t.units.kwhPer100km})`, "x")}
            />
            <YAxis
              width={44}
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={AXIS_LINE}
              tickFormatter={(value: number) => formatNumber(value, locale, { compact: true })}
              label={axisLabel(t.ml.axisCount, "y")}
            />
            <Tooltip content={renderTooltip} cursor={{ fill: "var(--muted)", opacity: 0.6 }} />
            <Bar dataKey="count" isAnimationActive={false} maxBarSize={40}>
              {rows.map((row) => (
                <Cell
                  key={row.label}
                  fill={row.label === centreLabel ? "var(--success)" : "var(--primary)"}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </ChartPanel>
  );
}
