"use client";

import { RotateCcw } from "lucide-react";
import { useId } from "react";

import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { BUNDESLAENDER } from "@/lib/constants";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { ChargingCategory } from "@/types/domain";

import { categoryBand, categoryLabel, Measure } from "./charging-vocabulary";
import { FilterChips } from "./filter-chips";
import { FilterSearch } from "./filter-search";
import { FilterSlider } from "./filter-slider";
import { OperatorCombobox, type OperatorOption } from "./operator-combobox";
import {
  CHARGING_CATEGORIES,
  POWER_MAX_KW,
  POWER_MIN_KW,
  POWER_STEP_KW,
  type ChargingFilterController,
} from "./use-charging-filters";

const ALL = "__all__";

/**
 * The filter bar for the charging explorer.
 *
 * Every control writes to the URL, so the visible state and the shareable state are the same
 * thing. Below the controls the active constraints are repeated as dismissible chips — the
 * controls say what *can* be narrowed, the chips say what *is*.
 */
export function ChargingFilters({
  controller,
  operators,
  operatorsLoading,
  yearBounds,
}: {
  controller: ChargingFilterController;
  operators: OperatorOption[];
  operatorsLoading: boolean;
  yearBounds: readonly [number, number];
}) {
  const t = useTranslations();
  const locale = useLocale();
  const fastId = useId();
  const { filters, setFilters, resetFilters, isFiltered } = controller;

  const [minYear, maxYear] = yearBounds;
  const years = [filters.commissionedFrom ?? minYear, filters.commissionedTo ?? maxYear];

  return (
    <section aria-label={t.common.filters} className="border-border bg-card space-y-4 rounded border p-4">
      <div className="flex items-start justify-between gap-4">
        <p className="eyebrow section-tick">{t.charging.explorerEyebrow}</p>
        <Button
          variant="ghost"
          size="sm"
          onClick={resetFilters}
          disabled={!isFiltered}
          className="h-7 px-2 text-xs"
        >
          <RotateCcw className="size-3.5" aria-hidden />
          {t.charging.resetFilters}
        </Button>
      </div>

      <div className="grid gap-x-5 gap-y-4 md:grid-cols-2 xl:grid-cols-4">
        <FilterSearch value={filters.q} onSubmit={(q) => setFilters({ q })} />

        <LabelledSelect
          label={t.charging.bundesland}
          placeholder={t.charging.allBundeslaender}
          value={filters.bundesland}
          onChange={(bundesland) => setFilters({ bundesland })}
          options={BUNDESLAENDER.map((entry) => ({ value: entry.code, label: entry.name }))}
          allLabel={t.charging.allBundeslaender}
        />

        <OperatorCombobox
          value={filters.operator}
          options={operators}
          loading={operatorsLoading}
          onChange={(operator) => setFilters({ operator })}
        />

        <LabelledSelect
          label={t.charging.category}
          placeholder={t.charging.allCategories}
          value={filters.category}
          onChange={(category) => setFilters({ category: category as ChargingCategory | null })}
          options={CHARGING_CATEGORIES.map((category) => ({
            value: category,
            label: `${categoryLabel(category, t)} · ${categoryBand(category, t)}`,
          }))}
          allLabel={t.charging.allCategories}
        />

        <FilterSlider
          label={t.charging.minPower}
          min={POWER_MIN_KW}
          max={POWER_MAX_KW}
          step={POWER_STEP_KW}
          value={[filters.minPowerKw]}
          onCommit={([minPowerKw]) => setFilters({ minPowerKw: minPowerKw ?? 0 })}
          readout={([draft]) =>
            draft ? (
              <Measure
                value={`≥ ${formatNumber(draft, locale)}`}
                unit={t.units.kw}
                valueClassName="text-xs"
              />
            ) : (
              <span className="text-muted-foreground text-xs">{t.charging.anyPower}</span>
            )
          }
        />

        <FilterSlider
          label={t.charging.commissioningYear}
          min={minYear}
          max={maxYear}
          step={1}
          value={years}
          onCommit={([from, to]) =>
            setFilters({
              commissionedFrom: from != null && from > minYear ? from : null,
              commissionedTo: to != null && to < maxYear ? to : null,
            })
          }
          readout={([from, to]) => (
            <span className="font-mono text-xs tabular-nums">
              {from} – {to}
            </span>
          )}
          className="md:col-span-2 xl:col-span-1"
        />

        <div className="flex items-end pb-1">
          <div className="border-border flex w-full items-center justify-between gap-3 rounded border px-3 py-2">
            <label htmlFor={fastId} className="text-xs leading-tight">
              {t.charging.fastOnly}
              <span className="text-muted-foreground mt-0.5 block font-mono text-[0.6875rem] tabular-nums">
                ≥ 22 {t.units.kw}
              </span>
            </label>
            <Switch
              id={fastId}
              checked={filters.fastOnly}
              onCheckedChange={(fastOnly) => setFilters({ fastOnly })}
            />
          </div>
        </div>
      </div>

      <FilterChips filters={filters} onClear={setFilters} onReset={resetFilters} />
    </section>
  );
}

function LabelledSelect({
  label,
  placeholder,
  value,
  onChange,
  options,
  allLabel,
}: {
  label: string;
  placeholder: string;
  value: string | null;
  onChange: (next: string | null) => void;
  options: { value: string; label: string }[];
  allLabel: string;
}) {
  const id = useId();
  // The label is passed to `SelectValue` explicitly rather than left to Radix's
  // selected-item portal: that way the trigger reads correctly in the server-rendered HTML
  // instead of being blank until the client has mounted the (closed) item list.
  const selectedLabel = value ? (options.find((o) => o.value === value)?.label ?? value) : allLabel;

  return (
    <div className="min-w-0 space-y-2">
      <label htmlFor={id} className="eyebrow block">
        {label}
      </label>
      <Select
        value={value ?? ALL}
        onValueChange={(next) => onChange(next === ALL ? null : next)}
      >
        <SelectTrigger id={id} className="h-8 w-full text-xs">
          <SelectValue placeholder={placeholder}>{selectedLabel}</SelectValue>
        </SelectTrigger>
        <SelectContent className="max-h-72">
          <SelectItem value={ALL} className="text-xs">
            {allLabel}
          </SelectItem>
          {options.map((option) => (
            <SelectItem key={option.value} value={option.value} className="text-xs">
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
