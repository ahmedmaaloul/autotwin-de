"use client";

import { Crosshair } from "lucide-react";
import { useMemo } from "react";

import { DataFreshness } from "@/components/shared/data-freshness";
import { SectionHeader } from "@/components/shared/section-header";
import { SourceBadge } from "@/components/shared/source-badge";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { StatusBadge, type StatusTone } from "@/components/shared/status-badge";
import { Button } from "@/components/ui/button";
import { useRoutes, useVehicleTelemetry } from "@/hooks/use-autotwin";
import { formatCoordinate, formatDateTime, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { VehicleLive, VehicleState } from "@/types/domain";

import { FieldRow } from "./field-row";
import { VehicleSparklines } from "./vehicle-sparklines";

const STATE_TONE: Record<VehicleState, StatusTone> = {
  idle: "neutral",
  driving: "simulation",
  charging: "healthy",
  stopped: "delayed",
  completed: "neutral",
};

/**
 * Everything known about one simulated vehicle.
 *
 * Two figures are computed here rather than read off the wire, and both say so in a tooltip:
 * the average consumption is the mean of the stored telemetry window, and the arrival time is
 * derived from the route's remaining distance at the current speed. The estimate is anchored to
 * the telemetry timestamp, not to the moment of rendering — an ETA that drifts while the page
 * sits idle would be a rendering artefact rather than a measurement.
 */
export function VehiclePanel({
  vehicle,
  onFocus,
}: {
  vehicle: VehicleLive;
  onFocus?: () => void;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const telemetry = useVehicleTelemetry(vehicle.vehicle_id);
  const routes = useRoutes();

  // The API does not promise an order; the sparklines need oldest → newest.
  const points = useMemo(
    () =>
      [...(telemetry.data ?? [])].sort(
        (a, b) => new Date(a.recorded_at).getTime() - new Date(b.recorded_at).getTime(),
      ),
    [telemetry.data],
  );

  const avgConsumption = useMemo(() => {
    if (points.length === 0) return null;
    const total = points.reduce((sum, point) => sum + point.energy_consumption_kwh_100km, 0);
    return total / points.length;
  }, [points]);

  const route = useMemo(
    () => routes.data?.items.find((item) => item.id === vehicle.route_id) ?? null,
    [routes.data, vehicle.route_id],
  );

  const eta = useMemo(() => {
    const odometerM = points.at(-1)?.odometer_m;
    if (!route || odometerM == null || vehicle.speed_kmh < 5) return null;
    const remainingKm = Math.max(0, (route.distance_m - odometerM) / 1000);
    const recordedAt = new Date(vehicle.recorded_at).getTime();
    if (Number.isNaN(recordedAt)) return null;
    return new Date(recordedAt + (remainingKm / vehicle.speed_kmh) * 3_600_000);
  }, [points, route, vehicle.speed_kmh, vehicle.recorded_at]);

  const socTone =
    vehicle.battery_soc_percent < 10
      ? "danger"
      : vehicle.battery_soc_percent < 25
        ? "warning"
        : "success";

  return (
    <div className="flex min-h-0 flex-col">
      <div className="border-border space-y-2 border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.live.panelTitle}
          title={<span className="font-mono text-sm">{vehicle.vehicle_id}</span>}
          actions={
            onFocus ? (
              <Button variant="outline" size="icon-sm" onClick={onFocus} title={t.live.centerMap}>
                <Crosshair aria-hidden />
                <span className="sr-only">{t.live.centerMap}</span>
              </Button>
            ) : undefined
          }
        />
        <div className="flex flex-wrap items-center gap-1.5">
          <StatusBadge
            status={STATE_TONE[vehicle.state]}
            label={t.live.vehicleState[vehicle.state]}
            pulse={vehicle.state === "driving"}
          />
          <SourceBadge origin="simulated" source="simulator" compact />
          <DataFreshness timestamp={vehicle.recorded_at} className="ml-auto" />
        </div>
      </div>

      <div className="border-border border-b px-4 py-3">
        <p className="eyebrow section-tick">{t.live.history}</p>
        <p className="text-muted-foreground mb-2 text-xs">{t.live.historyHint}</p>
        {telemetry.isPending ? (
          <LoadingSkeleton rows={3} />
        ) : telemetry.isError ? (
          <ErrorState error={telemetry.error} onRetry={() => void telemetry.refetch()} />
        ) : points.length === 0 ? (
          <EmptyState title={t.live.noHistory} description={t.live.noHistoryBody} />
        ) : (
          <VehicleSparklines points={points} />
        )}
      </div>

      <div className="px-4 py-2">
        <FieldRow label={t.live.model} value={vehicle.model_code} />
        <FieldRow label={t.live.trip} value={vehicle.trip_id ?? "–"} />
        <FieldRow
          label={t.live.speed}
          value={formatNumber(vehicle.speed_kmh, locale, { decimals: 0 })}
          unit={t.units.kmh}
        />
        <FieldRow
          label={t.live.soc}
          value={formatNumber(vehicle.battery_soc_percent, locale, { decimals: 1 })}
          unit={t.units.percent}
          tone={socTone}
        />
        <FieldRow
          label={t.live.batteryTemp}
          value={formatNumber(vehicle.battery_temperature_c, locale, { decimals: 1 })}
          unit={t.units.celsius}
        />
        <FieldRow
          label={t.live.power}
          value={formatNumber(vehicle.instantaneous_power_kw, locale, { decimals: 1 })}
          unit={t.units.kw}
        />
        <FieldRow
          label={t.live.currentConsumption}
          value={formatNumber(vehicle.energy_consumption_kwh_100km, locale, { decimals: 1 })}
          unit={t.units.kwhPer100km}
        />
        <FieldRow
          label={t.live.avgConsumption}
          value={formatNumber(avgConsumption, locale, { decimals: 1 })}
          unit={t.units.kwhPer100km}
          hint={t.live.avgConsumptionHint}
        />
        <FieldRow
          label={t.live.range}
          value={formatNumber(vehicle.estimated_range_km, locale, { decimals: 0 })}
          unit={t.units.km}
        />
        <FieldRow label={t.live.destination} value={route?.destination_name ?? "–"} />
        <FieldRow
          label={t.live.eta}
          value={eta ? formatDateTime(eta, locale, { timeStyle: "short" }) : "–"}
          hint={t.live.etaHint}
        />
        <FieldRow
          label={t.live.weather}
          value={formatNumber(vehicle.outside_temperature_c, locale, { decimals: 1 })}
          unit={t.units.celsius}
          hint={t.live.outsideTemp}
        />
        <FieldRow
          label={t.live.position}
          value={
            <span className="text-xs">
              {formatCoordinate(vehicle.latitude, vehicle.longitude, locale)}
            </span>
          }
        />
      </div>
    </div>
  );
}
