"use client";

import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { StatusBadge } from "@/components/shared/status-badge";
import { Card } from "@/components/ui/card";
import { formatDateTime, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { MLModelInfo } from "@/types/domain";

/**
 * The model's identity card: what was trained, from what, when, and on how much.
 *
 * A metric without the artefact it describes is unfalsifiable, so the version, the algorithm and
 * the row count sit above the numbers rather than in a footnote — and the feature list is shown
 * in full, because "which inputs does it see" is the first question an engineer asks of someone
 * else's model.
 */
export function ModelCard({ model }: { model: MLModelInfo }) {
  const t = useTranslations();
  const locale = useLocale();

  return (
    <Card className="gap-0 rounded border p-0 shadow-none">
      <div className="border-border border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.ml.modelCard}
          title={model.name}
          description={model.notes ?? undefined}
          actions={
            <div className="flex items-center gap-2">
              <SourceBadge origin={model.training_data_origin} source="simulator" compact />
              <StatusBadge
                status={model.is_active ? "healthy" : "neutral"}
                label={model.is_active ? t.ml.active : t.ml.inactive}
              />
            </div>
          }
        />
      </div>

      <dl className="divide-border grid grid-cols-2 divide-y sm:grid-cols-3 sm:divide-y-0 lg:grid-cols-5">
        <Field label={t.ml.version} value={model.version} mono />
        <Field label={t.ml.algorithm} value={model.algorithm} mono />
        <Field
          label={t.ml.trainedAt}
          value={formatDateTime(model.trained_at, locale, { dateStyle: "medium", timeStyle: "short" })}
          mono
        />
        <Field label={t.ml.datasetSize} value={formatNumber(model.training_rows, locale)} mono />
        <Field label={t.ml.features} value={formatNumber(model.feature_names.length, locale)} mono />
      </dl>

      <div className="border-border border-t px-4 py-3">
        <p className="eyebrow mb-2">{t.ml.features}</p>
        {model.feature_names.length === 0 ? (
          <p className="text-muted-foreground text-xs">{t.common.noData}</p>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {model.feature_names.map((feature) => (
              <li
                key={feature}
                className="border-border bg-muted text-muted-foreground rounded border px-1.5 py-0.5 font-mono text-[0.6875rem]"
              >
                {feature}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="border-border min-w-0 px-4 py-3 sm:not-last:border-r">
      <dt className="eyebrow mb-1">{label}</dt>
      <dd className={mono ? "truncate font-mono text-sm tabular-nums" : "truncate text-sm"}>
        {value}
      </dd>
    </div>
  );
}
