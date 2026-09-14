"use client";

import { Check } from "lucide-react";
import { useId } from "react";

import { formatDistanceKm } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

export interface CorridorOption {
  slug: string;
  origin: string;
  destination: string;
  /** Present only when the corridor came from the API rather than from the preset list. */
  distanceM?: number | null;
}

/**
 * One-click starting points: the reference corridors the platform is seeded with.
 *
 * A radio group rather than buttons, because picking a corridor is choosing between mutually
 * exclusive options, and that is what a keyboard user expects arrow keys to do here.
 *
 * There is no loading state here on purpose: a chip is a form preset, not a measurement, so it
 * is offered immediately and merely gains the corridor's real distance once `GET /routes`
 * answers. Waiting on the network to show a button that fills in two text fields would make the
 * page look broken for as long as the backend is down.
 */
export function CorridorChips({
  corridors,
  activeSlug,
  onSelect,
  disabled = false,
  className,
}: {
  corridors: CorridorOption[];
  activeSlug: string | null;
  onSelect: (corridor: CorridorOption) => void;
  disabled?: boolean;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const labelId = useId();

  return (
    <div className={cn("min-w-0", className)}>
      <p className="eyebrow mb-1.5" id={labelId}>
        {t.routes.quickSelect}
      </p>

      {corridors.length === 0 ? (
        <p className="text-muted-foreground text-xs">{t.common.noData}</p>
      ) : (
        <div
          role="radiogroup"
          aria-labelledby={labelId}
          className="flex flex-wrap gap-1.5"
        >
          {corridors.map((corridor) => {
            const active = corridor.slug === activeSlug;
            return (
              <button
                key={corridor.slug}
                type="button"
                role="radio"
                aria-checked={active}
                disabled={disabled}
                onClick={() => onSelect(corridor)}
                className={cn(
                  "border-border inline-flex h-7 items-center gap-1.5 rounded border px-2 text-xs whitespace-nowrap transition-colors duration-200 motion-reduce:transition-none",
                  "focus-visible:ring-ring hover:bg-muted focus-visible:ring-2 focus-visible:outline-none",
                  "disabled:pointer-events-none disabled:opacity-50",
                  active
                    ? "border-primary/40 bg-primary/10 text-primary font-medium"
                    : "text-muted-foreground",
                )}
              >
                {active ? <Check className="size-3" aria-hidden /> : null}
                <span>
                  {corridor.origin} <span aria-hidden>→</span> {corridor.destination}
                </span>
                {corridor.distanceM != null ? (
                  <span className="font-mono text-[0.6875rem] tabular-nums opacity-70">
                    {formatDistanceKm(corridor.distanceM, locale, { fromMetres: true })}
                    <span className="ml-0.5">{t.units.km}</span>
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
