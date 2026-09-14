"use client";

import { SectionHeader, PageHeader } from "@/components/shared/section-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useDataQuality } from "@/hooks/use-autotwin";
import { useTranslations } from "@/providers/locale-provider";

import { IngestionRunTable } from "./ingestion-run-table";
import { SourceQualityCard } from "./source-quality-card";

/**
 * Where the data came from, how healthy it is, and what happened on every run that fetched it.
 *
 * Two halves that belong together: the per-source cards are the current state, the run table is
 * the history that produced it. The filter list for the table is derived from the same response
 * as the cards, so the two can never disagree about which sources exist.
 */
export function DataQualityPage() {
  const t = useTranslations();
  const query = useDataQuality();

  const sources = query.data ?? [];

  return (
    <div className="space-y-6">
      <PageHeader title={t.dataQuality.title} description={t.dataQuality.subtitle} />

      <section className="space-y-3">
        <SectionHeader
          eyebrow={t.dataQuality.status}
          title={t.dataQuality.sources}
          description={t.dataQuality.sourcesSubtitle}
        />

        {query.isPending ? <LoadingSkeleton rows={4} /> : null}

        {query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : null}

        {!query.isPending && !query.isError && sources.length === 0 ? (
          <EmptyState title={t.dataQuality.noSources} />
        ) : null}

        {sources.length > 0 ? (
          <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-4">
            {sources.map((quality) => (
              <SourceQualityCard key={quality.source} quality={quality} />
            ))}
          </div>
        ) : null}
      </section>

      <IngestionRunTable sources={sources.map((quality) => quality.source)} />
    </div>
  );
}
