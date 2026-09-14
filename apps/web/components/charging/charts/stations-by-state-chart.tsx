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

import { BUNDESLAENDER } from "@/lib/constants";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { ChargingStatistics } from "@/types/domain";

import { AXIS_LABEL, AXIS_TICK, ChartFrame, ChartTooltip, GRID_STROKE } from "./chart-frame";

interface Row {
  code: string;
  name: string;
  stations: number;
  fastPoints: number;
  totalKw: number;
}

/**
 * Stations per federal state, sorted by size.
 *
 * Horizontal, because sixteen German state names do not fit under a vertical axis without
 * being rotated into illegibility. Sorted rather than alphabetical, because the question is a
 * ranking: who carries the network, and how far behind is the rest.
 */
export function StationsByStateChart({ data }: { data: ChargingStatistics["by_bundesland"] }) {
  const t = useTranslations();
  const locale = useLocale();

  const rows = useMemo<Row[]>(
    () =>
      data
        .map((entry) => ({
          code: entry.bundesland,
          name:
            BUNDESLAENDER.find((state) => state.code === entry.bundesland)?.name ??
            entry.bundesland,
          stations: entry.stations,
          fastPoints: entry.fast_points,
          totalKw: entry.total_kw,
        }))
        .sort((a, b) => b.stations - a.stations),
    [data],
  );

  return (
    <ChartFrame
      eyebrow={t.charging.byBundesland}
      question={t.charging.byBundeslandQuestion}
      height={Math.max(280, rows.length * 22 + 60)}
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
            <Label value={t.charging.stations} position="insideBottom" offset={-14} style={AXIS_LABEL} />
          </XAxis>
          <YAxis
            type="category"
            dataKey="name"
            width={132}
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: GRID_STROKE }}
          />
          <Tooltip
            cursor={{ fill: "var(--muted)", fillOpacity: 0.5 }}
            content={
              <StateTooltip
                locale={locale}
                labels={{
                  stations: t.charging.stations,
                  fastPoints: t.charging.fastPoints,
                  installedPower: t.charging.installedPower,
                  mw: t.charging.unitMw,
                }}
              />
            }
          />
          <Bar dataKey="stations" fill="var(--primary)" radius={[0, 2, 2, 0]} maxBarSize={16} />
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

function StateTooltip({
  active,
  payload,
  locale,
  labels,
}: {
  active?: boolean;
  payload?: { payload?: Row }[];
  locale: ReturnType<typeof useLocale>;
  labels: { stations: string; fastPoints: string; installedPower: string; mw: string };
}) {
  const row = payload?.[0]?.payload;
  if (!row) return null;

  return (
    <ChartTooltip
      active={active}
      title={row.name}
      rows={[
        {
          label: labels.stations,
          value: formatNumber(row.stations, locale),
          color: "var(--primary)",
        },
        { label: labels.fastPoints, value: formatNumber(row.fastPoints, locale) },
        {
          label: labels.installedPower,
          value: formatNumber(row.totalKw / 1000, locale, { decimals: 1 }),
          unit: labels.mw,
        },
      ]}
    />
  );
}
