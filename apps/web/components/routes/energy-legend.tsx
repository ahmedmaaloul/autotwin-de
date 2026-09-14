"use client";

import { ENERGY_LEVELS, ENERGY_LEVEL_VAR } from "@/lib/constants";
import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

import { intensityLabel } from "./labels";

/**
 * The shared intensity ramp, written out.
 *
 * The Streckenband, the route on the map and the segment table all fill from
 * `--energy-low … --energy-critical`, so the ramp is explained once and the three surfaces are
 * read against the same key. Each swatch carries its word: colour is never the only channel
 * (docs/DESIGN_SYSTEM.md §2).
 */
export function EnergyLegend({
  showRegisters = false,
  className,
}: {
  /** Also key the Streckenband's other registers: SOC line, charging tick, traffic marker. */
  showRegisters?: boolean;
  className?: string;
}) {
  const t = useTranslations();

  return (
    <div
      className={cn("flex flex-wrap items-center gap-x-4 gap-y-1.5", className)}
      aria-label={t.common.legend}
    >
      <span className="eyebrow">{t.routes.energyIntensity}</span>

      <ul className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        {ENERGY_LEVELS.map((level) => (
          <li key={level} className="text-muted-foreground flex items-center gap-1.5 text-xs">
            <span
              className="border-border/60 size-2.5 shrink-0 rounded-[1px] border"
              style={{ backgroundColor: ENERGY_LEVEL_VAR[level] }}
              aria-hidden
            />
            {intensityLabel(level, t)}
          </li>
        ))}
      </ul>

      {showRegisters ? (
        <ul className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs">
          <li className="flex items-center gap-1.5">
            <svg width="18" height="8" viewBox="0 0 18 8" aria-hidden className="shrink-0">
              <path
                d="M0 1 L18 7"
                stroke="var(--foreground)"
                strokeWidth="1.5"
                opacity="0.7"
                fill="none"
              />
            </svg>
            {t.routes.legendSoc}
          </li>
          <li className="flex items-center gap-1.5">
            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden className="shrink-0">
              <line x1="5" y1="0" x2="5" y2="6" stroke="var(--primary)" strokeWidth="1.5" />
              <circle cx="5" cy="8" r="2" fill="var(--primary)" />
            </svg>
            {t.routes.legendChargingStop}
          </li>
          <li className="flex items-center gap-1.5">
            <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden className="shrink-0">
              <path d="M5 2 L9 8 L1 8 Z" fill="var(--danger)" />
            </svg>
            {t.routes.legendTrafficEvent}
          </li>
        </ul>
      ) : null}
    </div>
  );
}
