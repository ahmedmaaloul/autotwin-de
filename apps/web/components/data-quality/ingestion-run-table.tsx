"use client";

import { useId, useState } from "react";

import { SectionHeader } from "@/components/shared/section-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Table, TableBody, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useIngestionRuns } from "@/hooks/use-autotwin";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { IngestionRunRow } from "./ingestion-run-row";
import { sourceTitle } from "./source-labels";

const PAGE_SIZE = 20;
const ALL_SOURCES = "__all__";

/**
 * The ingestion log.
 *
 * This table is where the platform stops claiming to be a data platform and shows it: every run
 * of every pipeline, with the mode it ran in, what it received, what survived, and — one click
 * down — which rule rejected what. One row expands at a time, because the point of expanding is
 * to read the detail, not to compare six reports at once.
 */
export function IngestionRunTable({ sources }: { sources: string[] }) {
  const t = useTranslations();
  const locale = useLocale();
  const tableId = useId();

  const [source, setSource] = useState(ALL_SOURCES);
  const [page, setPage] = useState(1);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const query = useIngestionRuns({
    source: source === ALL_SOURCES ? undefined : source,
    page,
    page_size: PAGE_SIZE,
  });

  const runs = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const hasNext = query.data?.has_next ?? false;

  return (
    <Card className="gap-0 rounded border p-0 shadow-none">
      <div className="border-border flex flex-wrap items-end justify-between gap-3 border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.dataQuality.runs}
          title={t.dataQuality.runs}
          description={t.dataQuality.runsSubtitle}
        />
        <Select
          value={source}
          onValueChange={(next) => {
            setSource(next);
            setPage(1);
            setExpandedId(null);
          }}
        >
          <SelectTrigger aria-label={t.dataQuality.filterBySource} className="min-w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_SOURCES}>{t.dataQuality.allSources}</SelectItem>
            {sources.map((entry) => (
              <SelectItem key={entry} value={entry}>
                {sourceTitle(entry)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {query.isPending ? (
        <div className="p-4">
          <LoadingSkeleton rows={6} />
        </div>
      ) : query.isError ? (
        <div className="p-4">
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </div>
      ) : runs.length === 0 ? (
        <div className="p-4">
          <EmptyState title={t.dataQuality.noRuns} description={t.dataQuality.noRunsBody} />
        </div>
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-9" />
                <TableHead>{t.dataQuality.source}</TableHead>
                <TableHead>{t.dataQuality.pipeline}</TableHead>
                <TableHead>{t.dataQuality.started}</TableHead>
                <TableHead className="text-right">{t.dataQuality.duration}</TableHead>
                <TableHead>{t.dataQuality.status}</TableHead>
                <TableHead className="text-right">{t.dataQuality.rows}</TableHead>
                <TableHead>{t.dataQuality.mode}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <IngestionRunRow
                  key={run.id}
                  run={run}
                  isExpanded={expandedId === run.id}
                  detailsId={`${tableId}-${run.id}`}
                  onToggle={() => setExpandedId(expandedId === run.id ? null : run.id)}
                />
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <div className="border-border text-muted-foreground flex items-center justify-between gap-3 border-t px-4 py-2 text-xs">
        <span className="font-mono tabular-nums">
          {t.common.page} {formatNumber(page, locale)}
          {total > 0 ? (
            <>
              {" · "}
              {formatNumber(total, locale)} {t.dataQuality.runs}
            </>
          ) : null}
        </span>
        <span className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={page <= 1 || query.isPending}
            onClick={() => {
              setPage((current) => Math.max(1, current - 1));
              setExpandedId(null);
            }}
          >
            {t.dataQuality.previous}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!hasNext || query.isPending}
            onClick={() => {
              setPage((current) => current + 1);
              setExpandedId(null);
            }}
          >
            {t.dataQuality.next}
          </Button>
        </span>
      </div>
    </Card>
  );
}
