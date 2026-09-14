"use client";

import { ChevronRight } from "lucide-react";

import { SectionHeader } from "@/components/shared/section-header";
import { StatusBadge } from "@/components/shared/status-badge";
import { Card } from "@/components/ui/card";
import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { SimulatorStatus } from "@/types/domain";

/**
 * How a telemetry event gets from the simulator to this browser.
 *
 * The transport is swappable by configuration — with `KAFKA_ENABLED=false` the simulator writes
 * to PostgreSQL through the same `TelemetrySink` interface (BUILD_SPEC §8) — so the diagram
 * shows the full path either way and dims the two stages that are bypassed. Hiding them would
 * make the direct-write mode look like a different architecture instead of the same one with a
 * shorter transport.
 */
export function TransportCard({
  status,
  className,
}: {
  status: SimulatorStatus | undefined;
  className?: string;
}) {
  const t = useTranslations();

  // Until the simulator has answered, the transport is unknown — and saying "direct database
  // writes" because a field is undefined would be a claim about the architecture that no data
  // supports. Unknown is drawn as unknown: every stage dashed, no transport named.
  const known = status != null;
  const kafka = status?.transport === "kafka";
  const connected = status?.kafka?.connected ?? false;
  const topics = status?.kafka?.topics ?? [];

  const stages = [
    { key: "simulator", label: t.simulation.nodeSimulator, active: known },
    { key: "broker", label: t.simulation.nodeBroker, active: known && kafka },
    { key: "consumer", label: t.simulation.nodeConsumer, active: known && kafka },
    { key: "database", label: t.simulation.nodeDatabase, active: known },
    { key: "stream", label: t.simulation.nodeStream, active: known },
    { key: "browser", label: t.simulation.nodeBrowser, active: known },
  ];

  return (
    <Card className={cn("gap-0 rounded border p-0 shadow-none", className)}>
      <div className="border-border flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.simulation.architecture}
          title={
            !known
              ? t.common.unknown
              : kafka
                ? t.simulation.transportKafka
                : t.simulation.transportDatabase
          }
        />
        {kafka ? (
          <StatusBadge
            status={connected ? "healthy" : "failed"}
            label={connected ? t.simulation.kafkaConnected : t.simulation.kafkaDisconnected}
          />
        ) : null}
      </div>

      <div className="space-y-3 px-4 py-3">
        <ol className="flex flex-wrap items-center gap-1.5">
          {stages.map((stage, index) => (
            <li key={stage.key} className="flex items-center gap-1.5">
              {index > 0 ? (
                <ChevronRight className="text-muted-foreground size-3.5 shrink-0" aria-hidden />
              ) : null}
              <span
                className={cn(
                  "rounded border px-2 py-1 font-mono text-[0.6875rem] whitespace-nowrap",
                  stage.active
                    ? "border-border bg-muted text-foreground"
                    : "border-border text-muted-foreground border-dashed opacity-60",
                )}
              >
                {stage.label}
              </span>
            </li>
          ))}
        </ol>

        {kafka ? (
          <dl className="space-y-2">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <dt className="eyebrow">{t.simulation.broker}</dt>
              <dd className="text-foreground font-mono text-xs break-all">
                {status?.kafka?.bootstrap_servers ?? "–"}
              </dd>
            </div>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <dt className="eyebrow">{t.simulation.topics}</dt>
              <dd className="flex flex-wrap gap-1">
                {topics.length === 0 ? (
                  <span className="text-muted-foreground text-xs">{t.common.none}</span>
                ) : (
                  topics.map((topic) => (
                    <span
                      key={topic}
                      className="border-border text-foreground rounded border px-1.5 py-0.5 font-mono text-[0.6875rem]"
                    >
                      {topic}
                    </span>
                  ))
                )}
              </dd>
            </div>
          </dl>
        ) : null}

        <p className="text-muted-foreground text-xs">{t.simulation.architectureHint}</p>
      </div>
    </Card>
  );
}
