"use client";

import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A standing, non-dismissible notice — the provenance statements the engineering pages owe the
 * reader before they read a number.
 *
 * Deliberately *not* the `Alert` primitive: `role="alert"` is a live region, and announcing a
 * permanent caption as if it had just happened is noise for anyone using a screen reader. This
 * is a `<aside role="note">` with a real heading, styled as information rather than as a
 * failure — the notice explains the data, it does not report a problem.
 */
export function NoticePanel({
  icon: Icon,
  title,
  tone = "info",
  actions,
  children,
  className,
}: {
  icon: LucideIcon;
  title: string;
  tone?: "info" | "caution";
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  const tones = {
    info: { border: "border-primary/30 bg-primary/5", icon: "text-primary" },
    caution: { border: "border-warning/40 bg-warning/5", icon: "text-warning" },
  }[tone];

  return (
    <aside
      role="note"
      className={cn("grid grid-cols-[auto_1fr] gap-x-2.5 rounded border px-3 py-2.5", tones.border, className)}
    >
      <Icon className={cn("row-span-2 mt-0.5 size-4 shrink-0", tones.icon)} aria-hidden />
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <p className="text-foreground text-sm font-medium">{title}</p>
        {actions}
      </div>
      <div className="text-muted-foreground mt-1 text-xs leading-relaxed">{children}</div>
    </aside>
  );
}
