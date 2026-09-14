"use client";

import { Clock } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useMinuteTick } from "@/hooks/use-minute-tick";
import { useMounted } from "@/hooks/use-mounted";
import { formatDateTime, formatRelativeAge } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

/**
 * Relative age of a dataset — `vor 18 Min. aktualisiert`.
 *
 * The value is computed client-side only: the server and the browser are never at exactly the
 * same instant, so rendering a relative time during SSR produces a hydration mismatch. It
 * refreshes from a single page-wide minute clock — these sources update every ten minutes at
 * best, so a per-second ticker would be false precision *and* needless main-thread work.
 *
 * The absolute timestamp in the tooltip is the precise value; the relative one is glanceable.
 */
export function DataFreshness({
  timestamp,
  className,
  showIcon = true,
}: {
  timestamp: string | Date | null | undefined;
  className?: string;
  showIcon?: boolean;
}) {
  const locale = useLocale();
  const t = useTranslations();
  const mounted = useMounted();
  // Depending on the tick is what re-renders this component once a minute.
  useMinuteTick();
  const relative = mounted && timestamp ? formatRelativeAge(timestamp, locale) : null;

  if (!timestamp) {
    return <span className={cn("text-muted-foreground text-xs", className)}>{t.provenance.never}</span>;
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "text-muted-foreground inline-flex cursor-help items-center gap-1 text-xs",
            className,
          )}
        >
          {showIcon ? <Clock className="size-3" aria-hidden /> : null}
          <span suppressHydrationWarning>{relative ?? "…"}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {t.common.lastUpdated}: {formatDateTime(timestamp, locale, { dateStyle: "long", timeStyle: "medium" })}
      </TooltipContent>
    </Tooltip>
  );
}
