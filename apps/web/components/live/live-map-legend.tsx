"use client";

import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

/**
 * What the dots on the live map mean.
 *
 * The vehicle markers are coloured by state of charge along the same danger → warning → success
 * ramp the rest of the product uses, so the legend states the thresholds rather than inventing
 * new words for them. Colour is never the only channel: each swatch is labelled.
 */
export function LiveMapLegend({
  showCharging,
  className,
}: {
  showCharging: boolean;
  className?: string;
}) {
  const t = useTranslations();

  const entries = [
    { key: "critical", label: t.live.legendCritical, className: "bg-danger", range: "< 10 %" },
    { key: "low", label: t.live.legendLow, className: "bg-warning", range: "10–25 %" },
    { key: "ok", label: t.live.legendOk, className: "bg-success", range: "> 25 %" },
  ];

  return (
    <div
      className={cn(
        "border-border bg-card/90 supports-[backdrop-filter]:bg-card/75 rounded border px-2.5 py-2 backdrop-blur",
        className,
      )}
    >
      <p className="eyebrow mb-1.5">{t.live.legendTitle}</p>
      <ul className="space-y-1">
        {entries.map((entry) => (
          <li key={entry.key} className="flex items-center gap-2">
            <span className={cn("size-2 shrink-0 rounded-full", entry.className)} aria-hidden />
            <span className="text-foreground text-xs">{entry.label}</span>
            <span className="text-muted-foreground ml-auto font-mono text-[0.6875rem] tabular-nums">
              {entry.range}
            </span>
          </li>
        ))}
        {showCharging ? (
          <li className="border-border mt-1.5 flex items-center gap-2 border-t pt-1.5">
            <span className="bg-primary size-2 shrink-0 rounded-full" aria-hidden />
            <span className="text-foreground text-xs">{t.live.legendCharging}</span>
          </li>
        ) : null}
      </ul>
    </div>
  );
}
