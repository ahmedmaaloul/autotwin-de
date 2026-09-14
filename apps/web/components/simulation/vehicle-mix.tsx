"use client";

import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Slider } from "@/components/ui/slider";
import { useVehicleProfiles } from "@/hooks/use-autotwin";
import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { mixShares, mixWeight, type SimulationConfig } from "./config";

/**
 * How the fleet is composed, by vehicle class.
 *
 * The sliders carry weights, not percentages, so moving one does not force the others to move
 * with it — the share read-out beside each label shows what the weights normalise to, which is
 * what the API is actually sent. A van and a compact differ by roughly 50 % in nominal
 * consumption, so this control changes the fleet's energy profile materially.
 */
export function VehicleMix({
  config,
  onChange,
  disabled,
}: {
  config: SimulationConfig;
  onChange: (mix: Record<string, number>) => void;
  disabled?: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const profiles = useVehicleProfiles();

  if (profiles.isPending) return <LoadingSkeleton rows={3} />;
  if (profiles.isError) {
    return <ErrorState error={profiles.error} onRetry={() => void profiles.refetch()} />;
  }

  const items = profiles.data ?? [];
  if (items.length === 0) {
    return <EmptyState title={t.simulation.vehicleMixEmpty} description={t.states.emptyBody} />;
  }

  const codes = items.map((profile) => profile.code);
  const shares = mixShares(config, codes);

  const setWeight = (code: string, weight: number) => {
    // Write every class explicitly on the first edit, so what is shown and what is sent agree
    // even for the classes that were left at their default weight.
    const next: Record<string, number> = {};
    for (const item of codes) next[item] = mixWeight(config, item);
    next[code] = weight;
    onChange(next);
  };

  return (
    <div className="space-y-2.5">
      {items.map((profile) => (
        <div key={profile.code}>
          <div className="flex items-baseline justify-between gap-2">
            <span className="text-foreground truncate text-xs">{profile.display_name}</span>
            <span className="flex shrink-0 items-baseline gap-1">
              <span className="text-foreground font-mono text-xs tabular-nums">
                {formatNumber(shares[profile.code] ?? 0, locale, { decimals: 0 })}
              </span>
              <span className="text-muted-foreground text-[0.6875rem]">{t.units.percent}</span>
            </span>
          </div>
          <Slider
            aria-label={profile.display_name}
            className="mt-1.5"
            min={0}
            max={100}
            step={5}
            disabled={disabled}
            value={[mixWeight(config, profile.code)]}
            onValueChange={(values) => setWeight(profile.code, values[0])}
          />
        </div>
      ))}
      <p className="text-muted-foreground text-xs">{t.simulation.vehicleMixHint}</p>
    </div>
  );
}
