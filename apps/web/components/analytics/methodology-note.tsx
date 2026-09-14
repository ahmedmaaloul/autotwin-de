"use client";

import { Sigma } from "lucide-react";
import type { ReactNode } from "react";

import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

/**
 * The sentence that makes a number mean something.
 *
 * The coverage and underserved endpoints return a `methodology` string describing exactly how
 * the figure was produced — which buffer, which power threshold, which projection. Showing
 * "largest gap 84 km" without it would be a claim rather than a measurement, so this component
 * is rendered next to those numbers on every screen that displays them (BUILD_SPEC §7.2).
 */
export function MethodologyNote({
  text,
  meta,
  className,
}: {
  text: string;
  meta?: ReactNode;
  className?: string;
}) {
  const t = useTranslations();

  return (
    <div className={cn("border-border bg-muted/40 rounded border px-3 py-2.5", className)}>
      <div className="mb-1 flex items-center justify-between gap-3">
        <p className="eyebrow flex items-center gap-1.5">
          <Sigma className="size-3" aria-hidden />
          {t.common.methodology}
        </p>
        {meta}
      </div>
      <p className="text-muted-foreground text-xs leading-relaxed">{text}</p>
    </div>
  );
}
