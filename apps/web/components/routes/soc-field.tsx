"use client";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

import { clampPercent } from "./route-request";

/**
 * A state-of-charge input: slider for the gesture, number field for the exact value.
 *
 * Both are needed. Someone exploring "what if I leave with less?" wants to drag; someone
 * reproducing a colleague's figure wants to type `68`. They are the same controlled value, and
 * the number field is the accessible label target so the pair announces as one control.
 *
 * The typed value is clamped on commit rather than on every keystroke, so deleting a digit to
 * retype it does not fight the user.
 */
export function SocField({
  id,
  label,
  value,
  onChange,
  min = 0,
  max = 100,
  step = 1,
  description,
  disabled = false,
  className,
}: {
  id: string;
  label: string;
  value: number;
  onChange: (next: number) => void;
  min?: number;
  max?: number;
  step?: number;
  description?: string;
  disabled?: boolean;
  className?: string;
}) {
  const t = useTranslations();

  return (
    <div className={cn("min-w-0", className)}>
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <Label htmlFor={id} className="text-[0.8125rem] font-medium">
          {label}
        </Label>
        <div className="flex items-baseline gap-1">
          <Input
            id={id}
            type="number"
            inputMode="numeric"
            min={min}
            max={max}
            step={step}
            value={String(value)}
            disabled={disabled}
            aria-describedby={description ? `${id}-hint` : undefined}
            onChange={(event) => {
              const next = Number(event.target.value);
              if (Number.isFinite(next)) onChange(clampPercent(next, min, max));
            }}
            className="h-7 w-16 px-1.5 text-right font-mono text-sm tabular-nums"
          />
          <span className="text-muted-foreground text-xs" aria-hidden>
            {t.units.percent}
          </span>
        </div>
      </div>

      <Slider
        value={[value]}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        aria-label={label}
        onValueChange={([next]) => {
          if (typeof next === "number") onChange(next);
        }}
      />

      {description ? (
        <p id={`${id}-hint`} className="text-muted-foreground mt-1.5 text-[0.6875rem]">
          {description}
        </p>
      ) : null}
    </div>
  );
}
