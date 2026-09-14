"use client";

import { useId, useState, type ReactNode } from "react";

import { Slider } from "@/components/ui/slider";
import { cn } from "@/lib/utils";

/**
 * A slider whose committed value lives in the URL.
 *
 * Dragging must feel immediate, but writing the URL on every pointer move would fire a request
 * per pixel. So the thumb follows a local draft while the pointer is down and the URL is
 * written on `onValueCommit` only.
 *
 * The draft is re-synced from the committed value *during render* rather than in an effect:
 * when a chip is dismissed or the filters are reset, the URL changes underneath this component
 * and the thumb has to follow. React's documented "adjust state when a prop changes" pattern
 * does that in one pass, keeps keyboard focus on the thumb (a remount via `key` would throw it
 * away, breaking arrow-key adjustment), and never runs a setState inside an effect body.
 */
export function FilterSlider({
  label,
  value,
  onCommit,
  min,
  max,
  step = 1,
  readout,
  className,
}: {
  label: string;
  /** The committed value — one number, or two for a range. */
  value: number[];
  onCommit: (next: number[]) => void;
  min: number;
  max: number;
  step?: number;
  /** Renders the live draft, so the number beside the label tracks the thumb. */
  readout: (draft: number[]) => ReactNode;
  className?: string;
}) {
  const id = useId();
  const [draft, setDraft] = useState(value);
  const [committed, setCommitted] = useState(value);

  if (!sameValues(committed, value)) {
    setCommitted(value);
    setDraft(value);
  }

  return (
    <div className={cn("min-w-0 space-y-2", className)}>
      <div className="flex items-baseline justify-between gap-2">
        <label htmlFor={id} className="eyebrow">
          {label}
        </label>
        {readout(draft)}
      </div>
      <Slider
        id={id}
        aria-label={label}
        value={draft}
        min={min}
        max={max}
        step={step}
        onValueChange={setDraft}
        onValueCommit={onCommit}
        className="py-1.5"
      />
    </div>
  );
}

function sameValues(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((entry, index) => entry === b[index]);
}
