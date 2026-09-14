"use client";

import Link from "next/link";
import { useState } from "react";

import { MapCanvas } from "@/components/maps/map-canvas";
import { ChargingLayer } from "@/components/maps/layers/charging-layer";
import { VehicleLayer } from "@/components/maps/layers/vehicle-layer";
import { PageHeader } from "@/components/shared/section-header";
import { EmptyState, ErrorState } from "@/components/shared/states";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { useChargingGeoJson } from "@/hooks/use-autotwin";
import { useTelemetryStream } from "@/hooks/use-telemetry-stream";
import { ApiError } from "@/lib/api/client";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { ConnectionIndicator } from "./connection-indicator";
import { LiveMapLegend } from "./live-map-legend";
import { MapFocus, type FocusRequest } from "./map-focus";
import { TelemetryBanner } from "./telemetry-banner";
import { useIsLargeScreen } from "./use-large-screen";
import { VehicleList } from "./vehicle-list";
import { VehiclePanel } from "./vehicle-panel";

/** Reused for the map overlay so a dead stream reads as "backend offline", not as "no traffic". */
const STREAM_ERROR = new ApiError(
  0,
  "stream_unavailable",
  "The telemetry stream is not available.",
);

/**
 * The live fleet.
 *
 * The page holds three pieces of state and nothing else: which vehicle is selected, whether the
 * charging layer is on, and the pending map-centring request. Telemetry itself never becomes
 * page state — it arrives through the SSE hook, which coalesces a burst of events into one
 * render per animation frame, and is handed straight to `<VehicleLayer>`, which interpolates
 * positions internally. That is what keeps several hundred vehicles moving smoothly: nothing
 * between the socket and the GeoJSON source re-creates a component tree per tick.
 *
 * The charging layer is fetched lazily — it is the 90 000-row payload, and nobody should pay
 * for it until they ask for the background.
 */
export function LivePage() {
  const t = useTranslations();
  const locale = useLocale();

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [chargingVisible, setChargingVisible] = useState(false);
  const [focusRequest, setFocusRequest] = useState<FocusRequest | null>(null);

  const { vehicles, vehiclesById, status, eventsReceived } = useTelemetryStream();
  const charging = useChargingGeoJson({ fast_only: true }, chargingVisible);
  const isLarge = useIsLargeScreen();

  const selected = selectedId ? (vehiclesById.get(selectedId) ?? null) : null;

  const focusOnSelected = () => {
    if (!selected) return;
    setFocusRequest((previous) => ({
      token: (previous?.token ?? 0) + 1,
      center: [selected.longitude, selected.latitude],
      zoom: 11,
    }));
  };

  const empty = vehicles.length === 0;
  const disconnected = status === "closed" || status === "error";

  const panel = selected ? (
    <VehiclePanel vehicle={selected} onFocus={isLarge ? focusOnSelected : undefined} />
  ) : (
    <div className="flex h-full items-center justify-center p-4">
      <EmptyState
        title={t.live.noVehicleSelected}
        description={t.live.noVehicleSelectedBody}
      />
    </div>
  );

  return (
    <div className="flex min-h-[40rem] flex-col gap-3 lg:h-[calc(100dvh-7rem)]">
      <TelemetryBanner />

      <PageHeader title={t.live.title} description={t.live.subtitle} />

      {/*
        The stream instruments live in their own row rather than in the header's action slot:
        that slot never shrinks, and four measurements plus a switch cannot fit on a 360 px
        phone without wrapping. Here they wrap.
      */}
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <span className="flex items-baseline gap-1">
            <span className="text-foreground font-mono text-sm tabular-nums">
              {formatNumber(vehicles.length, locale)}
            </span>
            <span className="text-muted-foreground text-xs">{t.units.vehicles}</span>
          </span>
          <ConnectionIndicator status={status} eventsReceived={eventsReceived} />
        </div>
        <span className="flex items-center gap-2">
          <Switch
            id="live-charging-layer"
            checked={chargingVisible}
            onCheckedChange={setChargingVisible}
          />
          <Label htmlFor="live-charging-layer" className="text-xs">
            {t.live.chargingLayer}
          </Label>
        </span>
      </div>

      <section className="flex min-h-0 flex-1 gap-3">
        <div className="border-border relative min-h-[22rem] min-w-0 flex-1 overflow-hidden rounded border">
          <MapCanvas ariaLabel={t.live.mapLabel}>
            <ChargingLayer data={charging.data} visible={chargingVisible} />
            <VehicleLayer
              vehicles={vehicles}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
            <MapFocus request={focusRequest} />
          </MapCanvas>

          <LiveMapLegend
            showCharging={chargingVisible}
            className="absolute top-3 left-3 z-10 w-44"
          />

          {empty ? (
            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center p-4">
              <div className="bg-card/95 pointer-events-auto max-w-sm rounded backdrop-blur">
                {status === "connecting" ? (
                  <EmptyState title={t.live.connecting} description={t.live.stream} />
                ) : disconnected ? (
                  <ErrorState error={STREAM_ERROR} />
                ) : (
                  <EmptyState
                    title={t.live.noVehicles}
                    description={t.live.noVehiclesBody}
                    action={
                      <Button asChild size="sm" className="mt-1">
                        <Link href="/simulation">{t.live.startSimulation}</Link>
                      </Button>
                    }
                  />
                )}
              </div>
            </div>
          ) : null}
        </div>

        <aside className="hidden w-[21rem] shrink-0 flex-col gap-3 lg:flex">
          {empty ? null : (
            <div className="border-border bg-card flex h-[38%] min-h-0 shrink-0 flex-col overflow-hidden rounded border">
              <VehicleList
                vehicles={vehicles}
                selectedId={selectedId}
                onSelect={setSelectedId}
                className="flex-1"
              />
            </div>
          )}
          <div className="border-border bg-card scrollbar-thin min-h-0 flex-1 overflow-y-auto rounded border">
            {panel}
          </div>
        </aside>
      </section>

      {/*
        Below `lg` the column is gone, and hunting for a 4 px dot on a phone map is not a
        selection mechanism. The same shortlist comes back under the map, and picking a row
        opens the sheet.
      */}
      {empty ? null : (
        <div className="border-border bg-card flex h-64 flex-col overflow-hidden rounded border lg:hidden">
          <VehicleList
            vehicles={vehicles}
            selectedId={selectedId}
            onSelect={setSelectedId}
            className="flex-1"
          />
        </div>
      )}

      <Sheet
        open={Boolean(selected) && !isLarge}
        onOpenChange={(open) => {
          if (!open) setSelectedId(null);
        }}
      >
        <SheetContent side="right" className="w-full gap-0 overflow-y-auto p-0 sm:max-w-sm">
          <SheetHeader className="sr-only">
            <SheetTitle>{t.live.panelTitle}</SheetTitle>
            <SheetDescription>{t.live.subtitle}</SheetDescription>
          </SheetHeader>
          {selected ? <VehiclePanel vehicle={selected} /> : null}
        </SheetContent>
      </Sheet>
    </div>
  );
}
