"use client";

import { MetricCard, MetricRow } from "@/components/shared/metric-card";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { MLMetrics } from "@/types/domain";

type Metrics = NonNullable<MLMetrics["metrics"]>;

/** Relative change of the model against the baseline, as a signed percentage. */
function improvement(model: number, baseline: number): number | null {
  if (!Number.isFinite(model) || !Number.isFinite(baseline) || baseline === 0) return null;
  return ((model - baseline) / Math.abs(baseline)) * 100;
}

/**
 * The three gütemaße, each against the physical baseline it has to beat.
 *
 * Reporting an MAE on its own says nothing: gradient boosting will always fit *something*. The
 * honest claim is the comparison — the same test set, scored by a model that knows only physics
 * and by one that learned from the simulator — so the baseline travels with every figure and the
 * delta carries its sign. For MAE and RMSE a fall is an improvement, for R² a rise is; the card
 * is told which, rather than assuming that up is good.
 */
export function MetricsComparison({ metrics }: { metrics: Metrics }) {
  const t = useTranslations();
  const locale = useLocale();

  const baselineFooter = (value: number, decimals: number, unit?: string) => (
    <span className="flex items-baseline gap-1">
      <span className="text-muted-foreground">{t.ml.baseline}</span>
      <span className="text-foreground font-mono tabular-nums">
        {formatNumber(value, locale, { decimals })}
      </span>
      {unit ? <span className="text-muted-foreground">{unit}</span> : null}
    </span>
  );

  return (
    <MetricRow columns={3}>
      <MetricCard
        label={t.ml.mae}
        hint={t.ml.maeHint}
        value={formatNumber(metrics.mae, locale, { decimals: 2 })}
        unit={t.units.kwhPer100km}
        delta={improvement(metrics.mae, metrics.baseline_mae)}
        positiveIsGood={false}
        footer={baselineFooter(metrics.baseline_mae, 2, t.units.kwhPer100km)}
      />
      <MetricCard
        label={t.ml.rmse}
        hint={t.ml.rmseHint}
        value={formatNumber(metrics.rmse, locale, { decimals: 2 })}
        unit={t.units.kwhPer100km}
        delta={improvement(metrics.rmse, metrics.baseline_rmse)}
        positiveIsGood={false}
        footer={baselineFooter(metrics.baseline_rmse, 2, t.units.kwhPer100km)}
      />
      <MetricCard
        label={t.ml.r2}
        hint={t.ml.r2Hint}
        value={formatNumber(metrics.r2, locale, { decimals: 3 })}
        delta={improvement(metrics.r2, metrics.baseline_r2)}
        positiveIsGood
        footer={baselineFooter(metrics.baseline_r2, 3)}
      />
    </MetricRow>
  );
}
