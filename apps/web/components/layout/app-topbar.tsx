"use client";

import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";

import { LanguageToggle } from "@/components/layout/language-toggle";
import { ThemeToggle } from "@/components/layout/theme-toggle";
import { StatusBadge, type StatusTone } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { SidebarTrigger } from "@/components/ui/sidebar";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { apiFetchRoot } from "@/lib/api/client";
import { useTranslations } from "@/providers/locale-provider";

import { GlobalSearch, useGlobalSearch } from "./global-search";

interface ReadyResponse {
  status: string;
  checks?: Record<string, boolean | string | null>;
}

/**
 * System status in the top bar is a deliberate choice: on a platform whose whole point is data
 * provenance, "is the backend actually answering?" belongs in the chrome, not buried on a
 * status page nobody opens.
 */
function useSystemStatus() {
  return useQuery({
    queryKey: ["ready"],
    queryFn: () => apiFetchRoot<ReadyResponse>("/ready", { timeoutMs: 5000 }),
    refetchInterval: 30_000,
    retry: false,
    staleTime: 15_000,
  });
}

export function AppTopbar() {
  const t = useTranslations();
  const { open, setOpen } = useGlobalSearch();
  const { data, isError, isLoading } = useSystemStatus();

  const tone: StatusTone = isLoading
    ? "neutral"
    : isError
      ? "failed"
      : data?.status === "ready"
        ? "healthy"
        : "degraded";

  const label = isLoading
    ? t.common.loading
    : isError
      ? t.states.offlineTitle
      : data?.status === "ready"
        ? t.status.healthy
        : t.status.degraded;

  return (
    <header className="border-border bg-background/95 supports-[backdrop-filter]:bg-background/80 sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b px-4 backdrop-blur">
      <SidebarTrigger className="-ml-1" />
      <Separator orientation="vertical" className="mr-1 hidden h-5 sm:block" />

      <Button
        variant="outline"
        size="sm"
        onClick={() => setOpen(true)}
        className="text-muted-foreground h-8 w-full max-w-sm min-w-0 shrink justify-start gap-2 px-2 font-normal"
      >
        <Search className="size-3.5" aria-hidden />
        <span className="min-w-0 truncate text-xs">{t.common.searchPlaceholder}</span>
        <kbd className="bg-muted text-muted-foreground ml-auto hidden rounded border px-1 font-mono text-[0.625rem] sm:inline-block">
          ⌘K
        </kbd>
      </Button>

      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="hidden sm:inline-flex">
              <StatusBadge status={tone} label={label} pulse={tone === "healthy"} />
            </span>
          </TooltipTrigger>
          <TooltipContent align="end" className="max-w-xs">
            <p className="mb-1 font-medium">{t.system.title}</p>
            {isError ? (
              <p>{t.states.offlineBody}</p>
            ) : (
              <ul className="space-y-0.5 font-mono text-[0.6875rem]">
                {Object.entries(data?.checks ?? {}).map(([key, value]) => (
                  <li key={key} className="flex justify-between gap-4">
                    <span className="opacity-70">{key}</span>
                    <span>{value === true ? "ok" : value === false ? "down" : String(value)}</span>
                  </li>
                ))}
              </ul>
            )}
          </TooltipContent>
        </Tooltip>

        <Separator orientation="vertical" className="hidden h-5 sm:block" />
        <LanguageToggle />
        <ThemeToggle />
      </div>

      <GlobalSearch open={open} onOpenChange={setOpen} />
    </header>
  );
}
