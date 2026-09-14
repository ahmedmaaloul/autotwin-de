"use client";

import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { StatusBadge, type StatusTone } from "@/components/shared/status-badge";
import { LoadingSkeleton } from "@/components/shared/states";
import { Card } from "@/components/ui/card";
import { useSimulatorStatus } from "@/hooks/use-autotwin";
import { formatNumber } from "@/lib/i18n/format";
import type { Messages } from "@/lib/i18n/messages/de";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { SimulationState } from "@/types/domain";

function stateTone(state: SimulationState): StatusTone {
  switch (state) {
    case "running":
      return "healthy";
    case "paused":
    case "stopping":
      return "delayed";
    case "failed":
      return "failed";
    default:
      return "neutral";
  }
}

function stateLabel(state: SimulationState, t: Messages): string {
  switch (state) {
    case "running":
      return t.status.running;
    case "paused":
      return t.status.paused;
    case "pending":
      return t.status.pending;
    case "completed":
      return t.status.completed;
    case "failed":
      return t.status.failed;
    default:
      return t.status.stopped;
  }
}

/**
 * The simulator, from the system's point of view rather than the operator's.
 *
 * `/simulation` is where the fleet is controlled; here it is one more dependency that is either
 * producing events or not — which is the question you ask when the live map looks empty.
 */
export function SimulatorStatusCard() {
  const t = useTranslations();
  const locale = useLocale();
  const query = useSimulatorStatus({ retry: false });
  const status = query.data;

  return (
    <Card className="gap-0 rounded border p-0 shadow-none">
      <div className="border-border border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.system.title}
          title={t.system.simulator}
          description={t.system.simulatorSubtitle}
          actions={
            <div className="flex items-center gap-2">
              <SourceBadge origin="simulated" compact />
              {status ? (
                <StatusBadge
                  status={stateTone(status.state)}
                  label={stateLabel(status.state, t)}
                  pulse={status.state === "running"}
                />
              ) : null}
            </div>
          }
        />
      </div>

      {query.isPending ? (
        <div className="p-4">
          <LoadingSkeleton rows={2} />
        </div>
      ) : !status ? (
        <p className="text-muted-foreground px-4 py-3 text-xs">{t.system.unavailableBody}</p>
      ) : (
        <>
          <dl className="divide-border grid grid-cols-2 divide-x divide-y sm:grid-cols-4 sm:divide-y-0">
            <Figure
              label={t.simulation.vehiclesActive}
              value={formatNumber(status.vehicles_active, locale)}
            />
            <Figure
              label={t.system.eventsPerSecond}
              value={formatNumber(status.events_per_second, locale, { decimals: 1 })}
            />
            <Figure
              label={t.system.messagesProcessed}
              value={formatNumber(status.messages_processed, locale, {
                compact: status.messages_processed > 9999,
              })}
            />
            <Figure
              label={t.simulation.errors}
              value={formatNumber(status.errors, locale)}
              tone={status.errors > 0 ? "text-danger" : undefined}
            />
          </dl>

          <div className="border-border space-y-2 border-t px-4 py-3 text-xs">
            <div className="flex items-baseline justify-between gap-4">
              <span className="text-muted-foreground">{t.system.transport}</span>
              <span className="font-mono">
                {status.transport === "kafka"
                  ? t.simulation.transportKafka
                  : t.simulation.transportDatabase}
              </span>
            </div>
            {status.kafka ? (
              <>
                <div className="flex items-baseline justify-between gap-4">
                  <span className="text-muted-foreground">{t.simulation.broker}</span>
                  <span className="flex items-center gap-2">
                    <span className="truncate font-mono">{status.kafka.bootstrap_servers}</span>
                    <StatusBadge
                      status={status.kafka.connected ? "healthy" : "failed"}
                      label={
                        status.kafka.connected
                          ? t.system.kafkaConnected
                          : t.system.kafkaDisconnected
                      }
                    />
                  </span>
                </div>
                {status.kafka.topics.length > 0 ? (
                  <div className="flex items-baseline justify-between gap-4">
                    <span className="text-muted-foreground">{t.system.topics}</span>
                    <span className="truncate text-right font-mono">
                      {status.kafka.topics.join(", ")}
                    </span>
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        </>
      )}
    </Card>
  );
}

function Figure({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="min-w-0 px-4 py-3">
      <dt className="eyebrow mb-1 truncate">{label}</dt>
      <dd className={`font-mono text-sm font-medium tabular-nums ${tone ?? ""}`}>{value}</dd>
    </div>
  );
}
