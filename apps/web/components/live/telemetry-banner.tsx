"use client";

import { FlaskConical } from "lucide-react";

import { useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

/**
 * The honesty bar (ADR 004).
 *
 * `/live` is the one screen where a reader could plausibly believe they are looking at a real
 * fleet, so the page says otherwise permanently and without being asked. It is a single-line
 * rule in the warning token rather than an alert box: an alert invites dismissal and steals
 * height from the map, while a persistent bar reads as a property of the page.
 */
export function TelemetryBanner({ className }: { className?: string }) {
  const t = useTranslations();

  return (
    <p
      role="note"
      className={cn(
        "border-warning/40 bg-warning/10 text-warning flex items-center gap-2 rounded border px-3 py-1.5 text-[0.6875rem] font-medium tracking-[0.04em]",
        className,
      )}
    >
      <FlaskConical className="size-3.5 shrink-0" aria-hidden />
      <span className="truncate">{t.live.banner}</span>
    </p>
  );
}
