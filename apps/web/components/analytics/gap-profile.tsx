"use client";

import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { CoverageGap } from "@/types/domain";
import { cn } from "@/lib/utils";

/**
 * A corridor compressed onto one scale, with the unserved stretches marked — the Streckenband
 * idea (docs/DESIGN_SYSTEM.md §7) reduced to the single variable this screen is about.
 *
 * Seeing *where* the gaps sit matters as much as how long they are: three short gaps clustered
 * behind one another are a different planning problem from three spread across 400 km.
 */
export function GapProfile({
  segments,
  totalKm,
  className,
}: {
  segments: CoverageGap[];
  totalKm: number;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const span = totalKm > 0 ? totalKm : 1;

  return (
    <div className={cn("min-w-0", className)}>
      <div
        role="img"
        aria-label={`${t.analytics.gapProfile} — ${t.analytics.gapProfileHint}`}
        className="bg-muted border-border relative h-2 w-full overflow-hidden rounded-[1px] border"
      >
        {segments.map((segment, index) => {
          const left = Math.max(0, Math.min(100, (segment.start_offset_km / span) * 100));
          const width = Math.max(
            0.8,
            Math.min(100 - left, ((segment.end_offset_km - segment.start_offset_km) / span) * 100),
          );
          return (
            <span
              key={`${segment.start_offset_km}-${index}`}
              className="bg-danger absolute inset-y-0"
              style={{ left: `${left}%`, width: `${width}%` }}
              aria-hidden
            />
          );
        })}
      </div>
      <div className="text-muted-foreground mt-1 flex justify-between font-mono text-[0.625rem] tabular-nums">
        <span>0</span>
        <span>
          {formatNumber(totalKm, locale, { decimals: 0 })} {t.units.km}
        </span>
      </div>
    </div>
  );
}
