"use client";

import { ArrowDown, ArrowUp } from "lucide-react";

import { SectionHeader } from "@/components/shared/section-header";
import { Card } from "@/components/ui/card";
import { formatDelta } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { EnergyDriver } from "@/types/domain";

/**
 * Why this journey costs what it costs.
 *
 * The drivers come from `autotwin_ml.insights`, which decomposes the prediction using the
 * physical model's own terms plus counterfactual re-runs ("what would this trip have cost at
 * 20 °C?"). That makes the attribution a computation rather than a narrative, and it needs no
 * LLM — the optional copilot only verbalises what this already contains (BUILD_SPEC §11, §76).
 *
 * Bars are scaled to the largest absolute contribution so the ranking is legible even when
 * every factor is small.
 */
export function EnergyImpactCard({
  headline,
  drivers,
  className,
}: {
  headline: string;
  drivers: EnergyDriver[];
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const maxAbs = Math.max(1, ...drivers.map((driver) => Math.abs(driver.delta_percent)));
  const increasing = drivers.filter((driver) => driver.delta_percent > 0);
  const decreasing = drivers.filter((driver) => driver.delta_percent < 0);

  return (
    <Card className={cn("gap-0 rounded p-0 shadow-none", className)}>
      <div className="border-border border-b px-4 py-3">
        <SectionHeader eyebrow={t.routes.explanation} title={headline} />
      </div>

      <div className="space-y-4 px-4 py-3">
        <DriverGroup
          title={t.routes.increasing}
          icon={ArrowUp}
          tone="text-danger"
          drivers={increasing}
          maxAbs={maxAbs}
          locale={locale}
        />
        <DriverGroup
          title={t.routes.decreasing}
          icon={ArrowDown}
          tone="text-success"
          drivers={decreasing}
          maxAbs={maxAbs}
          locale={locale}
        />
        {drivers.length === 0 ? (
          <p className="text-muted-foreground text-xs">{t.common.noData}</p>
        ) : null}
      </div>
    </Card>
  );
}

function DriverGroup({
  title,
  icon: Icon,
  tone,
  drivers,
  maxAbs,
  locale,
}: {
  title: string;
  icon: typeof ArrowUp;
  tone: string;
  drivers: EnergyDriver[];
  maxAbs: number;
  locale: "de" | "en";
}) {
  if (drivers.length === 0) return null;

  return (
    <div>
      <p className={cn("eyebrow mb-1.5 flex items-center gap-1", tone)}>
        <Icon className="size-3" aria-hidden />
        {title}
      </p>
      <ul className="space-y-1.5">
        {drivers
          .slice()
          .sort((a, b) => Math.abs(b.delta_percent) - Math.abs(a.delta_percent))
          .map((driver) => (
            <li key={driver.factor} className="grid grid-cols-[1fr_auto] items-center gap-x-3">
              <div className="min-w-0">
                <p className="truncate text-xs">
                  {locale === "de" ? driver.label_de : driver.label_en}
                </p>
                <div className="bg-muted mt-1 h-1 w-full overflow-hidden rounded-full">
                  <div
                    className={cn(
                      "h-full rounded-full transition-[width] duration-200 motion-reduce:transition-none",
                      driver.delta_percent > 0 ? "bg-danger" : "bg-success",
                    )}
                    style={{ width: `${(Math.abs(driver.delta_percent) / maxAbs) * 100}%` }}
                  />
                </div>
              </div>
              <span
                className={cn(
                  "font-mono text-xs tabular-nums",
                  driver.delta_percent > 0 ? "text-danger" : "text-success",
                )}
              >
                {formatDelta(driver.delta_percent, locale)}
              </span>
            </li>
          ))}
      </ul>
    </div>
  );
}
