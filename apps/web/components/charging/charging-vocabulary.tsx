"use client";

import { Zap } from "lucide-react";

import { cn } from "@/lib/utils";
import { useTranslations } from "@/providers/locale-provider";
import type { Messages } from "@/lib/i18n/messages/de";
import type { ChargingCategory, ConnectorType, CurrentType } from "@/types/domain";

/**
 * One place where the charging enums become words and colours.
 *
 * The map layer, the table badge, the legend and every chart have to agree on what "fast"
 * looks like — if the legend says blue and a bar says green, the reader stops trusting both.
 * The three colours below are the ones `components/maps/layers/charging-layer.tsx` paints with.
 */

export const CATEGORY_COLOR: Record<ChargingCategory, string> = {
  normal: "var(--muted-foreground)",
  fast: "var(--primary)",
  ultra_fast: "var(--success)",
};

export function categoryLabel(category: ChargingCategory, t: Messages): string {
  return {
    normal: t.charging.categoryNormal,
    fast: t.charging.categoryFast,
    ultra_fast: t.charging.categoryUltraFast,
  }[category];
}

/** The kW band behind each category — `<22 | 22–149 | >=150` (BUILD_SPEC §2). */
export function categoryBand(category: ChargingCategory, t: Messages): string {
  return {
    normal: t.charging.bandNormal,
    fast: t.charging.bandFast,
    ultra_fast: t.charging.bandUltraFast,
  }[category];
}

export function connectorLabel(connector: ConnectorType, t: Messages): string {
  return t.charging.connectorTypes[connector];
}

export function currentLabel(current: CurrentType, t: Messages): string {
  return t.charging.currentTypes[current];
}

/**
 * Charging type as a badge.
 *
 * The colour is doubled by the word and, for the two fast classes, by the bolt glyph — colour
 * is never the only channel (docs/DESIGN_SYSTEM.md §2).
 */
export function CategoryBadge({
  category,
  className,
}: {
  category: ChargingCategory;
  className?: string;
}) {
  const t = useTranslations();
  const tone = {
    normal: "border-border bg-muted text-muted-foreground",
    fast: "border-primary/30 bg-primary/10 text-primary",
    ultra_fast: "border-success/30 bg-success/10 text-success",
  }[category];

  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1 rounded border px-1.5 py-0.5 text-[0.6875rem] font-medium whitespace-nowrap",
        tone,
        className,
      )}
    >
      {category === "normal" ? null : <Zap className="size-3" aria-hidden />}
      {categoryLabel(category, t)}
    </span>
  );
}

/**
 * A measurement and its unit as two nodes, so a column of them lines up on the decimal point
 * and the unit never becomes part of the number string (docs/DESIGN_SYSTEM.md §3).
 */
export function Measure({
  value,
  unit,
  className,
  valueClassName,
}: {
  value: string;
  unit?: string;
  className?: string;
  valueClassName?: string;
}) {
  return (
    <span className={cn("inline-flex items-baseline gap-1", className)}>
      <span className={cn("font-mono tabular-nums tracking-tight", valueClassName)}>{value}</span>
      {unit ? <span className="text-muted-foreground text-[0.75rem]">{unit}</span> : null}
    </span>
  );
}
