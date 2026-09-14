"use client";

import { ArrowUpRight } from "lucide-react";
import Link from "next/link";

import { DataFreshness } from "@/components/shared/data-freshness";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState, LoadingSkeleton } from "@/components/shared/states";
import { StatusBadge } from "@/components/shared/status-badge";
import { useTranslations } from "@/providers/locale-provider";
import type { DashboardSummary } from "@/types/domain";

import { ingestionLabel, ingestionTone, providerModeLabel, sourceLabel } from "./labels";
import { Panel, PanelBody, PanelHeader, WrappingTitle } from "./panel";

/**
 * The honesty panel — a status board for the provider chain.
 *
 * Every row states four things the reader is entitled to before believing any number on this
 * page: which authority the data came from, whether the last ingestion succeeded, how old it
 * is, and whether it was answered live or from cache/fixture (ADR 004, ADR 005). It is a real
 * table because it is real tabular data, and because a screen reader should be able to say
 * "Bundesnetzagentur, Status, Betriebsbereit".
 */
export function DataSourcesPanel({
  summary,
  loading,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
}) {
  const t = useTranslations();
  const sources = summary?.data_freshness ?? [];

  return (
    <Panel>
      <PanelHeader>
        <SectionHeader
          eyebrow={t.overview.dataSources}
          title={<WrappingTitle>{t.overview.dataSourcesQuestion}</WrappingTitle>}
          actions={
            <Link
              href="/data-quality"
              className="text-primary focus-visible:ring-ring inline-flex items-center gap-1 rounded text-xs hover:underline focus-visible:ring-2 focus-visible:outline-none"
            >
              {t.overview.viewAll}
              <ArrowUpRight className="size-3" aria-hidden />
            </Link>
          }
        />
      </PanelHeader>

      {loading ? (
        <PanelBody>
          <LoadingSkeleton rows={5} />
        </PanelBody>
      ) : sources.length === 0 ? (
        <PanelBody>
          <EmptyState className="py-8" title={t.common.noData} description={t.states.emptyBody} />
        </PanelBody>
      ) : (
        <div className="scrollbar-thin min-w-0 overflow-x-auto">
          <table className="w-full min-w-[34rem] border-collapse text-left">
            <caption className="sr-only">{t.overview.dataSources}</caption>
            <thead>
              <tr className="border-border border-b">
                <th scope="col" className="eyebrow px-4 py-2 font-semibold">
                  {t.dataQuality.source}
                </th>
                <th scope="col" className="eyebrow px-2 py-2 font-semibold">
                  {t.dataQuality.status}
                </th>
                <th scope="col" className="eyebrow px-2 py-2 font-semibold">
                  {t.common.source}
                </th>
                <th scope="col" className="eyebrow px-2 py-2 font-semibold">
                  {t.dataQuality.freshness}
                </th>
                <th scope="col" className="eyebrow px-4 py-2 text-right font-semibold">
                  {t.dataQuality.mode}
                </th>
              </tr>
            </thead>
            <tbody>
              {sources.map((source) => (
                <tr key={source.source} className="border-border border-b last:border-b-0">
                  <th
                    scope="row"
                    className="text-foreground px-4 py-2 font-mono text-xs font-medium whitespace-nowrap"
                  >
                    {sourceLabel(source.source)}
                  </th>
                  <td className="px-2 py-2">
                    <StatusBadge
                      status={ingestionTone(source.status)}
                      label={ingestionLabel(source.status, t)}
                    />
                  </td>
                  <td className="px-2 py-2">
                    <SourceBadge origin={source.data_origin} compact />
                  </td>
                  <td className="px-2 py-2 whitespace-nowrap">
                    <DataFreshness timestamp={source.last_run_at} showIcon={false} />
                  </td>
                  <td className="text-muted-foreground px-4 py-2 text-right font-mono text-[0.6875rem] whitespace-nowrap">
                    {providerModeLabel(source.mode, t)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
