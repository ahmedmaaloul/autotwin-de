"use client";

import type { LucideIcon } from "lucide-react";
import { TrendingDown, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

export interface MetricCardProps {
  label: string;
  value: ReactNode;
  unit?: string;
  /** Signed change in percent. Positive is not automatically good — see `positiveIsGood`. */
  delta?: number | null;
  /** Consumption rising is bad; charger count rising is good. The metric decides, not the sign. */
  positiveIsGood?: boolean;
  icon?: LucideIcon;
  hint?: string;
  footer?: ReactNode;
  loading?: boolean;
  className?: string;
}

/**
 * A single measurement: label, big mono value, unit set smaller and muted beside it.
 *
 * The unit is a separate node rather than part of the value string, which is what lets a
 * column of these line up on the decimal point.
 */
export function MetricCard({
  label,
  value,
  unit,
  delta,
  positiveIsGood = true,
  icon: Icon,
  hint,
  footer,
  loading = false,
  className,
}: MetricCardProps) {
  const deltaIsGood = delta == null ? null : delta >= 0 === positiveIsGood;
  const DeltaIcon = delta != null && delta < 0 ? TrendingDown : TrendingUp;

  const labelNode = (
    <span className="eyebrow inline-flex items-center gap-1.5">
      {Icon ? <Icon className="size-3.5" aria-hidden /> : null}
      {label}
    </span>
  );

  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5 px-4 py-3", className)}>
      {hint ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="decoration-muted-foreground/40 w-fit cursor-help underline decoration-dotted underline-offset-4">
              {labelNode}
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">{hint}</TooltipContent>
        </Tooltip>
      ) : (
        labelNode
      )}

      {loading ? (
        <Skeleton className="h-8 w-24" />
      ) : (
        <div className="flex items-baseline gap-1.5">
          <span
            data-slot="metric-value"
            className="text-foreground font-mono text-2xl leading-none font-medium tracking-tight"
          >
            {value}
          </span>
          {unit ? <span className="text-muted-foreground text-xs">{unit}</span> : null}
          {delta != null && Number.isFinite(delta) ? (
            <span
              className={cn(
                "ml-1 inline-flex items-center gap-0.5 font-mono text-xs",
                deltaIsGood ? "text-success" : "text-danger",
              )}
            >
              <DeltaIcon className="size-3" aria-hidden />
              {delta > 0 ? "+" : ""}
              {delta.toFixed(1)} %
            </span>
          ) : null}
        </div>
      )}

      {footer ? <div className="text-muted-foreground text-xs">{footer}</div> : null}
    </div>
  );
}

/**
 * A horizontal strip of metrics separated by hairlines rather than gaps — the density the
 * dashboard needs, and the reason the numbers must be tabular.
 */
export function MetricRow({
  children,
  className,
  columns = 6,
}: {
  children: ReactNode;
  className?: string;
  columns?: 3 | 4 | 5 | 6;
}) {
  const gridClass = {
    3: "sm:grid-cols-2 lg:grid-cols-3",
    4: "sm:grid-cols-2 lg:grid-cols-4",
    5: "sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5",
    6: "sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6",
  }[columns];

  return (
    <div
      className={cn(
        "border-border bg-card divide-border grid divide-y rounded border sm:divide-x sm:divide-y-0",
        gridClass,
        className,
      )}
    >
      {children}
    </div>
  );
}
