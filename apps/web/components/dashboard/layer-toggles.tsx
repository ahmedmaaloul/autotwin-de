"use client";

import type { ReactNode } from "react";

import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useTranslations } from "@/providers/locale-provider";

export interface LayerState {
  charging: boolean;
  traffic: boolean;
  vehicles: boolean;
}

function LayerSwitch({
  id,
  checked,
  onCheckedChange,
  label,
}: {
  id: string;
  checked: boolean;
  onCheckedChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <Switch id={id} checked={checked} onCheckedChange={onCheckedChange} />
      <Label htmlFor={id} className="cursor-pointer text-xs font-normal whitespace-nowrap">
        {label}
      </Label>
    </div>
  );
}

/**
 * The map's control strip.
 *
 * Switches rather than a legend-that-is-also-a-control: a planner turning the 90 000 charging
 * points off to read the traffic picture underneath is doing something to the map, and a
 * control that looks like a control says so.
 */
export function LayerToggles({
  layers,
  onLayersChange,
  fastOnly,
  onFastOnlyChange,
  status,
}: {
  layers: LayerState;
  onLayersChange: (next: LayerState) => void;
  fastOnly: boolean;
  onFastOnlyChange: (next: boolean) => void;
  status: ReactNode;
}) {
  const t = useTranslations();

  return (
    <div className="border-border bg-card flex flex-wrap items-center gap-x-4 gap-y-2 rounded border px-3 py-2">
      <LayerSwitch
        id="layer-charging"
        checked={layers.charging}
        onCheckedChange={(next) => onLayersChange({ ...layers, charging: next })}
        label={t.overview.layerCharging}
      />
      <LayerSwitch
        id="layer-charging-fast"
        checked={fastOnly}
        onCheckedChange={onFastOnlyChange}
        label={t.charging.fastOnly}
      />
      <span className="bg-border hidden h-4 w-px sm:block" aria-hidden />
      <LayerSwitch
        id="layer-traffic"
        checked={layers.traffic}
        onCheckedChange={(next) => onLayersChange({ ...layers, traffic: next })}
        label={t.overview.layerTraffic}
      />
      <LayerSwitch
        id="layer-vehicles"
        checked={layers.vehicles}
        onCheckedChange={(next) => onLayersChange({ ...layers, vehicles: next })}
        label={t.overview.layerVehicles}
      />
      <div className="ml-auto flex min-w-0 items-center gap-3">{status}</div>
    </div>
  );
}
