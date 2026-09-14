"use client";

import { ArrowUpRight } from "lucide-react";
import Link from "next/link";

import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState } from "@/components/shared/states";
import { Skeleton } from "@/components/ui/skeleton";
import { formatNumber, formatPercent } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { DashboardSummary } from "@/types/domain";

import { CATEGORY_COLOR, CATEGORY_ORDER, categoryLabel } from "./labels";
import { Figure, Panel, PanelBody, PanelHeader, WrappingTitle } from "./panel";

/**
 * Question: how does the charging estate split across charging categories?
 *
 * Horizontal bars, not a pie: three shares read off a common baseline are comparable at a
 * glance and stay comparable when a fourth category appears, which is exactly what a pie chart
 * stops being. The bar carries the proportion, the mono figure carries the value, and the
 * category name carries the meaning — colour is the redundant channel here, not the message.
 */
export function ChargingMixCard({
  summary,
  loading,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const byCategory = summary?.charging_by_category ?? [];
  const counts = new Map(byCategory.map((entry) => [entry.category, entry.count]));
  const total = byCategory.reduce((sum, entry) => sum + entry.count, 0);

  return (
    <Panel>
      <PanelHeader>
        <SectionHeader
          eyebrow={t.overview.chargingStats}
          title={<WrappingTitle>{t.overview.chargingMixQuestion}</WrappingTitle>}
          actions={<SourceBadge origin="official" source="bundesnetzagentur" compact />}
        />
      </PanelHeader>
      <PanelBody className="space-y-3">
        {loading ? (
          <Skeleton className="h-24 w-full" />
        ) : total === 0 ? (
          <EmptyState className="py-6" title={t.common.noData} description={t.states.emptyBody} />
        ) : (
          <>
            <ul className="space-y-2.5">
              {CATEGORY_ORDER.map((category) => {
                const count = counts.get(category) ?? 0;
                const share = total > 0 ? (count / total) * 100 : 0;
                return (
                  <li key={category}>
                    <div className="mb-1 flex items-baseline justify-between gap-2">
                      <span className="text-foreground truncate text-xs">
                        {categoryLabel(category, t)}
                      </span>
                      <span className="flex shrink-0 items-baseline gap-2">
                        <Figure
                          className="text-foreground text-xs"
                          value={formatNumber(count, locale)}
                        />
                        <span className="text-muted-foreground w-14 text-right font-mono text-[0.6875rem] tabular-nums">
                          {formatPercent(share, locale, 1)}
                        </span>
                      </span>
                    </div>
                    <div
                      className="bg-muted h-1.5 w-full overflow-hidden rounded-[1px]"
                      role="img"
                      aria-label={`${categoryLabel(category, t)}: ${formatNumber(count, locale)} ${t.units.stations} (${formatPercent(share, locale, 1)})`}
                    >
                      <div
                        className="h-full transition-[width] duration-200 ease-out"
                        style={{
                          width: `${Math.max(share, share > 0 ? 1 : 0)}%`,
                          backgroundColor: CATEGORY_COLOR[category],
                        }}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
            <div className="border-border flex items-baseline justify-between gap-2 border-t pt-2.5">
              <span className="eyebrow">{t.charging.stations}</span>
              <Figure
                className="text-foreground text-sm font-medium"
                value={formatNumber(total, locale)}
                unit={t.units.stations}
              />
            </div>
            <Link
              href="/charging"
              className="text-primary focus-visible:ring-ring inline-flex items-center gap-1 rounded text-xs hover:underline focus-visible:ring-2 focus-visible:outline-none"
            >
              {t.overview.viewAll}
              <ArrowUpRight className="size-3" aria-hidden />
            </Link>
          </>
        )}
      </PanelBody>
    </Panel>
  );
}
