"use client";

import { ExternalLink, FileSearch } from "lucide-react";

import { SourceBadge } from "@/components/shared/source-badge";
import { formatDateTime } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { Provenance } from "@/types/domain";

/**
 * Where this record came from.
 *
 * This is the block that makes ADR 004 visible to a person instead of to a code reviewer. A
 * charging station in AutoTwin DE is not a number someone typed: it is a row from a named
 * register, pulled by a named ingestion run, at a knowable moment — and every one of those
 * four facts is shown, with the source document one click away.
 *
 * It is deliberately styled as a finished panel rather than a debug dump: hairline frame, the
 * same eyebrow and mono treatment as the rest of the page, and the badge first, because the
 * first question is always "is this real or simulated?".
 */
export function StationProvenance({ provenance }: { provenance: Provenance }) {
  const t = useTranslations();
  const locale = useLocale();

  const rows: { label: string; value: string | null; mono?: boolean }[] = [
    { label: t.common.source, value: provenance.source },
    { label: t.charging.sourceIdentifier, value: provenance.source_identifier ?? null, mono: true },
    {
      label: t.charging.sourceTimestamp,
      value: provenance.source_timestamp
        ? formatDateTime(provenance.source_timestamp, locale, { dateStyle: "medium" })
        : null,
      mono: true,
    },
    { label: t.charging.ingestionRun, value: provenance.ingestion_run_id ?? null, mono: true },
    {
      label: t.charging.ingestedAt,
      value: provenance.ingested_at
        ? formatDateTime(provenance.ingested_at, locale, {
            dateStyle: "medium",
            timeStyle: "short",
          })
        : null,
      mono: true,
    },
  ];

  return (
    <section
      aria-label={t.charging.provenanceTitle}
      className="border-border bg-muted/30 rounded border"
    >
      <header className="border-border flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
        <p className="eyebrow section-tick inline-flex items-center gap-1.5">
          <FileSearch className="size-3.5" aria-hidden />
          {t.charging.provenanceTitle}
        </p>
        <SourceBadge origin={provenance.data_origin} source={provenance.source} />
      </header>

      <dl className="divide-border divide-y">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-3 px-3 py-1.5">
            <dt className="text-muted-foreground shrink-0 text-xs">{row.label}</dt>
            <dd
              className={
                row.mono
                  ? "truncate font-mono text-[0.75rem] tabular-nums"
                  : "truncate text-xs font-medium"
              }
              title={row.value ?? undefined}
            >
              {row.value ?? "–"}
            </dd>
          </div>
        ))}
      </dl>

      <footer className="border-border space-y-2 border-t px-3 py-2">
        {provenance.source_url ? (
          <a
            href={provenance.source_url}
            target="_blank"
            rel="noreferrer noopener"
            className="text-primary focus-visible:ring-ring inline-flex items-center gap-1.5 rounded-sm text-xs hover:underline focus-visible:ring-2 focus-visible:outline-none"
          >
            <ExternalLink className="size-3.5" aria-hidden />
            {t.charging.openSource}
          </a>
        ) : (
          <span className="text-muted-foreground text-xs">{t.charging.sourceUrl}: –</span>
        )}
        <p className="text-muted-foreground text-[0.6875rem] leading-relaxed">
          {t.charging.provenanceNote}
        </p>
      </footer>
    </section>
  );
}
