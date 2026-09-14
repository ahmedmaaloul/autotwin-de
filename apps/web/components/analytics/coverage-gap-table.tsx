"use client";

import { Crosshair } from "lucide-react";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { CoverageGap } from "@/types/domain";
import { cn } from "@/lib/utils";

/**
 * Every gap on the corridor, longest first.
 *
 * The row order is a ranking, but the index passed back to the map is the *original* position in
 * the response, so the table and the map always mean the same gap no matter how this is sorted.
 */
export function CoverageGapTable({
  gaps,
  focusIndex = null,
  onFocus,
}: {
  gaps: CoverageGap[];
  focusIndex?: number | null;
  /** Omitted where there is no map to focus — the underserved list reuses the same table. */
  onFocus?: (index: number) => void;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const ranked = useMemo(
    () =>
      gaps
        .map((gap, index) => ({ gap, index }))
        .sort((a, b) => b.gap.gap_km - a.gap.gap_km),
    [gaps],
  );

  return (
    <div className="max-h-72 overflow-y-auto">
      <Table>
        <TableHeader className="bg-card sticky top-0 z-10">
          <TableRow>
            <TableHead className="w-12">{t.analytics.gap}</TableHead>
            <TableHead className="text-right">{t.analytics.gapFrom}</TableHead>
            <TableHead className="text-right">{t.analytics.gapTo}</TableHead>
            <TableHead className="text-right">{t.analytics.gapLength}</TableHead>
            {onFocus ? <TableHead className="w-10" /> : null}
          </TableRow>
        </TableHeader>
        <TableBody>
          {ranked.map(({ gap, index }, rank) => (
            <TableRow
              key={`${gap.start_offset_km}-${gap.end_offset_km}-${index}`}
              data-state={focusIndex === index ? "selected" : undefined}
              className={cn(focusIndex === index && "bg-muted/60")}
            >
              <TableCell className="text-muted-foreground font-mono text-xs tabular-nums">
                {rank + 1}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(gap.start_offset_km, locale, { decimals: 0 })}
              </TableCell>
              <TableCell className="text-right font-mono tabular-nums">
                {formatNumber(gap.end_offset_km, locale, { decimals: 0 })}
              </TableCell>
              <TableCell className="text-right">
                <span className="font-mono font-medium tabular-nums">
                  {formatNumber(gap.gap_km, locale, { decimals: 1 })}
                </span>
                <span className="text-muted-foreground ml-1 text-xs">{t.units.km}</span>
              </TableCell>
              {onFocus ? (
                <TableCell className="text-right">
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    className="size-7"
                    disabled={!gap.geometry}
                    aria-label={t.analytics.focusGap}
                    onClick={() => onFocus(index)}
                  >
                    <Crosshair className="size-3.5" aria-hidden />
                  </Button>
                </TableCell>
              ) : null}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
