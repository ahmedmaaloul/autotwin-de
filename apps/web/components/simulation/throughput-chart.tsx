"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";

import { EmptyState } from "@/components/shared/states";
import { formatDateTime, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { Locale } from "@/lib/i18n/config";

import type { Sample } from "./use-sample-history";

const TICK = { fontSize: 10, fill: "var(--muted-foreground)" } as const;

/**
 * Event rate over the last few minutes.
 *
 * The question it answers is on the section header above it: does the stream hold a steady rate
 * once the simulation is running? A simulator that starts fast and decays is a back-pressure
 * problem in the consumer, and the shape of this line is where that becomes visible — a single
 * instantaneous number never would.
 */
export function ThroughputChart({ samples }: { samples: Sample[] }) {
  const t = useTranslations();
  const locale = useLocale();

  if (samples.length < 2) {
    return (
      <EmptyState
        title={t.simulation.throughputEmpty}
        description={t.simulation.throughputEmptyBody}
      />
    );
  }

  return (
    <div className="h-44 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={samples} margin={{ top: 6, right: 8, bottom: 20, left: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="2 3" vertical={false} />
          <XAxis
            dataKey="at"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            minTickGap={48}
            tick={TICK}
            tickLine={false}
            stroke="var(--border)"
            tickFormatter={(value: number) => clockLabel(value, locale)}
            label={{
              value: t.simulation.throughputTime,
              position: "insideBottom",
              offset: -12,
              style: TICK,
            }}
          />
          <YAxis
            width={52}
            allowDecimals
            tick={TICK}
            tickLine={false}
            stroke="var(--border)"
            tickFormatter={(value: number) => formatNumber(value, locale, { decimals: 0 })}
            label={{
              value: t.simulation.throughputAxis,
              angle: -90,
              position: "insideLeft",
              style: { ...TICK, textAnchor: "middle" },
            }}
          />
          <Tooltip
            cursor={{ stroke: "var(--border)" }}
            content={<ThroughputTooltip locale={locale} label={t.simulation.throughputAxis} />}
          />
          <Line
            type="monotone"
            dataKey="value"
            stroke="var(--primary)"
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function clockLabel(value: number, locale: Locale): string {
  return formatDateTime(new Date(value), locale, { minute: "2-digit", second: "2-digit" });
}

function ThroughputTooltip({
  active,
  payload,
  locale,
  label,
}: TooltipProps<number, string> & { locale: Locale; label: string }) {
  if (!active || !payload || payload.length === 0) return null;
  const point = payload[0]?.payload as Sample | undefined;
  if (!point) return null;

  return (
    <div className="border-border bg-popover text-popover-foreground rounded border px-2 py-1.5 text-xs shadow-sm">
      <p className="font-mono tabular-nums">
        {formatDateTime(new Date(point.at), locale, { timeStyle: "medium" })}
      </p>
      <p className="mt-0.5 flex items-baseline gap-1">
        <span className="text-foreground font-mono text-sm tabular-nums">
          {formatNumber(point.value, locale, { decimals: 1 })}
        </span>
        <span className="text-muted-foreground">{label}</span>
      </p>
    </div>
  );
}
