"use client";

import {
  Cloud,
  CloudFog,
  CloudLightning,
  CloudRain,
  HelpCircle,
  Snowflake,
  Sun,
} from "lucide-react";

import { DataFreshness } from "@/components/shared/data-freshness";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState } from "@/components/shared/states";
import { Skeleton } from "@/components/ui/skeleton";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { DashboardSummary, WeatherCondition } from "@/types/domain";

import { conditionLabel } from "./labels";
import { Figure, Panel, PanelBody, PanelHeader, WrappingTitle } from "./panel";

const CONDITION_ICON: Record<WeatherCondition, typeof Sun> = {
  clear: Sun,
  clouds: Cloud,
  rain: CloudRain,
  snow: Snowflake,
  fog: CloudFog,
  storm: CloudLightning,
  unknown: HelpCircle,
};

/**
 * Weather is on the dashboard because it is the single largest non-obvious driver of range:
 * below freezing an EV loses a fifth of it. The number therefore sits next to the fleet's
 * consumption figure rather than on a page of its own.
 */
export function WeatherCard({
  summary,
  loading,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const weather = summary?.weather_snapshot ?? null;
  const Icon = CONDITION_ICON[weather?.condition ?? "unknown"];

  return (
    <Panel>
      <PanelHeader>
        <SectionHeader
          eyebrow={t.overview.weatherSnapshot}
          title={
            <WrappingTitle>
              {weather ? conditionLabel(weather.condition, t) : t.common.unknown}
            </WrappingTitle>
          }
          actions={<SourceBadge origin="official" source="dwd" compact />}
        />
      </PanelHeader>
      <PanelBody className="flex items-center gap-4">
        {loading ? (
          <Skeleton className="h-12 w-full" />
        ) : !weather ? (
          <EmptyState
            className="w-full py-6"
            title={t.common.noData}
            description={t.overview.weatherNoData}
          />
        ) : (
          <>
            <Icon className="text-muted-foreground size-8 shrink-0" aria-hidden />
            <div className="min-w-0 flex-1">
              <Figure
                className="text-foreground text-[2rem] leading-none font-medium"
                value={
                  weather.temperature_c == null
                    ? "–"
                    : formatNumber(weather.temperature_c, locale, { decimals: 1 })
                }
                unit={t.units.celsius}
              />
              <p className="text-muted-foreground mt-1.5 truncate text-xs">
                {weather.station ? (
                  <>
                    <span className="eyebrow mr-1.5">{t.overview.weatherStation}</span>
                    <span className="font-mono">{weather.station}</span>
                  </>
                ) : null}
              </p>
            </div>
            <DataFreshness timestamp={weather.observed_at} className="shrink-0 self-start" />
          </>
        )}
      </PanelBody>
    </Panel>
  );
}
