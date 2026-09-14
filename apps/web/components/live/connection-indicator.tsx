"use client";

import { StatusBadge, type StatusTone } from "@/components/shared/status-badge";
import type { StreamStatus } from "@/hooks/use-telemetry-stream";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

import { useEventRate } from "./use-event-rate";

/**
 * Is the telemetry stream actually connected, and how much is coming through?
 *
 * Both halves matter: an open EventSource that has gone silent looks identical to a healthy one
 * unless the rate is on screen next to it, and "0 Ereignisse/s" is the difference between a
 * broken page and an idle simulator.
 */
export function ConnectionIndicator({
  status,
  eventsReceived,
  className,
}: {
  status: StreamStatus;
  eventsReceived: number;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const rate = useEventRate(eventsReceived);

  const tone: StatusTone =
    status === "open" ? "healthy" : status === "connecting" ? "delayed" : "failed";

  const label =
    status === "open"
      ? t.live.connected
      : status === "connecting"
        ? t.live.reconnecting
        : status === "error"
          ? t.live.reconnecting
          : t.live.disconnected;

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <StatusBadge status={tone} label={label} pulse={status === "open"} />
      <span className="flex items-baseline gap-1" aria-label={t.live.eventsPerSecond}>
        <span className="text-foreground font-mono text-sm tabular-nums">
          {formatNumber(rate, locale, { decimals: rate > 0 && rate < 10 ? 1 : 0 })}
        </span>
        <span className="text-muted-foreground text-xs">{t.live.eventsPerSecond}</span>
      </span>
    </div>
  );
}
