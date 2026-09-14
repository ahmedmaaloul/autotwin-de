"use client";

import { MapPin, Plug } from "lucide-react";

import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { BUNDESLAENDER } from "@/lib/constants";
import { formatCoordinate, formatDateTime, formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { Messages } from "@/lib/i18n/messages/de";
import type { ChargingStationDetail, ConnectorType } from "@/types/domain";

import { CategoryBadge, connectorLabel, currentLabel, Measure } from "./charging-vocabulary";
import { stationLabel } from "./charging-table-columns";
import { StationProvenance } from "./station-provenance";

/**
 * The detail drawer for one charging site.
 *
 * A `<Sheet>` rather than a page: the reader is mid-comparison on the map or in the table, and
 * sending them to `/charging/{id}` would lose the filter, the viewport and the scroll position
 * they built up. The open station is still in the URL, so the view stays linkable.
 */
export function StationDetail({
  stationId,
  station,
  isLoading,
  error,
  onClose,
  onRetry,
}: {
  stationId: string | null;
  station: ChargingStationDetail | undefined;
  isLoading: boolean;
  error: unknown;
  onClose: () => void;
  onRetry: () => void;
}) {
  const t = useTranslations();
  const locale = useLocale();

  return (
    <Sheet open={stationId !== null} onOpenChange={(open) => (open ? null : onClose())}>
      <SheetContent
        side="right"
        className="w-full gap-0 p-0 sm:max-w-md"
        aria-describedby={undefined}
      >
        <SheetHeader className="border-border border-b px-4 pt-4 pb-3">
          <p className="eyebrow">{t.charging.detailTitle}</p>
          <SheetTitle className="pr-8 text-base leading-tight">
            {station ? stationLabel(station, t) : t.charging.station}
          </SheetTitle>
          {station?.operator ? (
            <SheetDescription className="text-xs">{station.operator}</SheetDescription>
          ) : null}
        </SheetHeader>

        <ScrollArea className="scrollbar-thin min-h-0 flex-1">
          <div className="space-y-4 p-4">
            {error ? <ErrorState error={error} onRetry={onRetry} /> : null}
            {!error && isLoading ? <LoadingSkeleton rows={8} /> : null}
            {!error && !isLoading && !station ? (
              <EmptyState
                icon={MapPin}
                title={t.charging.stationNotFound}
                description={t.charging.stationNotFoundBody}
              />
            ) : null}
            {!error && station ? (
              <StationBody station={station} t={t} locale={locale} />
            ) : null}
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}

function StationBody({
  station,
  t,
  locale,
}: {
  station: ChargingStationDetail;
  t: Messages;
  locale: ReturnType<typeof useLocale>;
}) {
  const address = [
    [station.street, station.house_number].filter(Boolean).join(" "),
    [station.postal_code, station.city].filter(Boolean).join(" "),
  ]
    .filter(Boolean)
    .join(", ");

  const bundesland = station.bundesland
    ? (BUNDESLAENDER.find((entry) => entry.code === station.bundesland)?.name ?? station.bundesland)
    : null;

  const connectors = countConnectors(station);

  return (
    <>
      <div className="border-border divide-border bg-card grid grid-cols-3 divide-x rounded border">
        <Figure label={t.charging.chargingPoints}>
          <Measure
            value={formatNumber(station.charging_points_count, locale)}
            valueClassName="text-lg"
          />
        </Figure>
        <Figure label={t.charging.maxPower}>
          <Measure
            value={formatNumber(station.max_power_kw, locale)}
            unit={t.units.kw}
            valueClassName="text-lg"
          />
        </Figure>
        <Figure label={t.charging.totalPower}>
          <Measure
            value={formatNumber(station.total_power_kw, locale)}
            unit={t.units.kw}
            valueClassName="text-lg"
          />
        </Figure>
      </div>

      <dl className="divide-border border-border divide-y rounded border">
        <Row label={t.charging.category}>
          <CategoryBadge category={station.charging_category} />
        </Row>
        <Row label={t.charging.address}>
          <span className="text-right text-xs">{address || "–"}</span>
        </Row>
        <Row label={t.charging.bundesland}>
          <span className="text-xs">{bundesland ?? "–"}</span>
        </Row>
        <Row label={t.charging.coordinates}>
          <span className="font-mono text-[0.75rem] tabular-nums">
            {formatCoordinate(station.latitude, station.longitude, locale)}
          </span>
        </Row>
        <Row label={t.charging.commissionedOn}>
          <span className="font-mono text-[0.75rem] tabular-nums">
            {station.commissioned_on
              ? formatDateTime(station.commissioned_on, locale, { dateStyle: "long" })
              : "–"}
          </span>
        </Row>
      </dl>

      <section aria-label={t.charging.connectors} className="space-y-2">
        <p className="eyebrow section-tick inline-flex items-center gap-1.5">
          <Plug className="size-3.5" aria-hidden />
          {t.charging.connectors}
        </p>
        {connectors.length === 0 ? (
          <p className="text-muted-foreground text-xs">{t.charging.noConnectors}</p>
        ) : (
          <ul className="flex flex-wrap gap-1.5">
            {connectors.map((connector) => (
              <li
                key={connector.type}
                className="border-border bg-card inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs"
              >
                <span className="font-medium">{connectorLabel(connector.type, t)}</span>
                <span className="text-muted-foreground font-mono text-[0.6875rem] tabular-nums">
                  ×{formatNumber(connector.count, locale)}
                </span>
                {connector.maxPowerKw != null ? (
                  <span className="text-muted-foreground border-border border-l pl-1.5 font-mono text-[0.6875rem] tabular-nums">
                    {formatNumber(connector.maxPowerKw, locale)} {t.units.kw}
                  </span>
                ) : null}
                <span className="text-muted-foreground text-[0.6875rem]">
                  {currentLabel(connector.current, t)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <StationProvenance provenance={station.provenance} />
    </>
  );
}

function Figure({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 px-3 py-2.5">
      <p className="eyebrow mb-1 truncate">{label}</p>
      {children}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2">
      <dt className="text-muted-foreground shrink-0 text-xs">{label}</dt>
      <dd className="min-w-0 text-right">{children}</dd>
    </div>
  );
}

interface ConnectorSummary {
  type: ConnectorType;
  count: number;
  maxPowerKw: number | null;
  current: ChargingStationDetail["charging_points"][number]["current_type"];
}

/** Twelve identical CCS plugs are one fact, not twelve rows. */
function countConnectors(station: ChargingStationDetail): ConnectorSummary[] {
  const grouped = new Map<ConnectorType, ConnectorSummary>();
  for (const point of station.charging_points) {
    const existing = grouped.get(point.connector_type);
    if (existing) {
      existing.count += 1;
      existing.maxPowerKw = Math.max(existing.maxPowerKw ?? 0, point.power_kw ?? 0) || null;
    } else {
      grouped.set(point.connector_type, {
        type: point.connector_type,
        count: 1,
        maxPowerKw: point.power_kw,
        current: point.current_type,
      });
    }
  }
  return [...grouped.values()].sort((a, b) => b.count - a.count);
}
