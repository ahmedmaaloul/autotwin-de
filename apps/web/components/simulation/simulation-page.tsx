"use client";

import Link from "next/link";
import { useState } from "react";

import { PageHeader, SectionHeader } from "@/components/shared/section-header";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useSimulations, useSimulatorStatus } from "@/hooks/use-autotwin";
import { useTranslations } from "@/providers/locale-provider";

import { DEFAULT_CONFIG, type SimulationConfig } from "./config";
import { RunControls } from "./run-controls";
import { RunsTable } from "./runs-table";
import { SimulationControls } from "./simulation-controls";
import { StatusBoard } from "./status-board";
import { ThroughputChart } from "./throughput-chart";
import { TransportCard } from "./transport-card";
import { useSampleHistory } from "./use-sample-history";

/**
 * The control centre for the simulator.
 *
 * The page holds the form and one piece of bookkeeping — the id of the run this browser just
 * created — and nothing else. The simulator's own status is the authority on what is running,
 * so `run_id` from the status poll wins over the local value as soon as the backend reports
 * one; the local id only covers the seconds between creating a run and the next poll.
 *
 * The parameter form is disabled while a run is live. Editing the seed of a simulation that is
 * already producing telemetry would describe a run that does not exist.
 */
export function SimulationPage() {
  const t = useTranslations();

  const [config, setConfig] = useState<SimulationConfig>(DEFAULT_CONFIG);
  const [createdRunId, setCreatedRunId] = useState<string | null>(null);

  const statusQuery = useSimulatorStatus();
  const runsQuery = useSimulations();

  const status = statusQuery.data;
  const state = status?.state ?? null;
  const activeRunId = status?.run_id ?? createdRunId;
  const runs = runsQuery.data?.items ?? [];
  const activeRun = runs.find((run) => run.id === activeRunId) ?? null;

  // Derived UI state: the API reports an instantaneous rate, the history is ours.
  const samples = useSampleHistory(status?.events_per_second);

  const live = state === "running" || state === "paused" || state === "stopping";

  return (
    <div className="space-y-6">
      <PageHeader
        title={t.simulation.title}
        description={t.simulation.subtitle}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/live">{t.simulation.openLive}</Link>
          </Button>
        }
      />

      <StatusBoard
        status={status}
        isPending={statusQuery.isPending}
        isError={statusQuery.isError}
        error={statusQuery.error}
        onRetry={() => void statusQuery.refetch()}
      />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_23rem]">
        <div className="min-w-0 space-y-6">
          <section className="space-y-3">
            <SectionHeader
              eyebrow={t.simulation.throughputTitle}
              title={t.simulation.throughputQuestion}
              actions={
                <span className="flex items-baseline gap-1">
                  <span className="text-foreground font-mono text-sm tabular-nums">
                    {samples.length}
                  </span>
                  <span className="text-muted-foreground text-xs">{t.simulation.samples}</span>
                </span>
              }
            />
            <ThroughputChart samples={samples} />
          </section>

          <TransportCard status={status} />

          <section className="space-y-3">
            <SectionHeader eyebrow={t.simulation.runs} title={t.simulation.runsSubtitle} />
            <RunsTable
              runs={runs}
              isPending={runsQuery.isPending}
              isError={runsQuery.isError}
              error={runsQuery.error}
              onRetry={() => void runsQuery.refetch()}
              activeRunId={activeRunId}
            />
          </section>
        </div>

        <aside className="min-w-0 space-y-4">
          <Card className="gap-0 rounded border p-0 shadow-none">
            <div className="space-y-3 px-4 py-3">
              <RunControls
                config={config}
                state={state}
                activeRun={activeRun}
                activeRunId={activeRunId}
                onRunCreated={setCreatedRunId}
              />
              {activeRunId ? null : (
                <p className="text-muted-foreground text-xs">{t.simulation.noActiveRunBody}</p>
              )}
            </div>
          </Card>

          <SimulationControls config={config} onChange={setConfig} disabled={live} />
        </aside>
      </div>
    </div>
  );
}
