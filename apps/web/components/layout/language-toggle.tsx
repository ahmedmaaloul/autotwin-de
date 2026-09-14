"use client";

import { Languages } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { LOCALES, LOCALE_LABELS } from "@/lib/i18n/config";
import { useLocale, useSetLocale, useTranslations } from "@/providers/locale-provider";

export function LanguageToggle() {
  const t = useTranslations();
  const locale = useLocale();
  const setLocale = useSetLocale();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-1.5 px-2 font-mono text-xs"
          aria-label={t.common.language}
          data-testid="language-toggle"
        >
          <Languages className="size-4" aria-hidden />
          {locale.toUpperCase()}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-32">
        {LOCALES.map((candidate) => (
          <DropdownMenuItem
            key={candidate}
            onSelect={() => setLocale(candidate)}
            data-active={candidate === locale}
            className="data-[active=true]:text-primary data-[active=true]:font-medium"
          >
            <span className="font-mono text-xs">{candidate.toUpperCase()}</span>
            {LOCALE_LABELS[candidate]}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
