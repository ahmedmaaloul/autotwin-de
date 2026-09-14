"use client";

import { Ban, CircleCheck } from "lucide-react";

import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { StatusBadge } from "@/components/shared/status-badge";
import { EmptyState } from "@/components/shared/states";
import { Card } from "@/components/ui/card";
import { formatDateTime, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { TrafficEvent } from "@/types/domain";

import { eventTypeLabel, severityTone, trafficSeverityLabel } from "./labels";

/**
 * What is actually in the way, from the Autobahn GmbH feed.
 *
 * These are the same events the Streckenband marks above the energy band and the map draws in
 * red, listed in the order they occur along the corridor so the list reads like the drive.
 * Severity is a badge with a word, never a bare colour.
 */
export function TrafficEventList({
  events,
  className,
}: {
  events: TrafficEvent[];
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  return (
    <Card className={cn("gap-0 rounded p-0 shadow-none", className)}>
      <div className="border-border border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.routes.traffic}
          title={t.routes.trafficEvents}
          actions={<SourceBadge origin="official" source="autobahn" compact />}
        />
      </div>

      {events.length === 0 ? (
        <div className="p-4">
          <EmptyState
            title={t.common.noResults}
            description={t.routes.noTrafficEvents}
            icon={CircleCheck}
          />
        </div>
      ) : (
        <div className="scrollbar-thin max-h-[26rem] overflow-y-auto">
          <ul className="divide-border divide-y">
            {events.map((event) => (
              <li key={event.id} className="px-4 py-3">
                <div className="flex flex-wrap items-center gap-1.5">
                  <StatusBadge
                    status={severityTone(event.severity)}
                    label={trafficSeverityLabel(event.severity, t) ?? t.common.unknown}
                  />
                  <span className="text-muted-foreground text-[0.6875rem]">
                    {eventTypeLabel(event.event_type, t)}
                  </span>
                  {event.road_name ? (
                    <span className="border-border text-foreground rounded border px-1.5 py-0.5 font-mono text-[0.6875rem]">
                      {event.road_name}
                    </span>
                  ) : null}
                  {event.is_blocked ? (
                    <span className="text-danger inline-flex items-center gap-1 text-[0.6875rem] font-medium">
                      <Ban className="size-3" aria-hidden />
                      {t.routes.blocked}
                    </span>
                  ) : null}
                </div>

                <p className="text-foreground mt-1 text-sm">{event.title}</p>

                {event.description ? (
                  <p className="text-muted-foreground mt-0.5 line-clamp-2 text-xs">
                    {event.description}
                  </p>
                ) : null}

                <dl className="text-muted-foreground mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[0.6875rem]">
                  {event.direction ? (
                    <div className="flex items-baseline gap-1">
                      <dt className="eyebrow">{t.routes.direction}</dt>
                      <dd>{event.direction}</dd>
                    </div>
                  ) : null}
                  {event.delay_minutes != null ? (
                    <div className="flex items-baseline gap-1">
                      <dt className="eyebrow">{t.routes.delay}</dt>
                      <dd>
                        <span className="text-foreground font-mono tabular-nums">
                          {formatNumber(event.delay_minutes, locale)}
                        </span>{" "}
                        {t.units.minutes}
                      </dd>
                    </div>
                  ) : null}
                  {event.starts_at ? (
                    <div className="flex items-baseline gap-1">
                      <dt className="eyebrow">{t.common.from}</dt>
                      <dd className="font-mono">{formatDateTime(event.starts_at, locale)}</dd>
                    </div>
                  ) : null}
                  {event.ends_at ? (
                    <div className="flex items-baseline gap-1">
                      <dt className="eyebrow">{t.common.to}</dt>
                      <dd className="font-mono">{formatDateTime(event.ends_at, locale)}</dd>
                    </div>
                  ) : null}
                </dl>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
