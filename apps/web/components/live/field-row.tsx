"use client";

import type { ReactNode } from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

/**
 * One labelled measurement in the vehicle panel: condensed label on the left, mono value on the
 * right, unit as a separate muted node so a column of these aligns on the decimal point
 * (docs/DESIGN_SYSTEM.md §3).
 */
export function FieldRow({
  label,
  value,
  unit,
  hint,
  tone,
  className,
}: {
  label: string;
  value: ReactNode;
  unit?: string;
  hint?: string;
  tone?: "default" | "success" | "warning" | "danger";
  className?: string;
}) {
  const labelNode = <span className="eyebrow truncate">{label}</span>;

  return (
    <div
      className={cn(
        "border-border flex min-w-0 items-baseline justify-between gap-3 border-b py-1.5 last:border-b-0",
        className,
      )}
    >
      {hint ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="decoration-muted-foreground/40 min-w-0 cursor-help underline decoration-dotted underline-offset-4">
              {labelNode}
            </span>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs">{hint}</TooltipContent>
        </Tooltip>
      ) : (
        labelNode
      )}

      <span className="flex shrink-0 items-baseline gap-1">
        <span
          className={cn(
            "font-mono text-sm tabular-nums",
            tone === "success" && "text-success",
            tone === "warning" && "text-warning",
            tone === "danger" && "text-danger",
            (tone === undefined || tone === "default") && "text-foreground",
          )}
        >
          {value}
        </span>
        {unit ? <span className="text-muted-foreground text-xs">{unit}</span> : null}
      </span>
    </div>
  );
}
