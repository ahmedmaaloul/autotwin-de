"use client";

import type { ReactNode } from "react";

import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

import { CATEGORY_COLOR, CATEGORY_ORDER, categoryLabel } from "./labels";

/**
 * The map's key.
 *
 * Every map in the product carries one (docs/DESIGN_SYSTEM.md §8), and the swatches read the
 * same CSS tokens the MapLibre paint expressions resolve from `lib/map/style.ts` — so the
 * legend cannot drift away from the thing it explains.
 */
function Swatch({ color, ring = false }: { color: string; ring?: boolean }) {
  return (
    <span
      aria-hidden
      className="size-2.5 shrink-0 rounded-full"
      style={
        ring
          ? { boxShadow: `inset 0 0 0 1.5px ${color}` }
          : { backgroundColor: color }
      }
    />
  );
}

function Row({ children }: { children: ReactNode }) {
  return <li className="flex items-center gap-2 whitespace-nowrap">{children}</li>;
}

export function MapLegend({
  showCharging,
  showTraffic,
  showVehicles,
  className,
}: {
  showCharging: boolean;
  showTraffic: boolean;
  showVehicles: boolean;
  className?: string;
}) {
  const t = useTranslations();

  return (
    <div
      className={cn(
        "border-border bg-card/95 text-muted-foreground pointer-events-none absolute top-2 left-2 z-10 max-w-[min(16rem,calc(100%-1rem))] rounded border p-2 text-[0.6875rem] backdrop-blur",
        className,
      )}
    >
      <p className="eyebrow mb-1.5">{t.common.legend}</p>
      <ul className="space-y-1">
        {showCharging ? (
          <>
            <Row>
              <Swatch color="var(--primary)" ring />
              {t.overview.legendCluster}
            </Row>
            {CATEGORY_ORDER.map((category) => (
              <Row key={category}>
                <Swatch color={CATEGORY_COLOR[category]} />
                {categoryLabel(category, t)}
              </Row>
            ))}
          </>
        ) : null}
        {showTraffic ? (
          <>
            <Row>
              <Swatch color="var(--danger)" />
              {t.overview.legendTrafficHigh}
            </Row>
            <Row>
              <Swatch color="var(--warning)" />
              {t.overview.legendTrafficModerate}
            </Row>
          </>
        ) : null}
        {showVehicles ? (
          <Row>
            <span className="flex shrink-0 items-center gap-0.5" aria-hidden>
              <Swatch color="var(--danger)" />
              <Swatch color="var(--warning)" />
              <Swatch color="var(--success)" />
            </span>
            {t.overview.legendVehicle}
          </Row>
        ) : null}
      </ul>
    </div>
  );
}
