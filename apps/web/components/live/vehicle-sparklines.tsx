"use client";

import { useMemo } from "react";

import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { TelemetryPoint } from "@/types/domain";

import { Sparkline } from "./sparkline";

/**
 * The three channels that describe a driving electric vehicle: how full it is, how fast it is
 * going, and what that is costing. Shown as traces rather than numbers because the shape is the
 * information — a SOC line bending downwards steeply is the thing an engineer reacts to.
 */
export function VehicleSparklines({ points }: { points: TelemetryPoint[] }) {
  const t = useTranslations();
  const locale = useLocale();

  const series = useMemo(
    () => ({
      soc: points.map((point) => point.battery_soc_percent),
      speed: points.map((point) => point.speed_kmh),
      consumption: points.map((point) => point.energy_consumption_kwh_100km),
    }),
    [points],
  );

  const channels = [
    {
      key: "soc",
      label: t.live.soc,
      unit: t.units.percent,
      values: series.soc,
      decimals: 1,
      color: "var(--success)",
    },
    {
      key: "speed",
      label: t.live.speed,
      unit: t.units.kmh,
      values: series.speed,
      decimals: 0,
      color: "var(--primary)",
    },
    {
      key: "consumption",
      label: t.live.currentConsumption,
      unit: t.units.kwhPer100km,
      values: series.consumption,
      decimals: 1,
      color: "var(--energy-medium)",
    },
  ] as const;

  return (
    <div className="space-y-3">
      {channels.map((channel) => {
        // The window's range rather than its last value: the live figure is already in the
        // field list below, and two numbers under one label that disagree — the stored series
        // lags the stream by up to one poll — would read as a bug.
        const span =
          channel.values.length === 0
            ? "–"
            : `${formatNumber(Math.min(...channel.values), locale, { decimals: channel.decimals })}–${formatNumber(
                Math.max(...channel.values),
                locale,
                { decimals: channel.decimals },
              )}`;
        return (
          <div key={channel.key}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="eyebrow">{channel.label}</span>
              <span className="flex items-baseline gap-1">
                <span className="text-foreground font-mono text-sm tabular-nums">{span}</span>
                <span className="text-muted-foreground text-xs">{channel.unit}</span>
              </span>
            </div>
            <Sparkline
              values={channel.values}
              color={channel.color}
              ariaLabel={`${channel.label}: ${span} ${channel.unit}`}
            />
          </div>
        );
      })}
    </div>
  );
}
