"use client";

import type { ReactNode } from "react";

import { SectionHeader } from "@/components/shared/section-header";
import { cn } from "@/lib/utils";

/**
 * Shared chart chrome.
 *
 * Every chart on this page carries the question it answers in its header — "Wann ist die
 * Ladeinfrastruktur entstanden?" rather than "Wachstum" — because a chart without a question
 * is a decoration that the reader has to reverse-engineer.
 *
 * The axis colours, grid and tooltip below are the only ones any chart here uses, so no chart
 * can invent a colour of its own (docs/DESIGN_SYSTEM.md §2).
 */

export const AXIS_TICK = { fontSize: 11, fill: "var(--muted-foreground)" } as const;
export const AXIS_LABEL = {
  fontSize: 11,
  fill: "var(--muted-foreground)",
  letterSpacing: "0.04em",
} as const;
export const GRID_STROKE = "var(--border)";

export function ChartFrame({
  eyebrow,
  question,
  hint,
  actions,
  height = 260,
  children,
  className,
}: {
  eyebrow: string;
  question: string;
  hint?: string;
  actions?: ReactNode;
  height?: number;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("border-border bg-card space-y-3 rounded border p-4", className)}>
      <SectionHeader eyebrow={eyebrow} title={question} description={hint} actions={actions} />
      <div style={{ height }} className="w-full">
        {children}
      </div>
    </section>
  );
}

export interface TooltipRow {
  label: string;
  value: string;
  unit?: string;
  color?: string;
}

/**
 * Recharts hands its tooltip content an untyped payload; this renders a fixed shape instead,
 * so every chart's tooltip has the same hairline frame and the same tabular numerals.
 */
export function ChartTooltip({
  active,
  title,
  rows,
}: {
  active?: boolean;
  title: ReactNode;
  rows: TooltipRow[];
}) {
  if (!active || rows.length === 0) return null;

  return (
    <div className="border-border bg-popover text-popover-foreground min-w-40 rounded border px-2.5 py-2 text-xs shadow-sm">
      <p className="mb-1.5 font-medium">{title}</p>
      <dl className="space-y-1">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-4">
            <dt className="text-muted-foreground inline-flex items-center gap-1.5">
              {row.color ? (
                <span
                  aria-hidden
                  className="inline-block size-2 shrink-0 rounded-[1px]"
                  style={{ backgroundColor: row.color }}
                />
              ) : null}
              {row.label}
            </dt>
            <dd className="inline-flex items-baseline gap-1">
              <span className="font-mono tabular-nums">{row.value}</span>
              {row.unit ? (
                <span className="text-muted-foreground text-[0.6875rem]">{row.unit}</span>
              ) : null}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
