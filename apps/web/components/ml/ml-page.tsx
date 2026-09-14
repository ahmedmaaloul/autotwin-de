"use client";

import { BrainCircuit, FlaskConical } from "lucide-react";
import { useState } from "react";

import { NoticePanel } from "@/components/analytics/notice-panel";
import { PageHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useMlMetrics, useMlModels } from "@/hooks/use-autotwin";
import { useTranslations } from "@/providers/locale-provider";

import { FeatureImportanceChart } from "./feature-importance-chart";
import { MetricsComparison } from "./metrics-comparison";
import { ModelCard } from "./model-card";
import { PredictedVsActualChart } from "./predicted-vs-actual-chart";
import { ResidualChart } from "./residual-chart";

/**
 * The energy model, its evidence, and the caveat that governs all of it.
 *
 * The notice at the top is not a disclaimer bolted on afterwards — it is the finding. The model
 * was trained on telemetry this platform generated (ADR 004), so what these metrics demonstrate
 * is that the pipeline — features, training, evaluation against a physical baseline, SHAP
 * attribution — is sound. They say nothing about a real BMW i4 on a real A8, and the page is
 * built so that nobody can read the numbers without having read that first.
 */
export function MlPage() {
  const t = useTranslations();
  const modelsQuery = useMlModels();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const models = modelsQuery.data ?? [];
  const selected =
    models.find((model) => model.id === selectedId) ??
    models.find((model) => model.is_active) ??
    models[0] ??
    null;

  const metricsQuery = useMlMetrics(
    selected ? { model: selected.name, version: selected.version } : {},
  );
  const metrics = metricsQuery.data;
  const model = selected ?? metrics?.model ?? null;

  const hasNoModel =
    !modelsQuery.isPending && !modelsQuery.isError && models.length === 0 && !metrics?.model;

  return (
    <div className="space-y-6">
      <PageHeader
        title={t.ml.title}
        description={t.ml.subtitle}
        actions={
          models.length > 1 ? (
            <Select value={model?.id} onValueChange={setSelectedId}>
              <SelectTrigger aria-label={t.ml.selectModel} className="min-w-56">
                <SelectValue placeholder={t.ml.selectModel} />
              </SelectTrigger>
              <SelectContent>
                {models.map((entry) => (
                  <SelectItem
                    key={entry.id}
                    value={entry.id}
                    textValue={`${entry.name} ${entry.version}`}
                  >
                    {entry.name}
                    <span className="text-muted-foreground ml-1 font-mono text-xs">
                      {entry.version}
                    </span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : null
        }
      />

      <NoticePanel
        icon={FlaskConical}
        title={t.ml.trainingOrigin}
        actions={<SourceBadge origin="simulated" source="simulator" />}
      >
        {t.ml.trainingDataNotice}
      </NoticePanel>

      {modelsQuery.isPending ? <LoadingSkeleton rows={6} /> : null}

      {modelsQuery.isError ? (
        <ErrorState error={modelsQuery.error} onRetry={() => void modelsQuery.refetch()} />
      ) : null}

      {hasNoModel ? (
        <EmptyState
          icon={BrainCircuit}
          title={t.ml.noModelTitle}
          description={t.ml.noModelBody}
          action={
            <code className="border-border bg-muted text-foreground mt-1 rounded border px-2 py-1 font-mono text-xs">
              make ml-train
            </code>
          }
        />
      ) : null}

      {model ? <ModelCard model={model} /> : null}

      {model && metricsQuery.isPending ? <LoadingSkeleton rows={4} /> : null}

      {model && metricsQuery.isError ? (
        <ErrorState error={metricsQuery.error} onRetry={() => void metricsQuery.refetch()} />
      ) : null}

      {model && metrics ? (
        metrics.metrics ? (
          <div className="space-y-4">
            <MetricsComparison metrics={metrics.metrics} />

            <div className="grid gap-4 xl:grid-cols-2">
              <PredictedVsActualChart points={metrics.predicted_vs_actual} />
              <ResidualChart residuals={metrics.residuals} />
            </div>

            <FeatureImportanceChart importance={metrics.feature_importance} />
          </div>
        ) : (
          <EmptyState title={t.ml.noMetrics} description={t.ml.noMetricsBody} />
        )
      ) : null}
    </div>
  );
}
