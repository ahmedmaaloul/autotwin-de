"use client";

import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Label,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatNumber, formatPercent } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { ChargingStatistics } from "@/types/domain";

import { AXIS_LABEL, AXIS_TICK, ChartFrame, ChartTooltip, GRID_STROKE } from "./chart-frame";

const TOP_N = 12;

interface Row {
  operator: string;
  stations: number;
  sharePercent: number;
}

/**
 * The twelve largest operators.
 *
 * Twelve, not "all": the tail of the Bundesnetzagentur register is hundreds of municipal
 * utilities with a handful of sites each, and plotting them turns a readable ranking into a
 * grey smear. The share column in the tooltip is what answers the concentration question —
 * a bar length says who is biggest, a percentage says whether that matters.
 */
export function TopOperatorsChart({ data }: { data: ChargingStatistics["by_operator"] }) {
  const t = useTranslations();
  const locale = useLocale();

  const rows = useMemo<Row[]>(() => {
    const total = data.reduce((sum, entry) => sum + entry.stations, 0);
    return [...data]
      .sort((a, b) => b.stations - a.stations)
      .slice(0, TOP_N)
      .map((entry) => ({
        operator: entry.operator,
        stations: entry.stations,
        sharePercent: total > 0 ? (entry.stations / total) * 100 : 0,
      }));
  }, [data]);

  return (
    <ChartFrame
      eyebrow={t.charging.byOperator}
      question={t.charging.byOperatorQuestion}
      height={Math.max(260, rows.length * 24 + 60)}
    >
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 16, bottom: 24, left: 8 }}>
          <CartesianGrid stroke={GRID_STROKE} horizontal={false} strokeDasharray="2 3" />
          <XAxis
            type="number"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: GRID_STROKE }}
            tickFormatter={(value: number) => formatNumber(value, locale)}
          >
            <Label
              value={t.charging.stations}
              position="insideBottom"
              offset={-14}
              style={AXIS_LABEL}
            />
          </XAxis>
          <YAxis
            type="category"
            dataKey="operator"
            width={148}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: GRID_STROKE }}
          />
          <Tooltip
            cursor={{ fill: "var(--muted)", fillOpacity: 0.5 }}
            content={
              <OperatorTooltip
                locale={locale}
                labels={{ stations: t.charging.stations, share: t.charging.share }}
              />
            }
          />
          <Bar dataKey="stations" fill="var(--primary)" radius={[0, 2, 2, 0]} maxBarSize={16} />
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

function OperatorTooltip({
  active,
  payload,
  locale,
  labels,
}: {
  active?: boolean;
  payload?: { payload?: Row }[];
  locale: ReturnType<typeof useLocale>;
  labels: { stations: string; share: string };
}) {
  const row = payload?.[0]?.payload;
  if (!row) return null;

  return (
    <ChartTooltip
      active={active}
      title={row.operator}
      rows={[
        {
          label: labels.stations,
          value: formatNumber(row.stations, locale),
          color: "var(--primary)",
        },
        { label: labels.share, value: formatPercent(row.sharePercent, locale) },
      ]}
    />
  );
}
