"use client";

import { BatteryMedium, CloudSun, Clock, Gauge, Route as RouteIcon, Zap } from "lucide-react";

import { MetricCard, MetricRow } from "@/components/shared/metric-card";
import { formatDelta, formatDistanceKm, formatNumber } from "@/lib/i18n/format";
import type { Locale } from "@/lib/i18n/config";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { RouteAnalysis } from "@/types/domain";

/**
 * The answer, in six numbers.
 *
 * Arrival SOC is the one that decides whether the journey works, so it is the only value
 * allowed to carry colour, and it is coloured against the minimum the request asked for rather
 * than against a fixed threshold: 18 % is comfortable when 10 % was required and a failure when
 * 25 % was. The required value sits underneath it so the comparison is visible, not implied.
 */
export function RouteSummary({
  analysis,
  className,
}: {
  analysis: RouteAnalysis;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const tone = arrivalTone(analysis.arrival_soc_percent, analysis.min_soc_percent_required);

  return (
    <MetricRow columns={6} className={className}>
      <MetricCard
        label={t.routes.distance}
        icon={RouteIcon}
        value={formatDistanceKm(analysis.route.distance_m, locale, { fromMetres: true })}
        unit={t.units.km}
        footer={
          <span className="truncate">
            {analysis.route.origin.name} <span aria-hidden>→</span>{" "}
            {analysis.route.destination.name}
          </span>
        }
      />

      <MetricCard
        label={t.routes.duration}
        icon={Clock}
        value={<DurationValue seconds={analysis.route.duration_s} />}
      />

      <MetricCard
        label={t.routes.arrivalSoc}
        icon={BatteryMedium}
        hint={t.routes.arrivalSocTooltip}
        value={
          <span className={TONE_TEXT[tone]}>
            {formatNumber(analysis.arrival_soc_percent, locale)}
          </span>
        }
        unit={t.units.percent}
        footer={
          <span>
            {t.routes.minSocRequired}{" "}
            <span className="font-mono tabular-nums">
              {formatNumber(analysis.min_soc_percent_required, locale)}
            </span>{" "}
            {t.units.percent}
          </span>
        }
      />

      <MetricCard
        label={t.routes.energyRequired}
        icon={Zap}
        value={formatNumber(analysis.energy_kwh_total, locale, { decimals: 1 })}
        unit={t.units.kwh}
        footer={
          <span>
            {t.routes.startSoc}{" "}
            <span className="font-mono tabular-nums">
              {formatNumber(analysis.start_soc_percent, locale)}
            </span>{" "}
            {t.units.percent}
          </span>
        }
      />

      <MetricCard
        label={t.routes.avgConsumption}
        icon={Gauge}
        value={formatNumber(analysis.avg_consumption_kwh_100km, locale, { decimals: 1 })}
        unit={t.units.kwhPer100km}
        footer={
          <span>
            {t.routes.baselineModel}{" "}
            <span className="font-mono tabular-nums">
              {formatNumber(analysis.baseline_kwh_100km, locale, { decimals: 1 })}
            </span>{" "}
            {t.units.kwhPer100km}
          </span>
        }
      />

      <MetricCard
        label={t.routes.weatherTraffic}
        icon={CloudSun}
        value={
          <span className={analysis.total_penalty_percent > 0 ? "text-danger" : "text-success"}>
            {signedNumber(analysis.total_penalty_percent, locale)}
          </span>
        }
        unit={t.units.percent}
        footer={
          <span className="flex flex-wrap gap-x-2">
            <span>
              {t.routes.weather} {formatDelta(analysis.weather_penalty_percent, locale)}
            </span>
            <span>
              {t.routes.traffic} {formatDelta(analysis.traffic_penalty_percent, locale)}
            </span>
          </span>
        }
      />
    </MetricRow>
  );
}

type ArrivalTone = "success" | "warning" | "danger";

const TONE_TEXT: Record<ArrivalTone, string> = {
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
};

/** Below the required minimum is a failure; within 10 points of it is uncomfortably close. */
function arrivalTone(arrival: number, required: number): ArrivalTone {
  if (arrival < required) return "danger";
  if (arrival < required + 10) return "warning";
  return "success";
}

/**
 * `+8,2` — the sign carries the message, the unit is rendered separately by the metric card
 * (docs/DESIGN_SYSTEM.md §3), which `formatDelta` cannot do because it bakes the `%` in.
 */
function signedNumber(value: number | null | undefined, locale: Locale, decimals = 1): string {
  if (value == null || !Number.isFinite(value)) return "–";
  return `${value > 0 ? "+" : ""}${formatNumber(value, locale, { decimals })}`;
}

/**
 * `2 Std. 19 Min.` with each unit as its own muted node, so the hours and the minutes still
 * line up down a column of metric cards.
 */
function DurationValue({ seconds, className }: { seconds: number; className?: string }) {
  const t = useTranslations();
  const locale = useLocale();

  if (!Number.isFinite(seconds) || seconds < 0) return <>–</>;

  const totalMinutes = Math.round(seconds / 60);
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;

  return (
    <span className={cn("inline-flex items-baseline gap-1", className)}>
      {hours > 0 ? (
        <>
          <span>{formatNumber(hours, locale)}</span>
          <span className="text-muted-foreground text-xs">{t.units.hours}</span>
        </>
      ) : null}
      <span>{hours > 0 ? String(minutes).padStart(2, "0") : formatNumber(minutes, locale)}</span>
      <span className="text-muted-foreground text-xs">{t.units.minutes}</span>
    </span>
  );
}
