"use client";

import { ExternalLink } from "lucide-react";
import type { ReactNode } from "react";

import { DataFreshness } from "@/components/shared/data-freshness";
import { SourceBadge } from "@/components/shared/source-badge";
import { StatusBadge } from "@/components/shared/status-badge";
import { Card } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { SourceQuality } from "@/types/domain";

import { sourceTitle, statusTone, toPercent } from "./source-labels";

/**
 * One data source, end to end: is it healthy, when did it last answer, how much of what it sent
 * survived validation, and under which licence may any of it be used.
 *
 * The four row counts are the honest version of "data quality". A single green tick would hide
 * the case this platform actually hits — a source that answers, but whose payload half fails the
 * rules — so received, accepted, rejected and duplicate are always shown together, with the
 * acceptance rate as the summary rather than as a substitute (BUILD_SPEC §6).
 */
export function SourceQualityCard({ quality }: { quality: SourceQuality }) {
  const t = useTranslations();
  const locale = useLocale();

  const acceptance = toPercent(quality.acceptance_rate);
  const mode = quality.provider_mode;

  return (
    <Card className="gap-0 rounded border p-0 shadow-none">
      <div className="border-border flex items-start justify-between gap-3 border-b px-4 py-3">
        <div className="min-w-0">
          <p className="eyebrow section-tick mb-1">{t.dataQuality.source}</p>
          <h3 className="truncate text-sm font-semibold tracking-tight">
            {sourceTitle(quality.source)}
          </h3>
          <div className="mt-1.5">
            <SourceBadge origin={quality.data_origin} compact />
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <StatusBadge status={statusTone(quality.status)} />
          <DataFreshness timestamp={quality.last_run_at} />
        </div>
      </div>

      <dl className="divide-border grid grid-cols-2 divide-x divide-y sm:grid-cols-4 sm:divide-y-0">
        <Count label={t.dataQuality.rowsReceived} value={quality.rows_received} />
        <Count label={t.dataQuality.rowsAccepted} value={quality.rows_accepted} tone="text-success" />
        <Count
          label={t.dataQuality.rowsRejected}
          value={quality.rows_rejected}
          tone={quality.rows_rejected > 0 ? "text-danger" : undefined}
        />
        <Count label={t.dataQuality.duplicates} value={quality.rows_duplicate} />
      </dl>

      <div className="border-border border-t px-4 py-3">
        <div className="mb-1.5 flex items-baseline justify-between gap-3">
          <span className="eyebrow">{t.dataQuality.acceptanceRate}</span>
          <span className="flex items-baseline gap-1">
            <span className="font-mono text-sm font-medium tabular-nums">
              {acceptance == null ? "–" : formatNumber(acceptance, locale, { decimals: 1 })}
            </span>
            <span className="text-muted-foreground text-xs">{t.units.percent}</span>
          </span>
        </div>
        <Progress value={acceptance ?? 0} />
      </div>

      <dl className="border-border space-y-2 border-t px-4 py-3 text-xs">
        <Row label={t.dataQuality.mode}>
          {mode ? (
            <span className="font-mono">{t.provenance.dataMode[mode]}</span>
          ) : (
            <span className="text-muted-foreground">{t.common.unknown}</span>
          )}
        </Row>
        <Row label={t.dataQuality.licence}>
          <span className="font-mono">{quality.licence ?? t.common.unknown}</span>
        </Row>
        <Row label={t.dataQuality.attribution}>
          <span className="text-right">{quality.attribution ?? "–"}</span>
        </Row>
        {quality.source_url ? (
          <Row label={t.common.source}>
            <a
              href={quality.source_url}
              target="_blank"
              rel="noreferrer noopener"
              className="text-primary inline-flex items-center gap-1 underline underline-offset-3"
            >
              {t.dataQuality.openSource}
              <ExternalLink className="size-3" aria-hidden />
            </a>
          </Row>
        ) : null}
      </dl>
    </Card>
  );
}

function Count({ label, value, tone }: { label: string; value: number; tone?: string }) {
  const locale = useLocale();
  return (
    <div className="min-w-0 px-4 py-3">
      <dt className="eyebrow mb-1 truncate">{label}</dt>
      <dd className={`font-mono text-sm font-medium tabular-nums ${tone ?? ""}`}>
        {formatNumber(value, locale)}
      </dd>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <dt className="text-muted-foreground shrink-0">{label}</dt>
      <dd className="min-w-0 text-right">{children}</dd>
    </div>
  );
}
