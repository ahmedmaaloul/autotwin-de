"use client";

import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { StatusBadge } from "@/components/shared/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatDateTime, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { SimulationRun } from "@/types/domain";

import { simulationLabel, simulationTone } from "./state-labels";

/**
 * The last twenty runs.
 *
 * The seed column is the reason this table matters: together with the vehicle count and speed
 * factor it is the complete description of a run, so a result that looks wrong can be
 * reproduced exactly rather than argued about.
 */
export function RunsTable({
  runs,
  isPending,
  isError,
  error,
  onRetry,
  activeRunId,
}: {
  runs: SimulationRun[];
  isPending: boolean;
  isError: boolean;
  error: unknown;
  onRetry: () => void;
  activeRunId: string | null;
}) {
  const t = useTranslations();
  const locale = useLocale();

  if (isPending) return <LoadingSkeleton rows={4} />;
  if (isError) return <ErrorState error={error} onRetry={onRetry} />;
  if (runs.length === 0) {
    return <EmptyState title={t.simulation.noRuns} description={t.simulation.noRunsBody} />;
  }

  return (
    <div className="border-border overflow-x-auto rounded border">
      <Table>
        <TableHeader className="bg-card sticky top-0 z-10">
          <TableRow>
            <TableHead className="eyebrow">{t.simulation.runName}</TableHead>
            <TableHead className="eyebrow">{t.simulation.state}</TableHead>
            <TableHead className="eyebrow text-right">{t.simulation.vehicleCount}</TableHead>
            <TableHead className="eyebrow text-right">{t.simulation.speedFactor}</TableHead>
            <TableHead className="eyebrow text-right">{t.simulation.seed}</TableHead>
            <TableHead className="eyebrow">{t.simulation.startedAt}</TableHead>
            <TableHead className="eyebrow">{t.simulation.stoppedAt}</TableHead>
            <TableHead className="eyebrow text-right">{t.simulation.eventsEmitted}</TableHead>
            <TableHead className="eyebrow text-right">{t.simulation.errors}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((run) => (
            <TableRow
              key={run.id}
              className={cn("h-9", run.id === activeRunId && "bg-muted/60")}
              aria-current={run.id === activeRunId ? "true" : undefined}
            >
              <TableCell className="max-w-48 truncate font-medium">{run.name}</TableCell>
              <TableCell>
                <StatusBadge
                  status={simulationTone(run.state)}
                  label={simulationLabel(run.state, t)}
                />
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(run.vehicle_count, locale)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(run.speed_factor, locale)}×
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(run.seed, locale)}
              </TableCell>
              <TableCell className="text-muted-foreground font-mono text-xs whitespace-nowrap">
                {formatDateTime(run.started_at, locale)}
              </TableCell>
              <TableCell className="text-muted-foreground font-mono text-xs whitespace-nowrap">
                {formatDateTime(run.stopped_at, locale)}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(run.events_emitted, locale)}
              </TableCell>
              <TableCell
                className={cn(
                  "text-right font-mono tabular-nums",
                  run.errors > 0 && "text-danger",
                )}
              >
                {formatNumber(run.errors, locale)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
