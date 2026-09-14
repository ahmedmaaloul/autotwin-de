"use client";

import { useMemo } from "react";

import { ScrollArea } from "@/components/ui/scroll-area";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { VehicleLive } from "@/types/domain";

/** Hard cap. The map is the instrument for the whole fleet; this list is a shortlist. */
const MAX_ROWS = 40;

/**
 * The vehicles most likely to need attention, as a shortlist.
 *
 * A live table of several hundred rows re-rendering at telemetry rate is the classic way to
 * make a dashboard stutter, so this one is sorted by state of charge and cut to 40 — the
 * lowest batteries, which is the ordering an operator actually wants — and the page says so
 * underneath. The full fleet lives on the map, where one GeoJSON source carries all of it.
 */
export function VehicleList({
  vehicles,
  selectedId,
  onSelect,
  className,
}: {
  vehicles: VehicleLive[];
  selectedId: string | null;
  onSelect: (vehicleId: string) => void;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const rows = useMemo(
    () =>
      [...vehicles]
        .sort((a, b) => a.battery_soc_percent - b.battery_soc_percent)
        .slice(0, MAX_ROWS),
    [vehicles],
  );

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className="border-border flex items-baseline justify-between gap-2 border-b px-4 py-2">
        <span className="eyebrow section-tick">{t.live.fleet}</span>
        <span className="flex items-baseline gap-1">
          <span className="text-foreground font-mono text-xs tabular-nums">{rows.length}</span>
          <span className="text-muted-foreground text-xs">/</span>
          <span className="text-foreground font-mono text-xs tabular-nums">{vehicles.length}</span>
          <span className="text-muted-foreground text-xs">{t.units.vehicles}</span>
        </span>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <ul className="divide-border divide-y">
          {rows.map((vehicle) => {
            const selected = vehicle.vehicle_id === selectedId;
            const tone =
              vehicle.battery_soc_percent < 10
                ? "text-danger"
                : vehicle.battery_soc_percent < 25
                  ? "text-warning"
                  : "text-foreground";
            return (
              <li key={vehicle.vehicle_id}>
                <button
                  type="button"
                  onClick={() => onSelect(vehicle.vehicle_id)}
                  aria-current={selected ? "true" : undefined}
                  className={cn(
                    "hover:bg-muted flex w-full items-baseline justify-between gap-3 px-4 py-1.5 text-left transition-colors",
                    selected && "bg-muted",
                  )}
                >
                  <span className="text-foreground truncate font-mono text-xs">
                    {vehicle.vehicle_id}
                  </span>
                  <span className="flex shrink-0 items-baseline gap-3">
                    <span className="flex items-baseline gap-1">
                      <span className={cn("font-mono text-xs tabular-nums", tone)}>
                        {formatNumber(vehicle.battery_soc_percent, locale, { decimals: 0 })}
                      </span>
                      <span className="text-muted-foreground text-[0.6875rem]">
                        {t.units.percent}
                      </span>
                    </span>
                    <span className="flex items-baseline gap-1">
                      <span className="text-muted-foreground font-mono text-xs tabular-nums">
                        {formatNumber(vehicle.speed_kmh, locale, { decimals: 0 })}
                      </span>
                      <span className="text-muted-foreground text-[0.6875rem]">{t.units.kmh}</span>
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      </ScrollArea>

      <p className="text-muted-foreground border-border border-t px-4 py-2 text-xs">
        {t.live.fleetSorted}. {t.live.fleetCapped}
      </p>
    </div>
  );
}
