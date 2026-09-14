"use client";

import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment } from "react";

import { StatusBadge } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { TableCell, TableRow } from "@/components/ui/table";
import { formatDateTime, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { IngestionRun } from "@/types/domain";
import { cn } from "@/lib/utils";

import { IngestionRunDetails } from "./ingestion-run-details";
import { outcomeTone, sourceTitle } from "./source-labels";

/** Seconds below a minute and a half read better as seconds; above it, as minutes. */
function elapsedSeconds(run: IngestionRun): number | null {
  if (!run.finished_at) return null;
  const seconds = (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000;
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

export const RUN_COLUMN_COUNT = 8;

export function IngestionRunRow({
  run,
  isExpanded,
  onToggle,
  detailsId,
}: {
  run: IngestionRun;
  isExpanded: boolean;
  onToggle: () => void;
  detailsId: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const seconds = elapsedSeconds(run);
  const duration =
    seconds == null
      ? { value: "–", unit: "" }
      : seconds < 90
        ? {
            value: formatNumber(seconds, locale, { decimals: 1 }),
            unit: t.dataQuality.secondsUnit,
          }
        : { value: formatNumber(seconds / 60, locale, { decimals: 1 }), unit: t.units.minutes };

  const outcomeLabel = {
    success: t.dataQuality.outcomeSuccess,
    partial: t.dataQuality.outcomePartial,
    failed: t.dataQuality.outcomeFailed,
  }[run.status];

  return (
    <Fragment>
      <TableRow className={cn(isExpanded && "bg-muted/50")}>
        <TableCell>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7"
            aria-expanded={isExpanded}
            aria-controls={detailsId}
            aria-label={isExpanded ? t.dataQuality.hideDetails : t.dataQuality.showDetails}
            onClick={onToggle}
          >
            {isExpanded ? (
              <ChevronDown className="size-3.5" aria-hidden />
            ) : (
              <ChevronRight className="size-3.5" aria-hidden />
            )}
          </Button>
        </TableCell>
        <TableCell className="font-medium whitespace-nowrap">{sourceTitle(run.source)}</TableCell>
        <TableCell className="text-muted-foreground font-mono text-xs">{run.pipeline}</TableCell>
        <TableCell className="font-mono text-xs whitespace-nowrap tabular-nums">
          {formatDateTime(run.started_at, locale, { dateStyle: "short", timeStyle: "medium" })}
        </TableCell>
        <TableCell className="text-right whitespace-nowrap">
          <span className="font-mono tabular-nums">{duration.value}</span>
          {duration.unit ? (
            <span className="text-muted-foreground ml-1 text-xs">{duration.unit}</span>
          ) : null}
        </TableCell>
        <TableCell>
          <StatusBadge status={outcomeTone(run.status)} label={outcomeLabel} />
        </TableCell>
        <TableCell className="text-right whitespace-nowrap">
          <span className="font-mono tabular-nums">{formatNumber(run.rows_accepted, locale)}</span>
          <span className="text-muted-foreground font-mono text-xs tabular-nums">
            {" / "}
            {formatNumber(run.rows_received, locale)}
          </span>
        </TableCell>
        <TableCell className="font-mono text-xs">
          {t.provenance.dataMode[run.provider_mode]}
        </TableCell>
      </TableRow>

      {isExpanded ? (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={RUN_COLUMN_COUNT} className="p-0" id={detailsId}>
            <IngestionRunDetails run={run} />
          </TableCell>
        </TableRow>
      ) : null}
    </Fragment>
  );
}
