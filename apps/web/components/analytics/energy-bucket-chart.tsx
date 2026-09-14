"use client";

import { useMemo } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  type TooltipProps,
  XAxis,
  YAxis,
} from "recharts";

import { SourceBadge } from "@/components/shared/source-badge";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useEnergyAnalytics } from "@/hooks/use-autotwin";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { AXIS_LINE, AXIS_TICK, axisLabel, ChartEmpty, ChartLegend, ChartPanel, ChartTooltip, GRID_STROKE } from "./chart-kit";

type Dimension = "temperature" | "speed" | "traffic";

interface Row {
  label: string;
  mean: number;
  samples: number;
}

/**
 * Mean consumption per bucket of one driving condition, with the sample count behind it.
 *
 * The sample line is not decoration: a bucket built from 11 trips and a bucket built from
 * 11 000 carry the same bar height and entirely different weight, and hiding that would make the
 * chart more confident than the data. The telemetry underneath is simulated (ADR 004), which is
 * why every one of these panels carries the badge.
 */
export function EnergyBucketChart({
  dimension,
  title,
  question,
  axisTitle,
}: {
  dimension: Dimension;
  title: string;
  question: string;
  axisTitle: string;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const query = useEnergyAnalytics(dimension);

  const severityLabels = useMemo<Record<string, string>>(
    () => ({
      low: t.analytics.severityLow,
      moderate: t.analytics.severityModerate,
      high: t.analytics.severityHigh,
      severe: t.analytics.severitySevere,
    }),
    [t],
  );

  const rows = useMemo<Row[]>(
    () =>
      (query.data?.buckets ?? []).map((bucket) => ({
        label:
          typeof bucket.bucket === "number"
            ? formatNumber(bucket.bucket, locale, { decimals: 0 })
            : (severityLabels[bucket.bucket] ?? bucket.bucket),
        mean: bucket.mean_kwh_100km,
        samples: bucket.samples,
      })),
    [query.data, locale, severityLabels],
  );

  const totalSamples = rows.reduce((sum, row) => sum + row.samples, 0);

  const renderTooltip = (props: TooltipProps<number, string>) => {
    if (!props.active || !props.payload?.length) return null;
    const row = props.payload[0]?.payload as Row | undefined;
    if (!row) return null;
    return (
      <ChartTooltip
        title={`${axisTitle}: ${row.label}`}
        rows={[
          {
            label: t.analytics.axisConsumption,
            value: formatNumber(row.mean, locale, { decimals: 1 }),
            unit: t.units.kwhPer100km,
            swatch: "var(--primary)",
          },
          {
            label: t.analytics.samples,
            value: formatNumber(row.samples, locale),
            swatch: "var(--muted-foreground)",
          },
        ]}
      />
    );
  };

  return (
    <ChartPanel
      eyebrow={t.analytics.energy}
      title={title}
      question={question}
      height={264}
      actions={<SourceBadge origin="simulated" compact />}
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2">
          <ChartLegend
            items={[
              { color: "var(--primary)", label: t.analytics.axisConsumption },
              { color: "var(--muted-foreground)", label: t.analytics.axisSamples, dashed: true },
            ]}
          />
          {rows.length > 0 ? (
            <span className="font-mono tabular-nums">
              {formatNumber(totalSamples, locale, { compact: totalSamples > 9999 })}{" "}
              <span className="text-muted-foreground">{t.analytics.samples}</span>
            </span>
          ) : null}
        </div>
      }
    >
      {query.isPending ? (
        <div className="p-2">
          <LoadingSkeleton rows={4} />
        </div>
      ) : query.isError ? (
        <div className="p-2">
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </div>
      ) : rows.length === 0 ? (
        <ChartEmpty message={t.common.noData} />
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 8, right: 8, bottom: 26, left: 4 }}>
            <CartesianGrid stroke={GRID_STROKE} strokeDasharray="2 4" vertical={false} />
            <XAxis
              dataKey="label"
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={AXIS_LINE}
              interval="preserveStartEnd"
              label={axisLabel(axisTitle, "x")}
            />
            <YAxis
              yAxisId="consumption"
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={AXIS_LINE}
              width={44}
              label={axisLabel(t.units.kwhPer100km, "y")}
            />
            <YAxis
              yAxisId="samples"
              orientation="right"
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={AXIS_LINE}
              width={40}
              tickFormatter={(value: number) => formatNumber(value, locale, { compact: true })}
            />
            <Tooltip content={renderTooltip} cursor={{ fill: "var(--muted)", opacity: 0.6 }} />
            <Bar
              yAxisId="consumption"
              dataKey="mean"
              name={t.analytics.axisConsumption}
              fill="var(--primary)"
              maxBarSize={26}
              isAnimationActive={false}
            />
            <Line
              yAxisId="samples"
              type="monotone"
              dataKey="samples"
              name={t.analytics.axisSamples}
              stroke="var(--muted-foreground)"
              strokeWidth={1.25}
              strokeDasharray="3 3"
              dot={false}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </ChartPanel>
  );
}
