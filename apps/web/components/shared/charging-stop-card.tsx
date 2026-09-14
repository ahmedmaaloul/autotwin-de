"use client";

import { BatteryCharging, Clock, MapPin, Zap } from "lucide-react";

import { Card } from "@/components/ui/card";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { ChargingStop } from "@/types/domain";

/**
 * One recommended charging stop.
 *
 * The rationale line is the point of this component. A recommendation without a reason is an
 * oracle, and an engineer reading a planning tool needs to see *why* this site beat the others —
 * so the backend produces `rationale_de`/`rationale_en` from the actual numbers that decided it
 * (power, detour, arrival time), and the card shows it directly under the figures.
 */
export function ChargingStopCard({
  stop,
  index,
  className,
}: {
  stop: ChargingStop;
  index: number;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const name = stop.station.operator ?? stop.station.city ?? t.charging.station;
  const address = [
    [stop.station.street, stop.station.house_number].filter(Boolean).join(" "),
    [stop.station.postal_code, stop.station.city].filter(Boolean).join(" "),
  ]
    .filter(Boolean)
    .join(", ");

  return (
    <Card className={cn("gap-0 rounded border p-0 shadow-none", className)}>
      <div className="border-border flex items-start justify-between gap-3 border-b px-4 py-3">
        <div className="min-w-0">
          <p className="eyebrow mb-0.5">
            {t.routes.chargingStops} {index + 1}
          </p>
          <h3 className="truncate text-sm font-semibold tracking-tight">{name}</h3>
          {address ? (
            <p className="text-muted-foreground mt-0.5 flex items-center gap-1 truncate text-xs">
              <MapPin className="size-3 shrink-0" aria-hidden />
              {address}
            </p>
          ) : null}
        </div>
        <div className="text-primary flex shrink-0 items-baseline gap-1">
          <Zap className="size-3.5 self-center" aria-hidden />
          <span className="font-mono text-lg leading-none font-medium tabular-nums">
            {formatNumber(stop.max_power_kw, locale)}
          </span>
          <span className="text-muted-foreground text-xs">kW</span>
        </div>
      </div>

      <dl className="divide-border grid grid-cols-2 divide-x sm:grid-cols-4">
        <Fact
          label={t.routes.distance}
          value={`${formatNumber(stop.offset_km, locale)} km`}
          sub={`+${formatNumber(stop.detour_km, locale, { decimals: 1 })} km ${locale === "de" ? "Umweg" : "detour"}`}
        />
        <Fact
          label={t.live.soc}
          value={`${formatNumber(stop.arrival_soc_percent, locale)} → ${formatNumber(stop.departure_soc_percent, locale)} %`}
          icon={BatteryCharging}
        />
        <Fact
          label={t.routes.duration}
          value={`${formatNumber(stop.charge_time_min, locale)} min`}
          icon={Clock}
        />
        <Fact
          label={t.routes.energyRequired}
          value={`${formatNumber(stop.energy_added_kwh, locale, { decimals: 1 })} kWh`}
          sub={`Ø ${formatNumber(stop.avg_power_kw, locale)} kW`}
        />
      </dl>

      <p className="text-muted-foreground border-border border-t px-4 py-2.5 text-xs">
        {locale === "de" ? stop.rationale_de : stop.rationale_en}
      </p>
    </Card>
  );
}

function Fact({
  label,
  value,
  sub,
  icon: Icon,
}: {
  label: string;
  value: string;
  sub?: string;
  icon?: typeof Zap;
}) {
  return (
    <div className="px-4 py-2.5">
      <dt className="eyebrow flex items-center gap-1">
        {Icon ? <Icon className="size-3" aria-hidden /> : null}
        {label}
      </dt>
      <dd className="text-foreground mt-0.5 font-mono text-sm font-medium tabular-nums">{value}</dd>
      {sub ? <dd className="text-muted-foreground mt-0.5 text-[0.6875rem]">{sub}</dd> : null}
    </div>
  );
}
