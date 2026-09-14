"use client";

import { useMemo } from "react";
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Label,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { ChargingStatistics } from "@/types/domain";

import { AXIS_LABEL, AXIS_TICK, ChartFrame, ChartTooltip, GRID_STROKE } from "./chart-frame";

interface Row {
  year: number;
  stations: number;
  cumulative: number;
}

/**
 * When the network was built.
 *
 * Two series on two scales, because the two readings are different questions: the area is the
 * stock that exists at the end of each year, the bars are how much was added in that year.
 * Only the pair shows the thing that matters — a stock curve that keeps rising while the
 * annual bars have stopped growing means the roll-out has plateaued.
 *
 * The area is a flat 12 %-opacity fill of `--primary`, not a gradient: a fading fill would
 * imply a value gradient the data does not have (docs/DESIGN_SYSTEM.md §2).
 */
export function RolloutChart({ data }: { data: ChargingStatistics["growth"] }) {
  const t = useTranslations();
  const locale = useLocale();

  const rows = useMemo<Row[]>(
    () =>
      [...data]
        .sort((a, b) => a.year - b.year)
        .map((entry) => ({
          year: entry.year,
          stations: entry.stations,
          cumulative: entry.cumulative,
        })),
    [data],
  );

  return (
    <ChartFrame eyebrow={t.charging.growth} question={t.charging.growthQuestion} height={300}>
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={rows} margin={{ top: 8, right: 12, bottom: 36, left: 4 }}>
          <CartesianGrid stroke={GRID_STROKE} vertical={false} strokeDasharray="2 3" />
          <XAxis
            dataKey="year"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: GRID_STROKE }}
          >
            <Label
              value={t.charging.commissioningYear}
              position="insideBottom"
              offset={-24}
              style={AXIS_LABEL}
            />
          </XAxis>
          <YAxis
            yAxisId="cumulative"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: GRID_STROKE }}
            width={56}
            tickFormatter={(value: number) => formatNumber(value, locale)}
          >
            <Label
              value={t.charging.cumulative}
              angle={-90}
              position="insideLeft"
              style={{ ...AXIS_LABEL, textAnchor: "middle" }}
            />
          </YAxis>
          <YAxis
            yAxisId="perYear"
            orientation="right"
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: GRID_STROKE }}
            width={52}
            tickFormatter={(value: number) => formatNumber(value, locale)}
          >
            <Label
              value={t.charging.perYear}
              angle={90}
              position="insideRight"
              style={{ ...AXIS_LABEL, textAnchor: "middle" }}
            />
          </YAxis>
          <Tooltip
            cursor={{ fill: "var(--muted)", fillOpacity: 0.5 }}
            content={
              <RolloutTooltip
                locale={locale}
                labels={{ cumulative: t.charging.cumulative, perYear: t.charging.perYear }}
              />
            }
          />
          <Legend
            verticalAlign="top"
            align="right"
            height={24}
            iconType="square"
            iconSize={9}
            wrapperStyle={{ fontSize: 11, color: "var(--muted-foreground)" }}
          />
          <Bar
            yAxisId="perYear"
            dataKey="stations"
            name={t.charging.perYear}
            fill="var(--muted-foreground)"
            fillOpacity={0.45}
            maxBarSize={18}
            radius={[2, 2, 0, 0]}
          />
          <Area
            yAxisId="cumulative"
            type="monotone"
            dataKey="cumulative"
            name={t.charging.cumulative}
            stroke="var(--primary)"
            strokeWidth={1.75}
            fill="var(--primary)"
            fillOpacity={0.12}
            dot={false}
            activeDot={{ r: 3, strokeWidth: 0 }}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

function RolloutTooltip({
  active,
  payload,
  locale,
  labels,
}: {
  active?: boolean;
  payload?: { payload?: Row }[];
  locale: ReturnType<typeof useLocale>;
  labels: { cumulative: string; perYear: string };
}) {
  const row = payload?.[0]?.payload;
  if (!row) return null;

  return (
    <ChartTooltip
      active={active}
      title={row.year}
      rows={[
        {
          label: labels.cumulative,
          value: formatNumber(row.cumulative, locale),
          color: "var(--primary)",
        },
        {
          label: labels.perYear,
          value: formatNumber(row.stations, locale),
          color: "var(--muted-foreground)",
        },
      ]}
    />
  );
}
