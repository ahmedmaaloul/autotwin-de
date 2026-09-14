import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The dashboard's surface primitive.
 *
 * Deliberately not `components/ui/card`: the shadcn card is a rounded, ring-elevated consumer
 * surface, and docs/DESIGN_SYSTEM.md §4 asks for the opposite — a 0.25 rem radius, a hairline
 * border doing the work a shadow would do elsewhere, and cards that never float. This is that
 * surface, and it matches `MetricRow`, which already uses the same three classes.
 */
export function Panel({
  children,
  className,
  ariaLabelledBy,
}: {
  children: ReactNode;
  className?: string;
  ariaLabelledBy?: string;
}) {
  return (
    <section
      aria-labelledby={ariaLabelledBy}
      className={cn("border-border bg-card flex min-w-0 flex-col rounded border", className)}
    >
      {children}
    </section>
  );
}

export function PanelHeader({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("border-border border-b px-4 py-3", className)}>{children}</div>;
}

export function PanelBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("min-w-0 flex-1 p-4", className)}>{children}</div>;
}

/**
 * A measured value: mono numerals, unit as a separate muted node.
 *
 * Every number on this page goes through here or through `MetricCard`, which is what keeps the
 * unit out of the number string and the columns aligned on the decimal point
 * (docs/DESIGN_SYSTEM.md §3).
 */
export function Figure({
  value,
  unit,
  className,
  unitClassName,
}: {
  value: ReactNode;
  unit?: ReactNode;
  className?: string;
  unitClassName?: string;
}) {
  return (
    <span className="inline-flex items-baseline gap-1">
      <span className={cn("font-mono tabular-nums tracking-tight", className)}>{value}</span>
      {unit ? (
        <span className={cn("text-muted-foreground text-xs", unitClassName)}>{unit}</span>
      ) : null}
    </span>
  );
}

/**
 * A section title that wraps.
 *
 * `SectionHeader` truncates its title — right for a route name in a narrow column, wrong for
 * the question a panel exists to answer, which must stay readable at every width. Overriding
 * `white-space` on a child of the truncated heading lets the question wrap without touching
 * the shared component.
 */
export function WrappingTitle({ children }: { children: ReactNode }) {
  return <span className="block whitespace-normal">{children}</span>;
}
