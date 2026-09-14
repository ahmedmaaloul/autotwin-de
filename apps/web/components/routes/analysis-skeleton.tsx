"use client";

import { Loader2 } from "lucide-react";

import { SectionHeader } from "@/components/shared/section-header";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useTranslations } from "@/providers/locale-provider";

import { EnergyLegend } from "./energy-legend";

/**
 * The waiting state.
 *
 * An analysis costs a routing call, a weather lookup, a traffic query and a per-segment model
 * run, so several seconds is normal. A spinner in an empty page reads as "broken"; the shape of
 * the answer, greyed out, reads as "working" — and by the time the data lands nothing on the
 * page moves except the values appearing in place.
 */
export function AnalysisSkeleton() {
  const t = useTranslations();

  return (
    <div className="space-y-4" role="status" aria-busy="true">
      <span className="sr-only">{t.routes.analyzing}</span>

      <Card className="border-primary/30 flex-row items-center gap-3 rounded p-4 shadow-none">
        <Loader2 className="text-primary size-4 shrink-0 animate-spin motion-reduce:animate-none" aria-hidden />
        <div className="min-w-0">
          <p className="text-foreground text-sm font-medium">{t.routes.loadingTitle}</p>
          <p className="text-muted-foreground mt-0.5 text-xs">{t.routes.loadingBody}</p>
        </div>
      </Card>

      {/* Metric row */}
      <div className="border-border bg-card divide-border grid divide-y rounded border sm:grid-cols-2 sm:divide-x sm:divide-y-0 lg:grid-cols-3 xl:grid-cols-6">
        {Array.from({ length: 6 }, (_, index) => (
          <div key={index} className="flex flex-col gap-2 px-4 py-3">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-7 w-24" />
            <Skeleton className="h-3 w-28" />
          </div>
        ))}
      </div>

      {/* Streckenband */}
      <Card className="gap-0 rounded p-0 shadow-none">
        <div className="border-border border-b px-4 py-3">
          <SectionHeader eyebrow={t.routes.strip} title={t.routes.loadingTitle} />
        </div>
        <div className="space-y-2 px-4 py-4">
          <Skeleton className="h-[132px] w-full" />
          <EnergyLegend showRegisters className="opacity-40" />
        </div>
      </Card>

      {/* Map + explanation */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Skeleton className="h-[26rem] lg:col-span-2" />
        <Skeleton className="h-[26rem]" />
      </div>

      {/* Segments + traffic */}
      <div className="grid gap-4 lg:grid-cols-3">
        <Skeleton className="h-64 lg:col-span-2" />
        <Skeleton className="h-64" />
      </div>
    </div>
  );
}
