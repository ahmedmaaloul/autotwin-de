"use client";

import type { ReactNode } from "react";

import { DataFreshness } from "@/components/shared/data-freshness";
import { SourceBadge } from "@/components/shared/source-badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { Messages } from "@/lib/i18n/messages/de";
import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { ProviderMode, RouteAnalysis } from "@/types/domain";

/**
 * Provenance for the analysis, immediately under the heading and above the numbers.
 *
 * A route analysis mixes origins, and pretending otherwise would be the exact dishonesty
 * ADR 004 exists to prevent: the corridor, the weather observations and the traffic reports are
 * official data, while the energy prediction comes from a model trained on simulated telemetry.
 * So the badge says `SIMULIERT`, the notice says which half that applies to, and the three
 * provider modes say whether each source answered live or from cache.
 */
export function AnalysisMeta({
  analysis,
  className,
}: {
  analysis: RouteAnalysis;
  className?: string;
}) {
  const t = useTranslations();

  const modes: { label: string; mode: ProviderMode }[] = [
    { label: t.routes.routing, mode: analysis.data_modes.routing },
    { label: t.routes.weather, mode: analysis.data_modes.weather },
    { label: t.routes.traffic, mode: analysis.data_modes.traffic },
  ];

  return (
    <div
      className={cn(
        "border-border bg-card flex flex-wrap items-center gap-x-4 gap-y-2 rounded border px-4 py-2.5",
        className,
      )}
    >
      <SourceBadge origin="simulated" />

      <p className="text-muted-foreground min-w-0 flex-1 text-xs">{t.routes.predictionNotice}</p>

      <dl className="flex flex-wrap items-center gap-x-4 gap-y-1">
        {analysis.model_name ? (
          <Fact label={t.routes.modelUsed}>
            <span className="font-mono text-[0.6875rem]">
              {analysis.model_name}
              {analysis.model_version ? ` · ${analysis.model_version}` : ""}
            </span>
          </Fact>
        ) : null}

        <Fact label={t.routes.dataModes}>
          <span className="flex flex-wrap items-center gap-x-2">
            {modes.map(({ label, mode }) => (
              <Tooltip key={label}>
                <TooltipTrigger asChild>
                  <span
                    className={cn(
                      "decoration-muted-foreground/40 cursor-help text-[0.6875rem] underline decoration-dotted underline-offset-4",
                      mode === "live" ? "text-success" : "text-warning",
                    )}
                  >
                    {label}
                  </span>
                </TooltipTrigger>
                <TooltipContent>{modeLabel(mode, t)}</TooltipContent>
              </Tooltip>
            ))}
          </span>
        </Fact>

        <Fact label={t.routes.generatedAt}>
          <DataFreshness timestamp={analysis.generated_at} showIcon={false} />
        </Fact>
      </dl>
    </div>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="eyebrow">{label}</dt>
      <dd className="text-muted-foreground text-xs">{children}</dd>
    </div>
  );
}

function modeLabel(mode: ProviderMode, t: Messages): string {
  return t.provenance.dataMode[mode];
}
