"use client";

import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { BUNDESLAENDER } from "@/lib/constants";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { Messages } from "@/lib/i18n/messages/de";
import type { Locale } from "@/lib/i18n/config";

import { categoryLabel } from "./charging-vocabulary";
import type { ChargingFilterState } from "./use-charging-filters";

interface Chip {
  key: string;
  label: string;
  value: string;
  clear: Partial<ChargingFilterState>;
}

/**
 * The active filters, spelled out.
 *
 * A row of controls above a result count leaves the reader guessing which of them is currently
 * biting. Naming each active constraint — and letting it be lifted on its own — is the
 * difference between "no results" being a dead end and being a question with an obvious next
 * move.
 */
export function FilterChips({
  filters,
  onClear,
  onReset,
}: {
  filters: ChargingFilterState;
  onClear: (patch: Partial<ChargingFilterState>) => void;
  onReset: () => void;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const chips = buildChips(filters, t, locale);

  if (chips.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="eyebrow mr-0.5">{t.charging.activeFilters}</span>
      {chips.map((chip) => (
        <span
          key={chip.key}
          className="border-border bg-card inline-flex items-center gap-1.5 rounded border py-0.5 pr-0.5 pl-2 text-xs"
        >
          <span className="text-muted-foreground">{chip.label}</span>
          <span className="font-medium">{chip.value}</span>
          <button
            type="button"
            onClick={() => onClear(chip.clear)}
            aria-label={`${t.charging.removeFilter}: ${chip.label} ${chip.value}`}
            className="hover:bg-muted focus-visible:ring-ring inline-flex size-4 items-center justify-center rounded-sm focus-visible:ring-2 focus-visible:outline-none"
          >
            <X className="size-3" aria-hidden />
          </button>
        </span>
      ))}
      <Button variant="ghost" size="sm" onClick={onReset} className="h-6 px-2 text-xs">
        {t.charging.resetFilters}
      </Button>
    </div>
  );
}

function buildChips(state: ChargingFilterState, t: Messages, locale: Locale): Chip[] {
  const chips: Chip[] = [];

  if (state.q) {
    chips.push({ key: "q", label: t.charging.searchLabel, value: state.q, clear: { q: "" } });
  }

  if (state.bundesland) {
    const match = BUNDESLAENDER.find((entry) => entry.code === state.bundesland);
    chips.push({
      key: "state",
      label: t.charging.bundesland,
      value: match?.name ?? state.bundesland,
      clear: { bundesland: null },
    });
  }

  if (state.operator) {
    chips.push({
      key: "operator",
      label: t.charging.operator,
      value: state.operator,
      clear: { operator: null },
    });
  }

  if (state.category) {
    chips.push({
      key: "category",
      label: t.charging.category,
      value: categoryLabel(state.category, t),
      clear: { category: null },
    });
  }

  if (state.minPowerKw > 0) {
    chips.push({
      key: "minPower",
      label: t.charging.minPower,
      value: `≥ ${formatNumber(state.minPowerKw, locale)} ${t.units.kw}`,
      clear: { minPowerKw: 0 },
    });
  }

  if (state.fastOnly) {
    chips.push({
      key: "fast",
      label: t.charging.category,
      value: t.charging.fastOnly,
      clear: { fastOnly: false },
    });
  }

  if (state.commissionedFrom !== null || state.commissionedTo !== null) {
    const from = state.commissionedFrom ?? t.charging.anyYear;
    const to = state.commissionedTo ?? t.charging.anyYear;
    chips.push({
      key: "years",
      label: t.charging.commissioningYear,
      value: `${from} – ${to}`,
      clear: { commissionedFrom: null, commissionedTo: null },
    });
  }

  return chips;
}
