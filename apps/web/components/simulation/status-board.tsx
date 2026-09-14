"use client";

import { Activity, AlertTriangle, Car, Database, Gauge } from "lucide-react";

import { MetricCard, MetricRow } from "@/components/shared/metric-card";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { ErrorState } from "@/components/shared/states";
import { StatusBadge } from "@/components/shared/status-badge";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { SimulatorStatus } from "@/types/domain";

import { simulationLabel, simulationTone } from "./state-labels";

/**
 * What the simulator is doing right now.
 *
 * Six figures, polled every three seconds, because they are the ones that separate "running" from
 * "running correctly": vehicles alone says nothing if the event rate is zero, and a healthy rate
 * says nothing if the database writes are not keeping up with it. The error counter is last and
 * turns red on its own — a simulator that is dropping messages should not look calm.
 */
export function StatusBoard({
  status,
  isPending,
  isError,
  error,
  onRetry,
}: {
  status: SimulatorStatus | undefined;
  isPending: boolean;
  isError: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const t = useTranslations();
  const locale = useLocale();

  if (isError) return <ErrorState error={error} onRetry={onRetry} />;

  const loading = isPending || !status;

  return (
    <div className="space-y-3">
      <SectionHeader
        eyebrow={t.simulation.statusTitle}
        title={t.simulation.statusSubtitle}
        actions={<SourceBadge origin="simulated" source="simulator" />}
      />

      <MetricRow columns={6}>
        <MetricCard
          label={t.simulation.state}
          icon={Activity}
          loading={loading}
          value={
            status ? (
              <StatusBadge
                className="font-sans"
                status={simulationTone(status.state)}
                label={simulationLabel(status.state, t)}
                pulse={status.state === "running"}
              />
            ) : null
          }
        />
        <MetricCard
          label={t.simulation.vehiclesActive}
          icon={Car}
          loading={loading}
          value={formatNumber(status?.vehicles_active, locale)}
          unit={t.units.vehicles}
        />
        <MetricCard
          label={t.simulation.eventsPerSecond}
          icon={Gauge}
          loading={loading}
          value={formatNumber(status?.events_per_second, locale, { decimals: 1 })}
        />
        <MetricCard
          label={t.simulation.messagesProcessed}
          loading={loading}
          value={formatNumber(status?.messages_processed, locale)}
        />
        <MetricCard
          label={t.simulation.dbWrites}
          icon={Database}
          loading={loading}
          value={formatNumber(status?.db_writes, locale)}
        />
        <MetricCard
          label={t.simulation.errors}
          icon={AlertTriangle}
          loading={loading}
          value={
            <span className={status && status.errors > 0 ? "text-danger" : undefined}>
              {formatNumber(status?.errors, locale)}
            </span>
          }
        />
      </MetricRow>
    </div>
  );
}
