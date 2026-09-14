"use client";

import { cva, type VariantProps } from "class-variance-authority";

import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

export type StatusTone = "healthy" | "delayed" | "degraded" | "failed" | "simulation" | "neutral";

const badge = cva(
  "inline-flex items-center gap-1.5 rounded border px-1.5 py-0.5 text-[0.6875rem] font-medium whitespace-nowrap",
  {
    variants: {
      tone: {
        healthy: "border-success/30 bg-success/10 text-success",
        delayed: "border-warning/30 bg-warning/10 text-warning",
        degraded: "border-warning/30 bg-warning/10 text-warning",
        failed: "border-danger/30 bg-danger/10 text-danger",
        simulation: "border-primary/30 bg-primary/10 text-primary",
        neutral: "border-border bg-muted text-muted-foreground",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

const DOT_TONE: Record<StatusTone, string> = {
  healthy: "bg-success",
  delayed: "bg-warning",
  degraded: "bg-warning",
  failed: "bg-danger",
  simulation: "bg-primary",
  neutral: "bg-muted-foreground",
};

/**
 * Status is never carried by colour alone — every badge pairs the dot with a word, so it
 * survives greyscale printing and colour-vision deficiency (docs/DESIGN_SYSTEM.md §2).
 */
export function StatusBadge({
  status,
  label,
  className,
  pulse = false,
}: {
  status: StatusTone;
  label?: string;
  className?: string;
  pulse?: boolean;
} & VariantProps<typeof badge>) {
  const t = useTranslations();
  const fallback: Record<StatusTone, string> = {
    healthy: t.status.healthy,
    delayed: t.status.delayed,
    degraded: t.status.degraded,
    failed: t.status.failed,
    simulation: t.status.simulation,
    neutral: t.common.unknown,
  };

  return (
    <span className={cn(badge({ tone: status }), className)}>
      <span className="relative flex size-1.5">
        {pulse ? (
          <span
            className={cn(
              "absolute inline-flex size-full animate-ping rounded-full opacity-60 motion-reduce:hidden",
              DOT_TONE[status],
            )}
            aria-hidden
          />
        ) : null}
        <span className={cn("relative inline-flex size-1.5 rounded-full", DOT_TONE[status])} />
      </span>
      {label ?? fallback[status]}
    </span>
  );
}
