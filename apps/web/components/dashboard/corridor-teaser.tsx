"use client";

import { ArrowRight, Route as RouteIcon } from "lucide-react";
import Link from "next/link";

import { SectionHeader } from "@/components/shared/section-header";
import { Skeleton } from "@/components/ui/skeleton";
import { DEMO_ROUTE_SLUG } from "@/lib/constants";
import { formatDistanceKm, formatDuration } from "@/lib/i18n/format";
import { useRoutes } from "@/hooks/use-autotwin";
import { useLocale, useTranslations } from "@/providers/locale-provider";

import { Figure, Panel, PanelBody, PanelHeader } from "./panel";

/**
 * A link, not an analysis.
 *
 * `POST /routes/analyze` is the flagship call and it is expensive — routing, weather and
 * traffic lookups plus an ML inference per segment. Firing it on every dashboard load to draw
 * a decorative strip would make the first screen the slowest one in the product. The corridor
 * gets a teaser with the two facts the cheap `/routes` list already carries, and the analysis
 * happens on /routes when the reader asks for it.
 */
export function CorridorTeaser() {
  const t = useTranslations();
  const locale = useLocale();
  const routes = useRoutes();

  const corridor = routes.data?.items.find((route) => route.slug === DEMO_ROUTE_SLUG) ?? null;
  const title = corridor
    ? `${corridor.origin_name} → ${corridor.destination_name}`
    : t.overview.corridorTitle;

  return (
    <Panel>
      <PanelHeader>
        <SectionHeader
          eyebrow={t.overview.demoCorridor}
          title={title}
          description={t.overview.corridorTeaser}
          actions={<RouteIcon className="text-muted-foreground size-4" aria-hidden />}
        />
      </PanelHeader>
      <PanelBody className="flex flex-wrap items-center justify-between gap-4">
        {routes.isLoading ? (
          <Skeleton className="h-8 w-56" />
        ) : (
          <dl className="flex flex-wrap items-baseline gap-x-8 gap-y-2">
            <div>
              <dt className="eyebrow">{t.routes.distance}</dt>
              <dd className="mt-0.5">
                <Figure
                  className="text-foreground text-lg"
                  value={
                    corridor ? formatDistanceKm(corridor.distance_m, locale, { fromMetres: true }) : "–"
                  }
                  unit={t.units.km}
                />
              </dd>
            </div>
            <div>
              <dt className="eyebrow">{t.routes.duration}</dt>
              <dd className="mt-0.5">
                <Figure
                  className="text-foreground text-lg"
                  value={corridor ? formatDuration(corridor.duration_s, locale) : "–"}
                />
              </dd>
            </div>
          </dl>
        )}

        <Link
          href={`/routes?route=${DEMO_ROUTE_SLUG}`}
          className="border-border bg-background hover:bg-muted focus-visible:ring-ring inline-flex h-8 shrink-0 items-center gap-1.5 rounded border px-3 text-xs font-medium focus-visible:ring-2 focus-visible:outline-none"
        >
          {t.overview.corridorCta}
          <ArrowRight className="size-3.5" aria-hidden />
        </Link>
      </PanelBody>
    </Panel>
  );
}
