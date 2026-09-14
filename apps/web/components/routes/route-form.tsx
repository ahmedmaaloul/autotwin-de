"use client";

import { Loader2, Play, SlidersHorizontal } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import type { VehicleProfile } from "@/types/domain";

import type { CorridorOption } from "./corridor-chips";
import { RouteFormFields } from "./route-form-fields";
import { isAnalysable, type RouteFormState } from "./route-request";

export interface RouteFormProps {
  value: RouteFormState;
  vehicleCode: string;
  onChange: (patch: Partial<RouteFormState>) => void;
  onSubmit: () => void;
  pending: boolean;
  corridors: CorridorOption[];
  profiles: VehicleProfile[];
  profilesLoading: boolean;
  profilesFailed: boolean;
}

/**
 * The control bar of the page.
 *
 * On a desktop it is what it looks like — a wide instrument panel above the results, where all
 * six inputs are visible at once and changing one is a single click. Below `lg` the same fields
 * move into a `<Sheet>` behind a summary line, because on a phone a six-field form between the
 * heading and the answer means the answer is never on screen.
 */
export function RouteForm({
  value,
  vehicleCode,
  onChange,
  onSubmit,
  pending,
  corridors,
  profiles,
  profilesLoading,
  profilesFailed,
}: RouteFormProps) {
  const t = useTranslations();
  const [sheetOpen, setSheetOpen] = useState(false);

  const canSubmit = isAnalysable(value) && !pending && !profilesFailed;

  const fieldProps = {
    value,
    vehicleCode,
    onChange,
    corridors,
    profiles,
    profilesLoading,
    profilesFailed,
    disabled: pending,
  };

  return (
    <section aria-label={t.routes.parameters}>
      {/* Desktop: everything visible at once. */}
      <Card className="hidden rounded p-4 shadow-none lg:block">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (canSubmit) onSubmit();
          }}
          className="space-y-4"
        >
          <RouteFormFields {...fieldProps} idPrefix="route-form" />
          <div className="border-border flex items-center justify-end border-t pt-4">
            <SubmitButton pending={pending} disabled={!canSubmit} />
          </div>
        </form>
      </Card>

      {/* Below lg: a summary line plus the same fields inside a sheet. */}
      <Card className="rounded p-3 shadow-none lg:hidden">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <FormSummary value={value} profiles={profiles} />
          <div className="flex items-center gap-2">
            <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
              <SheetTrigger asChild>
                <Button type="button" variant="outline" size="sm">
                  <SlidersHorizontal />
                  {t.routes.changeParameters}
                </Button>
              </SheetTrigger>
              <SheetContent side="bottom" className="max-h-[90svh] overflow-y-auto">
                <SheetHeader>
                  <SheetTitle>{t.routes.parameters}</SheetTitle>
                  <SheetDescription>{t.routes.subtitle}</SheetDescription>
                </SheetHeader>
                <div className="px-4 pb-6">
                  <RouteFormFields {...fieldProps} idPrefix="route-sheet" />
                  <div className="mt-4">
                    <SubmitButton
                      pending={pending}
                      disabled={!canSubmit}
                      className="w-full"
                      onClick={() => {
                        setSheetOpen(false);
                        onSubmit();
                      }}
                    />
                  </div>
                </div>
              </SheetContent>
            </Sheet>
            <SubmitButton pending={pending} disabled={!canSubmit} onClick={onSubmit} />
          </div>
        </div>
      </Card>
    </section>
  );
}

function SubmitButton({
  pending,
  disabled,
  onClick,
  className,
}: {
  pending: boolean;
  disabled: boolean;
  onClick?: () => void;
  className?: string;
}) {
  const t = useTranslations();
  return (
    <Button
      type={onClick ? "button" : "submit"}
      size="lg"
      disabled={disabled}
      onClick={onClick}
      className={className}
    >
      {pending ? (
        <Loader2 className="animate-spin motion-reduce:animate-none" aria-hidden />
      ) : (
        <Play aria-hidden />
      )}
      {pending ? t.routes.analyzing : t.routes.analyze}
    </Button>
  );
}

/** What the sheet is hiding, so the collapsed state still states the request. */
function FormSummary({
  value,
  profiles,
}: {
  value: RouteFormState;
  profiles: VehicleProfile[];
}) {
  const t = useTranslations();
  const locale = useLocale();
  const vehicle = profiles.find((profile) => profile.code === value.vehicleCode);

  return (
    <div className="min-w-0 text-xs">
      <p className="text-foreground truncate text-sm font-medium">
        {value.origin} <span aria-hidden>→</span> {value.destination}
      </p>
      <p className="text-muted-foreground truncate">
        {vehicle ? `${vehicle.display_name} · ` : ""}
        {t.routes.startSoc}{" "}
        <span className="font-mono tabular-nums">{formatNumber(value.startSoc, locale)}</span>{" "}
        {t.units.percent}
      </p>
    </div>
  );
}
