"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTranslations } from "@/providers/locale-provider";

/**
 * The trigger icon is chosen by CSS from the `.dark` class rather than by React state.
 *
 * That removes the whole mounted/hydration dance: the server renders both icons, the class on
 * `<html>` decides which one is visible, and there is never a frame showing the wrong one.
 */
export function ThemeToggle() {
  const t = useTranslations();
  const { setTheme, theme } = useTheme();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="size-8" aria-label={t.common.theme}>
          <Sun className="size-4 dark:hidden" aria-hidden />
          <Moon className="hidden size-4 dark:block" aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-36">
        {(
          [
            { value: "light", Icon: Sun, label: t.common.themeLight },
            { value: "dark", Icon: Moon, label: t.common.themeDark },
            { value: "system", Icon: Monitor, label: t.common.themeSystem },
          ] as const
        ).map(({ value, Icon, label }) => (
          <DropdownMenuItem
            key={value}
            onSelect={() => setTheme(value)}
            data-active={theme === value}
            className="data-[active=true]:text-primary data-[active=true]:font-medium"
          >
            <Icon className="size-4" aria-hidden />
            {label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
