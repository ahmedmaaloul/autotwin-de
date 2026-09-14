"use client";

import { EmptyState, ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useRoutes } from "@/hooks/use-autotwin";
import { formatDistanceKm } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";

/**
 * Which corridors the simulated vehicles drive on.
 *
 * Demo corridors first, because those are the routes with committed geometry and the ones the
 * rest of the product uses as reference strecken. An empty selection is meaningful rather than
 * invalid — it hands the choice back to the simulator, which spreads the fleet over all of
 * them.
 */
export function RouteDistribution({
  selected,
  onChange,
  disabled,
}: {
  selected: string[];
  onChange: (slugs: string[]) => void;
  disabled?: boolean;
}) {
  const t = useTranslations();
  const locale = useLocale();
  const routes = useRoutes();

  if (routes.isPending) return <LoadingSkeleton rows={3} />;
  if (routes.isError) {
    return <ErrorState error={routes.error} onRetry={() => void routes.refetch()} />;
  }

  const items = (routes.data?.items ?? []).filter((route) => route.slug);
  if (items.length === 0) {
    return <EmptyState title={t.simulation.routesEmpty} description={t.states.emptyBody} />;
  }

  const toggle = (slug: string, checked: boolean) => {
    onChange(checked ? [...selected, slug] : selected.filter((value) => value !== slug));
  };

  return (
    <ScrollArea className="border-border h-40 rounded border">
      <ul className="divide-border divide-y">
        {items.map((route) => {
          const slug = route.slug as string;
          const id = `route-${slug}`;
          return (
            <li key={route.id} className="flex items-center gap-2 px-2.5 py-1.5">
              <Checkbox
                id={id}
                checked={selected.includes(slug)}
                disabled={disabled}
                onCheckedChange={(checked) => toggle(slug, checked === true)}
              />
              <Label htmlFor={id} className="min-w-0 flex-1 text-xs font-normal">
                <span className="truncate">{route.name}</span>
              </Label>
              <span className="flex shrink-0 items-baseline gap-1">
                <span className="text-muted-foreground font-mono text-xs tabular-nums">
                  {formatDistanceKm(route.distance_m, locale, { fromMetres: true })}
                </span>
                <span className="text-muted-foreground text-[0.6875rem]">{t.units.km}</span>
              </span>
            </li>
          );
        })}
      </ul>
    </ScrollArea>
  );
}
