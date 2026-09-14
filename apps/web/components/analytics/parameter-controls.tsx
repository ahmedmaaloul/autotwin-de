"use client";

import type { ReactNode } from "react";
import { useId } from "react";

import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

/**
 * The parameter strip above an analysis.
 *
 * Both analytics tools are parameterised queries against PostGIS, and the parameters *are* the
 * analysis: a 5 km corridor and a 15 km corridor answer different questions. So the controls sit
 * above the result, always visible, with their current values shown as numbers — never as an
 * unlabelled slider position the reader has to estimate.
 */
export function ControlPanel({
  title,
  children,
  actions,
  className,
}: {
  title: string;
  children: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <section
      aria-label={title}
      className={cn("border-border bg-card rounded border px-4 py-3", className)}
    >
      <div className="mb-2.5 flex items-center justify-between gap-3">
        <p className="eyebrow">{title}</p>
        {actions}
      </div>
      <div className="grid gap-x-6 gap-y-4 sm:grid-cols-2 lg:grid-cols-4">{children}</div>
    </section>
  );
}

/** A labelled slider with its value rendered as a number, because a thumb position is not one. */
export function KmSlider({
  label,
  value,
  onChange,
  min,
  max,
  step = 1,
  unit,
  decimals = 0,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  min: number;
  max: number;
  step?: number;
  unit: string;
  decimals?: number;
}) {
  const locale = useLocale();

  return (
    <div role="group" aria-label={label} className="min-w-0">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <span className="text-muted-foreground text-xs">{label}</span>
        <span className="flex items-baseline gap-1">
          <span className="text-foreground font-mono text-sm tabular-nums">
            {formatNumber(value, locale, { decimals })}
          </span>
          <span className="text-muted-foreground text-[0.6875rem]">{unit}</span>
        </span>
      </div>
      <Slider
        value={[value]}
        min={min}
        max={max}
        step={step}
        onValueChange={(next) => onChange(next[0] ?? value)}
      />
      <div className="text-muted-foreground mt-1.5 flex justify-between font-mono text-[0.625rem] tabular-nums">
        <span>{formatNumber(min, locale, { decimals })}</span>
        <span>{formatNumber(max, locale, { decimals })}</span>
      </div>
    </div>
  );
}

/** Power classes a planner actually reasons in: AC, fast DC, HPC. */
export const POWER_OPTIONS = [11, 22, 50, 150, 300] as const;

export function PowerSelect({
  value,
  onChange,
  label,
}: {
  value: number;
  onChange: (value: number) => void;
  label: string;
}) {
  const locale = useLocale();
  const t = useTranslations();
  const id = useId();

  return (
    <div className="min-w-0">
      <Label htmlFor={id} className="text-muted-foreground mb-2 text-xs font-normal">
        {label}
      </Label>
      <Select value={String(value)} onValueChange={(next) => onChange(Number(next))}>
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {POWER_OPTIONS.map((option) => (
            <SelectItem
              key={option}
              value={String(option)}
              textValue={`${formatNumber(option, locale)} ${t.units.kw}`}
            >
              <span className="font-mono tabular-nums">{formatNumber(option, locale)}</span>
              <span className="text-muted-foreground ml-1 text-xs">{t.units.kw}</span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export function RouteSelect({
  value,
  onChange,
  label,
  options,
  disabled = false,
}: {
  value: string | null;
  onChange: (value: string) => void;
  label: string;
  options: { value: string; label: string }[];
  disabled?: boolean;
}) {
  const id = useId();

  return (
    <div className="min-w-0 lg:col-span-2">
      <Label htmlFor={id} className="text-muted-foreground mb-2 text-xs font-normal">
        {label}
      </Label>
      <Select value={value ?? undefined} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger id={id} className="w-full">
          <SelectValue placeholder={label} />
        </SelectTrigger>
        <SelectContent>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
