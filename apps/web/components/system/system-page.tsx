"use client";

import { RefreshCw } from "lucide-react";

import { DataFreshness } from "@/components/shared/data-freshness";
import { PageHeader, SectionHeader } from "@/components/shared/section-header";
import { StatusBadge } from "@/components/shared/status-badge";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { formatDuration } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { ServiceCheckList } from "./service-check-list";
import { SimulatorStatusCard } from "./simulator-status-card";
import { useReadiness, useServiceHealth } from "./use-system-health";

/**
 * Is the platform answering, and if not, which part of it is not?
 *
 * `/health` says the process is alive and which build it is; `/ready` says whether its
 * dependencies are. Keeping them apart is the whole point of the two endpoints — a container
 * that is up but cannot reach PostGIS is a different incident from one that has crashed — so the
 * page shows both rather than merging them into a single traffic light.
 */
export function SystemPage() {
  const t = useTranslations();
  const locale = useLocale();

  const health = useServiceHealth();
  const readiness = useReadiness();

  const isReady = readiness.data?.status === "ready" || readiness.data?.status === "ok";
  const checks = readiness.data?.checks ?? {};

  const refresh = () => {
    void health.refetch();
    void readiness.refetch();
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title={t.system.title}
        description={t.system.subtitle}
        actions={
          <Button type="button" variant="outline" size="sm" onClick={refresh}>
            <RefreshCw className="size-3.5" aria-hidden />
            {t.system.refresh}
          </Button>
        }
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="gap-0 rounded border p-0 shadow-none">
          <div className="border-border border-b px-4 py-3">
            <SectionHeader
              eyebrow={t.system.api}
              title={t.system.healthTitle}
              description={t.system.healthSubtitle}
              actions={
                readiness.isError ? (
                  <StatusBadge status="failed" label={t.system.notReady} />
                ) : readiness.isPending ? (
                  <StatusBadge status="neutral" label={t.common.loading} />
                ) : (
                  <StatusBadge
                    status={isReady ? "healthy" : "degraded"}
                    label={isReady ? t.system.ready : t.system.notReady}
                    pulse={isReady}
                  />
                )
              }
            />
          </div>

          {health.isPending ? (
            <div className="p-4">
              <LoadingSkeleton rows={2} />
            </div>
          ) : health.isError ? (
            <div className="p-4">
              <ErrorState error={health.error} onRetry={() => void health.refetch()} />
            </div>
          ) : (
            <dl className="divide-border grid grid-cols-3 divide-x">
              <Field label={t.system.version} value={health.data?.version ?? "–"} />
              <Field
                label={t.system.uptime}
                value={
                  health.data ? formatDuration(health.data.uptime_s, locale) : "–"
                }
              />
              <div className="min-w-0 px-4 py-3">
                <dt className="eyebrow mb-1">{t.system.lastChecked}</dt>
                <dd>
                  <DataFreshness
                    timestamp={
                      readiness.dataUpdatedAt ? new Date(readiness.dataUpdatedAt) : null
                    }
                  />
                </dd>
              </div>
            </dl>
          )}

        </Card>

        <Card className="gap-0 rounded border p-0 shadow-none">
          <div className="border-border border-b px-4 py-3">
            <SectionHeader
              eyebrow={t.system.title}
              title={t.system.dependencies}
              description={t.system.dependenciesSubtitle}
            />
          </div>

          {readiness.isPending ? (
            <div className="p-4">
              <LoadingSkeleton rows={3} />
            </div>
          ) : readiness.isError ? (
            <div className="p-4">
              <ErrorState error={readiness.error} onRetry={() => void readiness.refetch()} />
            </div>
          ) : (
            <ServiceCheckList checks={checks} />
          )}
        </Card>
      </div>

      <SimulatorStatusCard />
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 px-4 py-3">
      <dt className="eyebrow mb-1">{label}</dt>
      <dd className="truncate font-mono text-sm tabular-nums">{value}</dd>
    </div>
  );
}
