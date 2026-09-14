"use client";

import type { FeatureCollection } from "geojson";
import { useCallback } from "react";

import { ChargingLayer } from "@/components/maps/layers/charging-layer";
import { RouteLayer } from "@/components/maps/layers/route-layer";
import { MapCanvas } from "@/components/maps/map-canvas";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { Card } from "@/components/ui/card";
import { ENERGY_LEVELS, ENERGY_LEVEL_VAR } from "@/lib/constants";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { CoverageGap, RouteAnalysis } from "@/types/domain";

import { CoverageGapLayer } from "./layers/coverage-gap-layer";

/**
 * The corridor, its charging sites and — the point of the screen — the stretches where there
 * are none.
 *
 * Three declarative layers on one `<MapCanvas>`; the page never touches MapLibre itself
 * (docs/DESIGN_SYSTEM.md §8). The route keeps the energy-intensity ramp it has everywhere else
 * so the map, the Streckenband and the charts cannot disagree about what "high" looks like.
 */
export function CorridorMap({
  analysis,
  gaps,
  stations,
  focusIndex,
  geometryPending,
}: {
  analysis: RouteAnalysis | null;
  gaps: CoverageGap[];
  stations: FeatureCollection | undefined;
  focusIndex: number | null;
  geometryPending: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const formatLabel = useCallback(
    (gap: CoverageGap) => `${formatNumber(gap.gap_km, locale, { decimals: 0 })} ${t.units.km}`,
    [locale, t.units.km],
  );

  return (
    <Card className="gap-0 rounded border p-0 shadow-none">
      {/* The sites are official data, but the colour of the route line is a model output — two
          provenances on one map, so both are stated rather than averaged into one badge. */}
      <div className="border-border border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.common.legend}
          title={t.analytics.corridorMap}
          description={t.analytics.corridorMapHint}
          actions={
            <div className="flex items-center gap-2">
              <SourceBadge origin="official" source="bundesnetzagentur" compact />
              <SourceBadge origin="derived" compact />
            </div>
          }
        />
      </div>

      <div className="h-[24rem] w-full md:h-[28rem]">
        <MapCanvas ariaLabel={t.analytics.corridorMap} initialZoom={6}>
          <RouteLayer analysis={analysis} fitOnLoad />
          <ChargingLayer data={stations} visible={Boolean(stations)} />
          <CoverageGapLayer gaps={gaps} formatLabel={formatLabel} focusIndex={focusIndex} />
        </MapCanvas>
      </div>

      <div className="border-border flex flex-wrap items-center gap-x-5 gap-y-2 border-t px-4 py-2.5">
        <span className="flex items-center gap-1.5 text-xs">
          <span className="flex" aria-hidden>
            {ENERGY_LEVELS.map((level) => (
              <span
                key={level}
                className="inline-block h-1 w-3"
                style={{ backgroundColor: ENERGY_LEVEL_VAR[level] }}
              />
            ))}
          </span>
          <span className="text-muted-foreground">
            {t.analytics.legendRoute} · {t.routes.energyIntensity}
          </span>
        </span>

        <span className="flex items-center gap-1.5 text-xs">
          <span
            className="inline-block h-1 w-6"
            style={{
              backgroundImage:
                "repeating-linear-gradient(90deg, var(--danger) 0 5px, transparent 5px 8px)",
            }}
            aria-hidden
          />
          <span className="text-muted-foreground">{t.analytics.legendGap}</span>
        </span>

        <span className="flex items-center gap-1.5 text-xs">
          <span className="bg-primary inline-block size-2 rounded-full" aria-hidden />
          <span className="text-muted-foreground">{t.analytics.legendStations}</span>
        </span>

        {!analysis ? (
          <span className="text-muted-foreground ml-auto text-xs">
            {geometryPending
              ? t.analytics.corridorGeometryLoading
              : t.analytics.corridorGeometryUnavailable}
          </span>
        ) : null}
      </div>
    </Card>
  );
}
