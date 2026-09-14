"use client";

import { Check, ChevronsUpDown } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

export interface OperatorOption {
  operator: string;
  stations: number;
}

/**
 * Operator picker.
 *
 * There are hundreds of operators in the Bundesnetzagentur register, so a `<select>` would be
 * unusable — this is a searchable list. Each row carries the operator's station count, which
 * turns picking a filter into reading a ranking: the reader learns that EnBW is an order of
 * magnitude bigger than the regional Stadtwerke *while* choosing.
 */
export function OperatorCombobox({
  value,
  options,
  loading = false,
  onChange,
}: {
  value: string | null;
  options: OperatorOption[];
  loading?: boolean;
  onChange: (operator: string | null) => void;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const [open, setOpen] = useState(false);

  const select = (operator: string | null) => {
    onChange(operator);
    setOpen(false);
  };

  return (
    <div className="min-w-0 space-y-2">
      <span className="eyebrow block" id="charging-operator-label">
        {t.charging.operator}
      </span>
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            variant="outline"
            role="combobox"
            aria-expanded={open}
            aria-labelledby="charging-operator-label"
            disabled={loading && options.length === 0}
            className="h-8 w-full justify-between px-2 text-xs font-normal"
          >
            <span className={cn("truncate", !value && "text-muted-foreground")}>
              {value ?? t.charging.allOperators}
            </span>
            <ChevronsUpDown className="size-3.5 shrink-0 opacity-50" aria-hidden />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-[min(22rem,calc(100vw-2rem))] p-0">
          <Command>
            <CommandInput placeholder={t.charging.operatorSearch} className="h-9 text-xs" />
            <CommandList className="scrollbar-thin max-h-64">
              <CommandEmpty className="px-3 py-4 text-xs">{t.charging.operatorEmpty}</CommandEmpty>
              <CommandGroup>
                <CommandItem value="__all__" onSelect={() => select(null)} className="text-xs">
                  <Check
                    className={cn("size-3.5", value === null ? "opacity-100" : "opacity-0")}
                    aria-hidden
                  />
                  {t.charging.allOperators}
                </CommandItem>
                {options.map((option) => (
                  <CommandItem
                    key={option.operator}
                    value={option.operator}
                    onSelect={() => select(option.operator)}
                    className="text-xs"
                  >
                    <Check
                      className={cn(
                        "size-3.5",
                        value === option.operator ? "opacity-100" : "opacity-0",
                      )}
                      aria-hidden
                    />
                    <span className="min-w-0 flex-1 truncate">{option.operator}</span>
                    <span className="text-muted-foreground font-mono text-[0.6875rem] tabular-nums">
                      {formatNumber(option.stations, locale)}
                    </span>
                  </CommandItem>
                ))}
              </CommandGroup>
            </CommandList>
          </Command>
          <p className="border-border text-muted-foreground border-t px-3 py-2 text-[0.6875rem]">
            {t.charging.operatorHint}
          </p>
        </PopoverContent>
      </Popover>
    </div>
  );
}
