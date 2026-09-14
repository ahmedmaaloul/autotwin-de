"use client";

import { AlertTriangle, DatabaseZap, Inbox, RefreshCw, WifiOff } from "lucide-react";
import type { ReactNode } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api/client";
import type { DataMode } from "@/lib/api/client";
import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

/**
 * Every page owes the reader a loading, empty and error state — a blank panel is a bug, not a
 * state (docs/DESIGN_SYSTEM.md §10). These are the three, plus the degraded-source notice that
 * makes the provider fallback chain visible.
 */

export function LoadingSkeleton({
  rows = 3,
  className,
}: {
  rows?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2", className)} role="status" aria-busy="true">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-9 w-full" />
      ))}
      <span className="sr-only">Wird geladen</span>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  icon: Icon = Inbox,
  className,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  icon?: typeof Inbox;
  className?: string;
}) {
  const t = useTranslations();
  return (
    <div
      className={cn(
        "border-border text-muted-foreground flex flex-col items-center justify-center gap-2 rounded border border-dashed px-6 py-10 text-center",
        className,
      )}
    >
      <Icon className="size-5 opacity-50" aria-hidden />
      <p className="text-foreground text-sm font-medium">{title ?? t.states.emptyTitle}</p>
      <p className="max-w-sm text-xs">{description ?? t.states.emptyBody}</p>
      {action}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const t = useTranslations();
  const isNetwork = error instanceof ApiError && error.status === 0;
  const apiError = error instanceof ApiError ? error : null;

  return (
    <Alert variant="destructive" className={cn("items-start", className)}>
      {isNetwork ? <WifiOff className="size-4" /> : <AlertTriangle className="size-4" />}
      <AlertTitle>{isNetwork ? t.states.offlineTitle : t.states.errorTitle}</AlertTitle>
      <AlertDescription className="space-y-2">
        <p>{isNetwork ? t.states.offlineBody : (apiError?.message ?? t.states.errorBody)}</p>
        {apiError?.requestId ? (
          <p className="font-mono text-[0.6875rem] opacity-70">
            request-id: {apiError.requestId}
          </p>
        ) : null}
        {onRetry ? (
          <Button size="sm" variant="outline" onClick={onRetry} className="mt-1">
            <RefreshCw className="size-3.5" aria-hidden />
            {t.common.retry}
          </Button>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}

/**
 * Shown when the API answered from cache or a fixture instead of the live source.
 *
 * This is the user-visible half of the provider fallback chain (ADR 005): the platform keeps
 * working when Bundesnetzagentur or DWD is unreachable, and it says so rather than quietly
 * presenting stale numbers as current ones.
 */
export function DataModeNotice({
  mode,
  className,
}: {
  mode: DataMode | null | undefined;
  className?: string;
}) {
  const t = useTranslations();
  if (!mode || mode === "live") return null;

  return (
    <Alert className={cn("border-warning/30 bg-warning/5 py-2", className)}>
      <DatabaseZap className="text-warning size-4" />
      <AlertTitle className="text-warning text-xs">{t.provenance.degradedTitle}</AlertTitle>
      <AlertDescription className="text-xs">
        {mode === "cache" ? t.provenance.degradedCache : t.provenance.degradedFixture}
      </AlertDescription>
    </Alert>
  );
}
