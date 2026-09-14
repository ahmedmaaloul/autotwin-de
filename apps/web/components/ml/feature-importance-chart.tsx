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

import {
  AXIS_LINE,
  AXIS_TICK,
  axisLabel,
  ChartPanel,
  ChartTooltip,
  GRID_STROKE,
} from "@/components/analytics/chart-kit";
import { EmptyState } from "@/components/shared/states";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { MLMetrics } from "@/types/domain";

const TOP_N = 15;

/**
 * Global feature importance from SHAP, top fifteen.
 *
 * Mean |SHAP| rather than the tree's own split-gain importance: gain is biased towards
 * high-cardinality features and cannot be compared across models, while mean |SHAP| is in the
 * units of the target and says what the feature actually moved the prediction by
 * (docs/ml/energy-model.md).
 */
export function FeatureImportanceChart({
  importance,
}: {
  importance: MLMetrics["feature_importance"];
}) {
  const t = useTranslations();
  const locale = useLocale();

  const rows = importance
    .slice()
    .sort((a, b) => b.importance - a.importance)
    .slice(0, TOP_N);

  const renderTooltip = (props: TooltipProps<number, string>) => {
    if (!props.active || !props.payload?.length) return null;
    const row = props.payload[0]?.payload as (typeof rows)[number] | undefined;
    if (!row) return null;
    return (
      <ChartTooltip
        title={row.feature}
        rows={[
          {
            label: t.ml.axisImportance,
            value: formatNumber(row.importance, locale, { decimals: 3 }),
            swatch: "var(--primary)",
          },
        ]}
      />
    );
  };

  return (
    <ChartPanel
      eyebrow={t.ml.shapTitle}
      title={t.ml.featureImportance}
      question={t.ml.featureImportanceQuestion}
      height={Math.max(240, rows.length * 24 + 64)}
      footer={t.ml.topFeatures}
    >
      {rows.length === 0 ? (
        <div className="p-3">
          <EmptyState title={t.ml.noExplanation} description={t.ml.noExplanationBody} />
        </div>
      ) : (
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={rows}
            layout="vertical"
            margin={{ top: 4, right: 16, bottom: 26, left: 4 }}
            barCategoryGap={3}
          >
            <CartesianGrid stroke={GRID_STROKE} strokeDasharray="2 4" horizontal={false} />
            <XAxis
              type="number"
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={AXIS_LINE}
              tickFormatter={(value: number) => formatNumber(value, locale, { decimals: 2 })}
              label={axisLabel(t.ml.axisImportance, "x")}
            />
            <YAxis
              type="category"
              dataKey="feature"
              tick={AXIS_TICK}
              tickLine={false}
              axisLine={AXIS_LINE}
              width={148}
            />
            <Tooltip content={renderTooltip} cursor={{ fill: "var(--muted)", opacity: 0.6 }} />
            <Bar dataKey="importance" fill="var(--primary)" isAnimationActive={false} maxBarSize={14} />
          </BarChart>
        </ResponsiveContainer>
      )}
    </ChartPanel>
  );
}
