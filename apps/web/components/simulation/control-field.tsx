"use client";

import { useId, type ReactNode } from "react";

import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";

/**
 * One labelled parameter: condensed label, the current value in mono on the right, the control
 * underneath.
 *
 * The control is wrapped in a labelled `role="group"` because a slider thumb carries the
 * `slider` role but no name of its own — the group gives assistive technology something to
 * announce, and the visible read-out means the value is never hidden behind a drag.
 */
export function ControlField({
  label,
  value,
  unit,
  hint,
  children,
  className,
}: {
  label: string;
  value?: ReactNode;
  unit?: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  const labelId = useId();

  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <Label id={labelId} className="eyebrow">
          {label}
        </Label>
        {value != null ? (
          <span className="flex items-baseline gap-1">
            <span className="text-foreground font-mono text-sm tabular-nums">{value}</span>
            {unit ? <span className="text-muted-foreground text-xs">{unit}</span> : null}
          </span>
        ) : null}
      </div>
      <div role="group" aria-labelledby={labelId}>
        {children}
      </div>
      {hint ? <p className="text-muted-foreground text-xs">{hint}</p> : null}
    </div>
  );
}
