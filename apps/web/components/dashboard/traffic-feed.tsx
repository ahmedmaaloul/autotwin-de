"use client";

import { Ban, Clock3 } from "lucide-react";

import { DataFreshness } from "@/components/shared/data-freshness";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState, LoadingSkeleton } from "@/components/shared/states";
import { StatusBadge } from "@/components/shared/status-badge";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { DashboardSummary, TrafficEvent } from "@/types/domain";

import { eventTypeLabel, severityLabel, severityTone } from "./labels";
import { Panel, PanelBody, PanelHeader, WrappingTitle } from "./panel";

function TrafficRow({ event }: { event: TrafficEvent }) {
  const t = useTranslations();
  const locale = useLocale();

  return (
    <li className="border-border flex flex-col gap-1 border-b px-4 py-2.5 last:border-b-0">
      <div className="flex items-center gap-2">
        <StatusBadge
          status={severityTone(event.severity)}
          label={severityLabel(event.severity, t)}
        />
        {event.road_name ? (
          <span className="text-foreground shrink-0 font-mono text-xs font-medium">
            {event.road_name}
          </span>
        ) : null}
        <span className="text-muted-foreground truncate text-xs">
          {eventTypeLabel(event.event_type, t)}
        </span>
        <DataFreshness
          timestamp={event.starts_at}
          className="ml-auto shrink-0"
          showIcon={false}
        />
      </div>
      <p className="text-foreground truncate text-xs" title={event.title}>
        {event.title}
      </p>
      {event.is_blocked || event.delay_minutes != null ? (
        <div className="text-muted-foreground flex items-center gap-3 text-[0.6875rem]">
          {event.is_blocked ? (
            <span className="text-danger inline-flex items-center gap-1">
              <Ban className="size-3" aria-hidden />
              {t.overview.blocked}
            </span>
          ) : null}
          {event.delay_minutes != null ? (
            <span className="inline-flex items-center gap-1">
              <Clock3 className="size-3" aria-hidden />
              {t.overview.delayMinutes}
              <span className="text-foreground font-mono tabular-nums">
                {formatNumber(event.delay_minutes, locale)}
              </span>
              <span>{t.units.minutes}</span>
            </span>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

/**
 * The five most recent disruptions, as the dashboard's answer to "is anything on fire?".
 *
 * Road number in mono because it is an identifier a reader matches against a sign, severity as
 * a badge that states its own word, and the age relative — a Baustelle reported four hours ago
 * is a different fact from one reported four minutes ago.
 */
export function TrafficFeed({
  summary,
  loading,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
}) {
  const t = useTranslations();
  const events = summary?.recent_traffic ?? [];

  return (
    <Panel>
      <PanelHeader>
        <SectionHeader
          eyebrow={t.overview.recentTraffic}
          title={<WrappingTitle>{t.overview.trafficQuestion}</WrappingTitle>}
          actions={<SourceBadge origin="official" source="autobahn" compact />}
        />
      </PanelHeader>
      {loading ? (
        <PanelBody>
          <LoadingSkeleton rows={5} />
        </PanelBody>
      ) : events.length === 0 ? (
        <PanelBody>
          <EmptyState
            className="py-8"
            title={t.overview.noTrafficTitle}
            description={t.overview.noTrafficBody}
          />
        </PanelBody>
      ) : (
        <ul className="min-w-0">
          {events.slice(0, 5).map((event) => (
            <TrafficRow key={event.id} event={event} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
