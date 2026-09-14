"use client";

import { FlaskConical, Landmark, Sigma } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

export type DataOrigin = "official" | "simulated" | "derived";

const SOURCE_LABELS: Record<string, string> = {
  bundesnetzagentur: "Bundesnetzagentur",
  dwd: "Deutscher Wetterdienst",
  autobahn: "Autobahn GmbH",
  mobilithek: "Mobilithek",
  osm: "OpenStreetMap",
  osrm: "OSRM",
  simulator: "AutoTwin Simulator",
  derived: "AutoTwin",
};

/**
 * The honesty component.
 *
 * Anywhere simulated data is displayed, this badge is mandatory (docs/DESIGN_SYSTEM.md §6 and
 * ADR 004). It is not a footnote — someone reading a consumption figure has to be able to see,
 * without asking, whether it was measured by an authority or produced by our simulator.
 */
export function SourceBadge({
  origin,
  source,
  className,
  compact = false,
}: {
  origin: DataOrigin;
  source?: string;
  className?: string;
  compact?: boolean;
}) {
  const t = useTranslations();

  const config = {
    official: {
      Icon: Landmark,
      label: t.provenance.official,
      tooltip: t.provenance.officialTooltip,
      classes: "border-success/30 bg-success/10 text-success",
    },
    simulated: {
      Icon: FlaskConical,
      label: t.provenance.simulated,
      tooltip: t.provenance.simulatedTooltip,
      classes: "border-warning/40 bg-warning/10 text-warning",
    },
    derived: {
      Icon: Sigma,
      label: t.provenance.derived,
      tooltip: t.provenance.derivedTooltip,
      classes: "border-border bg-muted text-muted-foreground",
    },
  }[origin];

  const sourceLabel = source ? (SOURCE_LABELS[source] ?? source) : null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "inline-flex w-fit cursor-help items-center gap-1.5 rounded border px-1.5 py-0.5 text-[0.6875rem] font-medium tracking-wide",
            config.classes,
            className,
          )}
        >
          <config.Icon className="size-3" aria-hidden />
          {config.label}
          {sourceLabel && !compact ? (
            <>
              <span className="opacity-40" aria-hidden>
                ·
              </span>
              <span className="font-normal">{sourceLabel}</span>
            </>
          ) : null}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">
        {config.tooltip}
        {sourceLabel ? (
          <span className="mt-1 block opacity-70">
            {t.common.source}: {sourceLabel}
          </span>
        ) : null}
      </TooltipContent>
    </Tooltip>
  );
}
