"use client";

import type { ReactNode } from "react";

import { SectionHeader } from "@/components/shared/section-header";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

/**
 * The chart vocabulary shared by the engineering pages (`/analytics`, `/ml`).
 *
 * Recharts is configured once here rather than per chart, for two reasons. The obvious one is
 * consistency: one axis colour, one grid weight, one tooltip shape. The less obvious one is
 * that every chart in this product has to answer a *stated question* — so `ChartPanel` makes
 * the question a required prop. A chart without one is decoration, and decoration is exactly
 * what docs/DESIGN_SYSTEM.md forbids.
 *
 * Colours come only from the CSS tokens. MapLibre needs concrete hexes (see `lib/map/style.ts`);
 * SVG does not, so charts read `var(--…)` directly and follow the theme without a re-render.
 */

export const AXIS_TICK = {
  fill: "var(--muted-foreground)",
  fontSize: 11,
  fontFamily: "var(--font-mono)",
} as const;

export const AXIS_LINE = { stroke: "var(--border)" } as const;

export const GRID_STROKE = "var(--border)";

/** Axis titles are set in the UI face, not the mono face — they are words, not measurements. */
export const AXIS_LABEL = {
  fill: "var(--muted-foreground)",
  fontSize: 11,
  fontFamily: "var(--font-sans)",
} as const;

/**
 * Axis titles, positioned once.
 *
 * Every chart in this product states what its axes measure — a bare tick scale of "12, 16, 20"
 * is a puzzle, not a measurement.
 */
export function axisLabel(value: string, orientation: "x" | "y") {
  return orientation === "x"
    ? { value, position: "insideBottom" as const, offset: -14, ...AXIS_LABEL }
    : {
        value,
        angle: -90,
        position: "insideLeft" as const,
        offset: 6,
        ...AXIS_LABEL,
        style: { textAnchor: "middle" as const },
      };
}

export function ChartPanel({
  eyebrow,
  title,
  question,
  actions,
  footer,
  height = 260,
  children,
  className,
}: {
  eyebrow?: string;
  title: string;
  /** The question this chart answers. Required by design. */
  question: string;
  actions?: ReactNode;
  footer?: ReactNode;
  height?: number;
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card className={cn("gap-0 rounded border p-0 shadow-none", className)}>
      <div className="border-border border-b px-4 py-3">
        <SectionHeader eyebrow={eyebrow} title={title} description={question} actions={actions} />
      </div>
      <div className="px-2 py-3" style={{ height }}>
        {children}
      </div>
      {footer ? (
        <div className="border-border text-muted-foreground border-t px-4 py-2 text-xs">
          {footer}
        </div>
      ) : null}
    </Card>
  );
}

/**
 * Tooltip body.
 *
 * Values are mono and right-aligned with the unit as a separate muted node, exactly as they are
 * everywhere else — a tooltip is the one place a dashboard usually forgets that rule.
 */
export function ChartTooltip({
  title,
  rows,
}: {
  title: ReactNode;
  rows: { label: string; value: string; unit?: string; swatch?: string }[];
}) {
  return (
    <div className="border-border bg-popover text-popover-foreground min-w-40 rounded border px-2.5 py-2 text-xs shadow-sm">
      <p className="text-foreground mb-1.5 font-medium">{title}</p>
      <ul className="space-y-1">
        {rows.map((row) => (
          <li key={row.label} className="flex items-baseline justify-between gap-4">
            <span className="text-muted-foreground inline-flex items-center gap-1.5">
              {row.swatch ? (
                <span
                  className="inline-block size-2 shrink-0 rounded-[1px]"
                  style={{ backgroundColor: row.swatch }}
                  aria-hidden
                />
              ) : null}
              {row.label}
            </span>
            <span className="text-foreground font-mono tabular-nums">
              {row.value}
              {row.unit ? <span className="text-muted-foreground ml-1 text-[0.6875rem]">{row.unit}</span> : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Inline legend — colour is never the only channel, so every swatch carries its word. */
export function ChartLegend({
  items,
  className,
}: {
  items: { color: string; label: string; dashed?: boolean }[];
  className?: string;
}) {
  return (
    <ul className={cn("flex flex-wrap items-center gap-x-4 gap-y-1", className)}>
      {items.map((item) => (
        <li key={item.label} className="text-muted-foreground flex items-center gap-1.5 text-xs">
          <span
            className={cn("inline-block h-0.5 w-3.5 shrink-0", item.dashed && "opacity-60")}
            style={{
              backgroundColor: item.dashed ? "transparent" : item.color,
              backgroundImage: item.dashed
                ? `repeating-linear-gradient(90deg, ${item.color} 0 3px, transparent 3px 6px)`
                : undefined,
            }}
            aria-hidden
          />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

/** A chart with no rows must say so; an empty plot area reads as a broken component. */
export function ChartEmpty({ message }: { message: string }) {
  return (
    <div className="text-muted-foreground flex size-full items-center justify-center px-4 text-center text-xs">
      {message}
    </div>
  );
}
