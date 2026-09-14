"use client";

import { CircleCheck } from "lucide-react";
import { useState } from "react";

import { DataFreshness } from "@/components/shared/data-freshness";
import { SectionHeader } from "@/components/shared/section-header";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Accordion } from "@/components/ui/accordion";
import { Card } from "@/components/ui/card";
import { useUnderserved } from "@/hooks/use-autotwin";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { MethodologyNote } from "./methodology-note";
import { ControlPanel, KmSlider, PowerSelect } from "./parameter-controls";
import { UnderservedCorridorItem } from "./underserved-corridor-item";

/**
 * Which corridors are worst served, under parameters the planner sets.
 *
 * This is the screen an infrastructure team would actually work from, so the ranking is only
 * half of it: the thresholds that produced the ranking are visible and adjustable, and the
 * report's own `methodology` sentence is shown with the parameters it was computed under. A
 * ranking whose assumptions are hidden is a leaderboard, not an analysis.
 */
export function UnderservedPanel() {
  const t = useTranslations();
  const locale = useLocale();

  const [minPowerKw, setMinPowerKw] = useState(150);
  const [maxGapKm, setMaxGapKm] = useState(50);
  const [bufferKm, setBufferKm] = useState(5);

  const query = useUnderserved({
    min_power_kw: minPowerKw,
    max_gap_km: maxGapKm,
    corridor_buffer_km: bufferKm,
  });

  const report = query.data;
  const corridors = report?.corridors ?? [];
  const worstOverall = corridors.reduce(
    (max, corridor) => Math.max(max, corridor.worst_gap_km),
    0,
  );

  return (
    <div className="space-y-4">
      <ControlPanel title={t.analytics.parameters}>
        <PowerSelect
          label={t.analytics.minPowerThreshold}
          value={minPowerKw}
          onChange={setMinPowerKw}
        />
        <KmSlider
          label={t.analytics.maxGapThreshold}
          value={maxGapKm}
          onChange={setMaxGapKm}
          min={10}
          max={150}
          step={5}
          unit={t.units.km}
        />
        <KmSlider
          label={t.analytics.corridorBuffer}
          value={bufferKm}
          onChange={setBufferKm}
          min={1}
          max={25}
          step={0.5}
          decimals={1}
          unit={t.units.km}
        />
      </ControlPanel>

      {query.isPending ? <LoadingSkeleton rows={5} /> : null}

      {query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : null}

      {report ? (
        <>
          <MethodologyNote
            text={report.methodology}
            meta={
              <span className="text-muted-foreground flex items-center gap-2 font-mono text-[0.6875rem] tabular-nums">
                <span>
                  {formatNumber(report.parameters.min_power_kw, locale)} {t.units.kw}
                </span>
                <span aria-hidden>·</span>
                <span>
                  {formatNumber(report.parameters.max_gap_km, locale)} {t.units.km}
                </span>
                <span aria-hidden>·</span>
                <span>
                  {formatNumber(report.parameters.corridor_buffer_km, locale, { decimals: 1 })}{" "}
                  {t.units.km}
                </span>
              </span>
            }
          />

          <Card className="gap-0 rounded border p-0 shadow-none">
            <div className="border-border flex items-center justify-between gap-4 border-b px-4 py-3">
              <SectionHeader
                eyebrow={t.analytics.underserved}
                title={t.analytics.corridors}
                description={t.analytics.underservedSubtitle}
              />
              <div className="flex shrink-0 flex-col items-end gap-1">
                <span className="flex items-baseline gap-1">
                  <span className="font-mono text-lg leading-none font-medium tabular-nums">
                    {formatNumber(corridors.length, locale)}
                  </span>
                  <span className="text-muted-foreground text-xs">{t.analytics.corridors}</span>
                </span>
                <DataFreshness timestamp={report.generated_at} />
              </div>
            </div>

            {corridors.length === 0 ? (
              <div className="p-4">
                <EmptyState
                  icon={CircleCheck}
                  title={t.analytics.noUnderserved}
                  description={t.analytics.noUnderservedBody}
                />
              </div>
            ) : (
              <Accordion type="multiple">
                {corridors
                  .slice()
                  .sort((a, b) => b.worst_gap_km - a.worst_gap_km)
                  .map((corridor, index) => (
                    <UnderservedCorridorItem
                      key={corridor.route_slug}
                      corridor={corridor}
                      rank={index + 1}
                      worstOverall={worstOverall}
                    />
                  ))}
              </Accordion>
            )}
          </Card>
        </>
      ) : null}
    </div>
  );
}
