"use client";

import { Dices } from "lucide-react";

import { SectionHeader } from "@/components/shared/section-header";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";

import {
  randomSeed,
  SPEED_FACTOR_RANGE,
  TRAFFIC_LEVELS,
  VEHICLE_COUNT_RANGE,
  WEATHER_MODES,
  type SimulationConfig,
  type TrafficLevel,
  type WeatherMode,
} from "./config";
import { ControlField } from "./control-field";
import { RouteDistribution } from "./route-distribution";
import { VehicleMix } from "./vehicle-mix";

/**
 * Everything that defines the next run.
 *
 * The seed sits at the bottom with its promise spelled out: the simulator is deterministic
 * physics plus bounded noise, so the same seed and the same parameters reproduce the same
 * telemetry (ADR 004). That is what makes a demo defensible — a reviewer can re-run it.
 *
 * Controls are disabled while a run is active rather than hidden: the parameters of the run you
 * are watching are worth reading.
 */
export function SimulationControls({
  config,
  onChange,
  disabled = false,
  className,
}: {
  config: SimulationConfig;
  onChange: (config: SimulationConfig) => void;
  disabled?: boolean;
  className?: string;
}) {
  const t = useTranslations();
  const locale = useLocale();

  const patch = (partial: Partial<SimulationConfig>) => onChange({ ...config, ...partial });

  return (
    <Card className={cn("gap-0 rounded border p-0 shadow-none", className)}>
      <div className="border-border border-b px-4 py-3">
        <SectionHeader
          eyebrow={t.simulation.controlsTitle}
          title={t.simulation.controlsSubtitle}
        />
      </div>

      <div className="space-y-4 px-4 py-4">
        <ControlField
          label={t.simulation.vehicleCount}
          value={formatNumber(config.vehicleCount, locale)}
          unit={t.units.vehicles}
        >
          <Slider
            aria-label={t.simulation.vehicleCount}
            min={VEHICLE_COUNT_RANGE.min}
            max={VEHICLE_COUNT_RANGE.max}
            step={VEHICLE_COUNT_RANGE.step}
            disabled={disabled}
            value={[config.vehicleCount]}
            onValueChange={(values) => patch({ vehicleCount: values[0] })}
          />
        </ControlField>

        <ControlField
          label={t.simulation.speedFactor}
          value={`${formatNumber(config.speedFactor, locale)}×`}
        >
          <Slider
            aria-label={t.simulation.speedFactor}
            min={SPEED_FACTOR_RANGE.min}
            max={SPEED_FACTOR_RANGE.max}
            step={SPEED_FACTOR_RANGE.step}
            disabled={disabled}
            value={[config.speedFactor]}
            onValueChange={(values) => patch({ speedFactor: values[0] })}
          />
        </ControlField>

        <ControlField
          label={t.simulation.routeDistribution}
          value={formatNumber(config.routeSlugs.length, locale)}
          hint={t.simulation.routeDistributionHint}
        >
          <RouteDistribution
            selected={config.routeSlugs}
            onChange={(routeSlugs) => patch({ routeSlugs })}
            disabled={disabled}
          />
        </ControlField>

        <div className="grid gap-4 sm:grid-cols-2">
          <ControlField label={t.simulation.weatherMode}>
            <Select
              value={config.weatherMode}
              disabled={disabled}
              onValueChange={(value) => patch({ weatherMode: value as WeatherMode })}
            >
              <SelectTrigger aria-label={t.simulation.weatherMode} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {WEATHER_MODES.map((mode) => (
                  <SelectItem key={mode} value={mode}>
                    {t.simulation.weatherModes[mode]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </ControlField>

          <ControlField label={t.simulation.trafficIntensity}>
            <Select
              value={config.trafficIntensity}
              disabled={disabled}
              onValueChange={(value) => patch({ trafficIntensity: value as TrafficLevel })}
            >
              <SelectTrigger aria-label={t.simulation.trafficIntensity} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TRAFFIC_LEVELS.map((level) => (
                  <SelectItem key={level} value={level}>
                    {t.simulation.trafficLevels[level]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </ControlField>
        </div>

        <ControlField label={t.simulation.vehicleMix}>
          <VehicleMix
            config={config}
            onChange={(vehicleMix) => patch({ vehicleMix })}
            disabled={disabled}
          />
        </ControlField>

        <div className="space-y-1.5">
          <Label htmlFor="simulation-seed" className="eyebrow">
            {t.simulation.seed}
          </Label>
          <div className="flex items-center gap-2">
            <Input
              id="simulation-seed"
              type="number"
              inputMode="numeric"
              min={0}
              step={1}
              disabled={disabled}
              className="font-mono tabular-nums"
              value={config.seed}
              onChange={(event) => {
                const parsed = Number.parseInt(event.target.value, 10);
                patch({ seed: Number.isFinite(parsed) ? Math.max(0, parsed) : 0 });
              }}
            />
            <Button
              type="button"
              variant="outline"
              size="icon"
              disabled={disabled}
              title={t.simulation.seedRandomize}
              onClick={() => patch({ seed: randomSeed() })}
            >
              <Dices aria-hidden />
              <span className="sr-only">{t.simulation.seedRandomize}</span>
            </Button>
          </div>
          <p className="text-muted-foreground text-xs">{t.simulation.reproducible}</p>
        </div>
      </div>
    </Card>
  );
}
