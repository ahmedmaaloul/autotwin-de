"use client";

import { useMemo } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Label,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { ChargingCategory, ChargingStatistics } from "@/types/domain";

import { categoryBand, categoryLabel, CATEGORY_COLOR } from "../charging-vocabulary";
import { AXIS_LABEL, AXIS_TICK, ChartFrame, ChartTooltip, GRID_STROKE } from "./chart-frame";

const ORDER: ChargingCategory[] = ["normal", "fast", "ultra_fast"];

interface Row {
  category: ChargingCategory;
  label: string;
  band: string;
  count: number;
}

/**
 * How many sites reach which power class.
 *
 * The classes are the contract's own bands (`<22 | 22–149 | >=150 kW`, BUILD_SPEC §2), so the
 * x-axis carries the kW range under each class name — the reader should never have to know
 * that "Schnellladen" means 22 kW here.
 *
 * Absolute counts, on purpose: the HPC bar is the small one, and the whole point of this chart
 * is being able to read *how* small it still is. The share view is the next chart down.
 */
export function PowerClassChart({ data }: { data: ChargingStatistics["by_power_class"] }) {
  const t = useTranslations();
  const locale = useLocale();

  const rows = useMemo<Row[]>(
    () =>
      ORDER.map((category) => ({
        category,
        label: categoryLabel(category, t),
        band: categoryBand(category, t),
        count: data.find((entry) => entry.category === category)?.count ?? 0,
      })),
    [data, t],
  );

  const bands = useMemo(() => new Map(rows.map((row) => [row.label, row.band])), [rows]);

  return (
    <ChartFrame eyebrow={t.charging.byPower} question={t.charging.byPowerQuestion} height={260}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 16, right: 8, bottom: 44, left: 8 }}>
          <CartesianGrid stroke={GRID_STROKE} vertical={false} strokeDasharray="2 3" />
          <XAxis
            dataKey="label"
            height={40}
            interval={0}
            tick={<ClassTick bands={bands} />}
            tickLine={false}
            axisLine={{ stroke: GRID_STROKE }}
          >
            <Label
              value={t.charging.powerClass}
              position="insideBottom"
              offset={-34}
              style={AXIS_LABEL}
            />
          </XAxis>
          <YAxis
            tick={AXIS_TICK}
            tickLine={false}
            axisLine={{ stroke: GRID_STROKE }}
            width={52}
            tickFormatter={(value: number) => formatNumber(value, locale)}
          >
            <Label
              value={t.charging.stations}
              angle={-90}
              position="insideLeft"
              style={{ ...AXIS_LABEL, textAnchor: "middle" }}
            />
          </YAxis>
          <Tooltip
            cursor={{ fill: "var(--muted)", fillOpacity: 0.5 }}
            content={<PowerTooltip locale={locale} stationsLabel={t.charging.stations} />}
          />
          <Bar dataKey="count" radius={[2, 2, 0, 0]} maxBarSize={72}>
            {rows.map((row) => (
              <Cell key={row.category} fill={CATEGORY_COLOR[row.category]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartFrame>
  );
}

/**
 * Two-line category tick: the class name over its kW band.
 *
 * Recharts clones this element per tick and injects `x`, `y` and `payload`. Drawing the band as
 * a second `tspan` rather than a `LabelList` keeps it below the axis line instead of colliding
 * with the tick text, and means the band travels with the class name at every width.
 */
function ClassTick({
  x,
  y,
  payload,
  bands,
}: {
  x?: number;
  y?: number;
  payload?: { value?: string | number };
  bands: Map<string, string>;
}) {
  const label = String(payload?.value ?? "");
  return (
    <text x={x} y={y} textAnchor="middle" fill="var(--muted-foreground)" fontSize={11}>
      <tspan x={x} dy={14}>
        {label}
      </tspan>
      <tspan x={x} dy={13} fontSize={10} opacity={0.8} className="font-mono">
        {bands.get(label) ?? ""}
      </tspan>
    </text>
  );
}

function PowerTooltip({
  active,
  payload,
  locale,
  stationsLabel,
}: {
  active?: boolean;
  payload?: { payload?: Row }[];
  locale: ReturnType<typeof useLocale>;
  stationsLabel: string;
}) {
  const row = payload?.[0]?.payload;
  if (!row) return null;

  return (
    <ChartTooltip
      active={active}
      title={`${row.label} · ${row.band}`}
      rows={[
        {
          label: stationsLabel,
          value: formatNumber(row.count, locale),
          color: CATEGORY_COLOR[row.category],
        },
      ]}
    />
  );
}
