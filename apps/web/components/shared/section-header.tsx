import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The one structural flourish in the system: a kilometre-marker tick before a section label
 * (docs/DESIGN_SYSTEM.md §4). Used on section headers and nowhere else — that restraint is
 * what keeps it meaning something.
 */
export function SectionHeader({
  eyebrow,
  title,
  description,
  actions,
  className,
}: {
  eyebrow?: string;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-4", className)}>
      <div className="min-w-0">
        {eyebrow ? <p className="eyebrow section-tick mb-1">{eyebrow}</p> : null}
        <h2 className="text-foreground truncate text-base font-semibold tracking-tight">{title}</h2>
        {description ? (
          <p className="text-muted-foreground mt-0.5 text-sm">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <header className="border-border flex flex-wrap items-end justify-between gap-4 border-b pb-4">
      <div className="min-w-0">
        <h1 className="text-foreground text-[1.375rem] leading-tight font-semibold tracking-tight">
          {title}
        </h1>
        {description ? (
          <p className="text-muted-foreground mt-1 text-sm">{description}</p>
        ) : null}
        {children}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}
