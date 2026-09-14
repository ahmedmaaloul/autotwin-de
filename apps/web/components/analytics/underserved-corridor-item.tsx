"use client";

import {
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { UnderservedCorridor } from "@/types/domain";

import { CoverageGapTable } from "./coverage-gap-table";
import { GapProfile } from "./gap-profile";

/**
 * One ranked corridor. Collapsed it answers "how bad, and where"; expanded it lists the
 * individual stretches a planner would have to close.
 */
export function UnderservedCorridorItem({
  corridor,
  rank,
  worstOverall,
}: {
  corridor: UnderservedCorridor;
  rank: number;
  /** The worst gap in the whole report — the bars are scaled against it, not against 100. */
  worstOverall: number;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const totalKm = corridor.segments.reduce(
    (max, segment) => Math.max(max, segment.end_offset_km),
    0,
  );
  const share = worstOverall > 0 ? (corridor.worst_gap_km / worstOverall) * 100 : 0;

  return (
    <AccordionItem value={corridor.route_slug} className="border-border border-b last:border-b-0">
      <AccordionTrigger className="items-center px-4 hover:no-underline">
        <div className="grid min-w-0 flex-1 grid-cols-[2rem_minmax(0,1fr)_auto] items-center gap-x-4 gap-y-2 pr-3 md:grid-cols-[2rem_minmax(0,14rem)_minmax(0,1fr)_auto]">
          <span className="text-muted-foreground font-mono text-sm tabular-nums">{rank}</span>

          <span className="min-w-0">
            <span className="block truncate text-sm font-medium">{corridor.name}</span>
            <span className="text-muted-foreground block truncate font-mono text-[0.6875rem]">
              {corridor.route_slug}
            </span>
          </span>

          <span className="col-span-3 md:col-span-1 md:px-2">
            <GapProfile segments={corridor.segments} totalKm={totalKm} />
          </span>

          <span className="col-start-3 row-start-1 flex items-baseline justify-end gap-1 md:col-start-4">
            <span className="text-danger font-mono text-lg leading-none font-medium tabular-nums">
              {formatNumber(corridor.worst_gap_km, locale, { decimals: 0 })}
            </span>
            <span className="text-muted-foreground text-xs">{t.units.km}</span>
          </span>
        </div>
      </AccordionTrigger>

      <AccordionContent className="px-4">
        <div className="mb-3 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Figure label={t.analytics.worstGap} value={formatNumber(corridor.worst_gap_km, locale, { decimals: 1 })} unit={t.units.km} />
          <Figure label={t.analytics.segments} value={formatNumber(corridor.segments.length, locale)} />
          <Figure
            label={t.analytics.gapLength}
            value={formatNumber(
              corridor.segments.reduce((sum, segment) => sum + segment.gap_km, 0),
              locale,
              { decimals: 0 },
            )}
            unit={t.units.km}
          />
          <div className="min-w-0">
            <p className="eyebrow mb-1.5">{t.analytics.rank}</p>
            <div className="bg-muted h-1 w-full overflow-hidden rounded-full">
              <div
                className="bg-danger h-full rounded-full transition-[width] duration-200 motion-reduce:transition-none"
                style={{ width: `${Math.max(2, Math.min(100, share))}%` }}
              />
            </div>
          </div>
        </div>

        <div className="border-border rounded border">
          <CoverageGapTable gaps={corridor.segments} />
        </div>
      </AccordionContent>
    </AccordionItem>
  );
}

function Figure({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="min-w-0">
      <p className="eyebrow mb-1">{label}</p>
      <p className="flex items-baseline gap-1">
        <span className="font-mono text-sm font-medium tabular-nums">{value}</span>
        {unit ? <span className="text-muted-foreground text-xs">{unit}</span> : null}
      </p>
    </div>
  );
}
