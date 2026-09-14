"use client";

import { useMemo } from "react";
import {
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  type TooltipProps,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

import {
  AXIS_LINE,
  AXIS_TICK,
  axisLabel,
  ChartEmpty,
  ChartLegend,
  ChartPanel,
  ChartTooltip,
  GRID_STROKE,
} from "@/components/analytics/chart-kit";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { MLMetrics } from "@/types/domain";

interface Point {
  x: number;
  y: number;
}

/**
 * Prediction against measurement, with the identity line as the reference.
 *
 * Both series are plotted against the same x: the ML prediction and the physical baseline for
 * the very same trips. The question "is the model better?" then becomes visual and honest —
 * whichever cloud hugs y = x more tightly wins, and systematic bias shows up as a cloud that
 * sits consistently above or below the line rather than as a smaller error number.
 */
export function PredictedVsActualChart({ points }: { points: MLMetrics["predicted_vs_actual"] }) {
  const t = useTranslations();
  const locale = useLocale();

  const { model, baseline, domain } = useMemo(() => {
    const model: Point[] = [];
    const baseline: Point[] = [];
    let min = Number.POSITIVE_INFINITY;
    let max = Number.NEGATIVE_INFINITY;

    for (const point of points) {
      model.push({ x: point.actual, y: point.predicted });
      baseline.push({ x: point.actual, y: point.baseline });
      min = Math.min(min, point.actual, point.predicted, point.baseline);
      max = Math.max(max, point.actual, point.predicted, point.baseline);
    }

    if (!Number.isFinite(min) || !Number.isFinite(max)) return { model, baseline, domain: null };
    const padding = Math.max(1, (max - min) * 0.05);
    return {
      model,
      baseline,
      domain: [Math.max(0, min - padding), max + padding] as [number, number],
    };
  }, [points]);

  const renderTooltip = (props: TooltipProps<number, string>) => {
    if (!props.active || !props.payload?.length) return null;
    const point = props.payload[0]?.payload as Point | undefined;
    if (!point) return null;
    return (
      <ChartTooltip
        title={t.ml.predictedVsActual}
        rows={[
          {
            label: t.ml.axisActual,
            value: formatNumber(point.x, locale, { decimals: 1 }),
            unit: t.units.kwhPer100km,
          },
          {
            label: t.ml.axisPredicted,
            value: formatNumber(point.y, locale, { decimals: 1 }),
            unit: t.units.kwhPer100km,
          },
        ]}
      />
    );
  };

  return (
    <ChartPanel
      eyebrow={t.ml.metrics}
      title={t.ml.predictedVsActual}
      question={t.ml.predictedVsActualQuestion}
      height={320}
      footer={
        <ChartLegend
          items={[
            { color: "var(--primary)", label: t.ml.mlModel },
            { color: "var(--muted-foreground)", label: t.ml.baseline },
            { color: "var(--foreground)", label: t.ml.identityLine, dashed: true },
          ]}
        />
      }
    >
      {points.length === 0 || !domain ? (
        <ChartEmpty message={t.common.noData} />
      ) : (
        // Recharts marks every scatter symbol `role="img"`, which would announce several hundred
        // unlabelled images. Declaring the plot as one labelled image collapses that subtree into
        // a single node; the numbers behind it are stated as metrics directly above the chart.
        <div
          role="img"
          aria-label={`${t.ml.predictedVsActual} — ${t.ml.predictedVsActualQuestion}`}
          className="size-full"
        >
          <ResponsiveContainer width="100%" height="100%">
            <ScatterChart margin={{ top: 8, right: 12, bottom: 26, left: 4 }}>
              <CartesianGrid stroke={GRID_STROKE} strokeDasharray="2 4" />
              <XAxis
                type="number"
                dataKey="x"
                domain={domain}
                tick={AXIS_TICK}
                tickLine={false}
                axisLine={AXIS_LINE}
                tickFormatter={(value: number) => formatNumber(value, locale, { decimals: 0 })}
                label={axisLabel(`${t.ml.axisActual} (${t.units.kwhPer100km})`, "x")}
              />
              <YAxis
                type="number"
                dataKey="y"
                domain={domain}
                width={44}
                tick={AXIS_TICK}
                tickLine={false}
                axisLine={AXIS_LINE}
                tickFormatter={(value: number) => formatNumber(value, locale, { decimals: 0 })}
                label={axisLabel(t.units.kwhPer100km, "y")}
              />
              <ZAxis range={[16, 16]} />
              <ReferenceLine
                segment={[
                  { x: domain[0], y: domain[0] },
                  { x: domain[1], y: domain[1] },
                ]}
                stroke="var(--foreground)"
                strokeDasharray="4 4"
                strokeOpacity={0.45}
                ifOverflow="extendDomain"
              />
              <Tooltip content={renderTooltip} cursor={{ strokeDasharray: "3 3" }} />
              <Scatter
                name={t.ml.baseline}
                data={baseline}
                fill="var(--muted-foreground)"
                fillOpacity={0.35}
                isAnimationActive={false}
              />
              <Scatter
                name={t.ml.mlModel}
                data={model}
                fill="var(--primary)"
                fillOpacity={0.7}
                isAnimationActive={false}
              />
            </ScatterChart>
          </ResponsiveContainer>
        </div>
      )}
    </ChartPanel>
  );
}
