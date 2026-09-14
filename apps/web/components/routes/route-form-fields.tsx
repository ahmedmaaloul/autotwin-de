"use client";

import { ArrowLeftRight, CircleDot, Flag } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { VehicleProfile } from "@/types/domain";

import { CorridorChips, type CorridorOption } from "./corridor-chips";
import { vehicleClassLabel } from "./labels";
import { SocField } from "./soc-field";
import type { RouteFormState } from "./route-request";

export interface RouteFormFieldsProps {
  value: RouteFormState;
  vehicleCode: string;
  onChange: (patch: Partial<RouteFormState>) => void;
  corridors: CorridorOption[];
  profiles: VehicleProfile[];
  profilesLoading: boolean;
  profilesFailed: boolean;
  /** Distinguishes the inline desktop form from its copy inside the mobile sheet. */
  idPrefix: string;
  disabled?: boolean;
}

/**
 * The inputs of the analysis, without any layout chrome.
 *
 * Rendered twice — inline on a desktop control bar, and inside a `<Sheet>` below `lg` — over
 * one piece of state, so the two are never out of step. `idPrefix` keeps the `label`/`input`
 * associations unique across both copies.
 */
export function RouteFormFields({
  value,
  vehicleCode,
  onChange,
  corridors,
  profiles,
  profilesLoading,
  profilesFailed,
  idPrefix,
  disabled = false,
}: RouteFormFieldsProps) {
  const t = useTranslations();

  const selected = profiles.find((profile) => profile.code === vehicleCode) ?? null;

  return (
    <div className="space-y-4">
      <CorridorChips
        corridors={corridors}
        activeSlug={value.routeSlug}
        disabled={disabled}
        onSelect={(corridor) =>
          onChange({
            routeSlug: corridor.slug,
            origin: corridor.origin,
            destination: corridor.destination,
          })
        }
      />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_minmax(0,1.1fr)] lg:items-end">
        <PlaceField
          id={`${idPrefix}-origin`}
          label={t.routes.origin}
          placeholder={t.routes.originPlaceholder}
          icon={CircleDot}
          value={value.origin}
          disabled={disabled}
          // Typing a place means the seeded corridor no longer describes the request.
          onChange={(origin) => onChange({ origin, routeSlug: null })}
        />

        <Button
          type="button"
          variant="outline"
          size="icon"
          disabled={disabled}
          aria-label={t.routes.swapDirection}
          title={t.routes.swapDirection}
          className="hidden lg:inline-flex"
          onClick={() =>
            onChange({ origin: value.destination, destination: value.origin, routeSlug: null })
          }
        >
          <ArrowLeftRight />
        </Button>

        <PlaceField
          id={`${idPrefix}-destination`}
          label={t.routes.destination}
          placeholder={t.routes.destinationPlaceholder}
          icon={Flag}
          value={value.destination}
          disabled={disabled}
          onChange={(destination) => onChange({ destination, routeSlug: null })}
        />

        <div className="min-w-0">
          {/* While the profiles are loading there is no control to point at, and a `for` that
              resolves to nothing is worse than none: a screen reader announces a label with no
              field. The association comes back with the select. */}
          <Label
            htmlFor={profilesLoading ? undefined : `${idPrefix}-vehicle`}
            className="mb-1.5 text-[0.8125rem] font-medium"
          >
            {t.routes.vehicle}
          </Label>
          {profilesLoading ? (
            <Skeleton className="h-8 w-full" />
          ) : (
            <Select
              value={selected ? vehicleCode : undefined}
              disabled={disabled || profilesFailed || profiles.length === 0}
              onValueChange={(next) => onChange({ vehicleCode: next })}
            >
              <SelectTrigger id={`${idPrefix}-vehicle`} className="w-full">
                <SelectValue placeholder={t.routes.vehicle} />
              </SelectTrigger>
              <SelectContent>
                {profiles.map((profile) => (
                  <SelectItem key={profile.code} value={profile.code}>
                    {profile.display_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <p className="text-muted-foreground mt-1.5 min-h-[1rem] text-[0.6875rem]">
            {profilesFailed ? (
              <span className="text-warning">{t.routes.vehicleUnavailable}</span>
            ) : selected ? (
              <VehicleSpecs profile={selected} />
            ) : null}
          </p>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <SocField
          id={`${idPrefix}-start-soc`}
          label={t.routes.startSoc}
          value={value.startSoc}
          min={1}
          disabled={disabled}
          onChange={(startSoc) => onChange({ startSoc })}
        />
        <SocField
          id={`${idPrefix}-min-soc`}
          label={t.routes.minArrivalSoc}
          value={value.minArrivalSoc}
          max={90}
          disabled={disabled}
          onChange={(minArrivalSoc) => onChange({ minArrivalSoc })}
        />
      </div>
    </div>
  );
}

/** Battery and nominal consumption, so the chosen profile is more than a name. */
function VehicleSpecs({ profile }: { profile: VehicleProfile }) {
  const t = useTranslations();
  const locale = useLocale();
  const klass = vehicleClassLabel(profile.vehicle_class, t);

  return (
    <>
      {klass ? <span>{klass} · </span> : null}
      <span className="font-mono tabular-nums">
        {formatNumber(profile.battery_capacity_kwh, locale)}
      </span>{" "}
      {t.units.kwh} ·{" "}
      <span className="font-mono tabular-nums">
        {formatNumber(profile.nominal_consumption_kwh_100km, locale, { decimals: 1 })}
      </span>{" "}
      {t.units.kwhPer100km}
    </>
  );
}

function PlaceField({
  id,
  label,
  placeholder,
  value,
  onChange,
  disabled,
  icon: Icon,
}: {
  id: string;
  label: string;
  placeholder: string;
  value: string;
  onChange: (next: string) => void;
  disabled: boolean;
  icon: typeof CircleDot;
}) {
  return (
    <div className="min-w-0">
      <Label htmlFor={id} className="mb-1.5 text-[0.8125rem] font-medium">
        {label}
      </Label>
      <div className="relative">
        <Icon
          className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2"
          aria-hidden
        />
        <Input
          id={id}
          value={value}
          placeholder={placeholder}
          disabled={disabled}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => onChange(event.target.value)}
          className="pl-8"
        />
      </div>
    </div>
  );
}
