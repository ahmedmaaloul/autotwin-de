"use client";

import { Route as RouteIcon } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { PageHeader } from "@/components/shared/section-header";
import { EmptyState, ErrorState } from "@/components/shared/states";
import { useChargingOptimisation, useRouteAnalysis, useRoutes, useVehicleProfiles } from "@/hooks/use-autotwin";
import { useTranslations } from "@/providers/locale-provider";

import { AnalysisSkeleton } from "./analysis-skeleton";
import type { CorridorOption } from "./corridor-chips";
import { CORRIDOR_PRESETS } from "./demo-corridors";
import { RouteAnalysisResult } from "./route-analysis-result";
import { RouteForm } from "./route-form";
import {
  fromSearchParams,
  isAnalysable,
  toAnalyzeRequest,
  toSearchParams,
  type RouteFormState,
} from "./route-request";

/**
 * The flagship page: describe a journey, get an auditable answer.
 *
 * Three decisions shape this component.
 *
 * **The selection lives here.** The Streckenband, the map and the segment table are three views
 * of one `selectedOrdinal`, so clicking an expensive bar highlights the stretch of Autobahn and
 * the table row that explains it. Holding that state in any one of them would make the other
 * two its dependants.
 *
 * **The request lives in the URL.** Submitting writes `?route=…&vehicle=…&soc=…&min=…`, and a
 * page opened with those parameters analyses immediately — an analysis is a claim about a
 * specific journey and has to be quotable.
 *
 * **The charging plan is chained, not polled.** `POST /routes/optimize-charging` runs only when
 * the analysis says a stop is needed, fired from the analysis's own `onSuccess` with the same
 * request object, so the two answers always describe the same journey.
 */
export function RoutesPage() {
  const t = useTranslations();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const [initial] = useState(() => fromSearchParams(searchParams));
  const [form, setForm] = useState<RouteFormState>(initial.state);
  const [selectedOrdinal, setSelectedOrdinal] = useState<number | null>(null);
  const [selectedStationId, setSelectedStationId] = useState<string | null>(null);

  const routesQuery = useRoutes();
  const profilesQuery = useVehicleProfiles();
  const analysis = useRouteAnalysis();
  const charging = useChargingOptimisation();

  const { mutate: analyze, reset: resetAnalysis } = analysis;
  const { mutate: optimise, reset: resetCharging } = charging;

  const profiles = useMemo(() => profilesQuery.data ?? [], [profilesQuery.data]);

  // If the requested code is not among the profiles the API actually offers, fall back to the
  // first one rather than sending a code the backend will reject. Derived during render so no
  // effect ever writes back into the form.
  const vehicleCode = useMemo(() => {
    if (profiles.length === 0) return form.vehicleCode;
    return profiles.some((profile) => profile.code === form.vehicleCode)
      ? form.vehicleCode
      : profiles[0].code;
  }, [profiles, form.vehicleCode]);

  const corridors = useMemo<CorridorOption[]>(() => {
    const items = routesQuery.data?.items ?? [];
    if (items.length > 0) {
      return items.flatMap((route) =>
        route.slug
          ? [
              {
                slug: route.slug,
                origin: route.origin_name,
                destination: route.destination_name,
                distanceM: route.distance_m,
              },
            ]
          : [],
      );
    }
    // The API is unreachable: offer the seeded corridors as plain form presets so the page is
    // still usable. They carry no figures — see demo-corridors.ts.
    return CORRIDOR_PRESETS.map((preset) => ({ ...preset }));
  }, [routesQuery.data]);

  const runAnalysis = useCallback(
    (state: RouteFormState, code: string) => {
      if (!isAnalysable(state)) return;
      const request = toAnalyzeRequest(state, code);
      resetCharging();

      analyze(request, {
        onSuccess: (result) => {
          // The selection pointed at segments of the previous analysis; it is cleared when the
          // new one lands rather than when the request starts, so nothing flickers in between.
          setSelectedOrdinal(null);
          setSelectedStationId(null);
          // Only ask the optimiser when the analysis says the battery will not make it.
          if (result.data.charging_required) optimise(request);
        },
      });
    },
    [analyze, optimise, resetCharging],
  );

  const handleSubmit = useCallback(() => {
    // `history.replaceState` rather than `router.replace`: the search params are a bookmark of
    // client state, not a different route. Going through the router would re-request this
    // segment from the server and re-suspend the page's boundary, blanking the result that was
    // just computed. Next.js supports the native History API for exactly this case.
    window.history.replaceState(null, "", `${pathname}?${toSearchParams(form).toString()}`);
    runAnalysis(form, vehicleCode);
  }, [form, vehicleCode, pathname, runAnalysis]);

  // A link that arrives with parameters is a shared analysis: run it as soon as the vehicle
  // list has settled, so the recipient sees the result rather than a pre-filled form.
  const autoRan = useRef(false);
  useEffect(() => {
    if (autoRan.current) return;
    if (!initial.hasRequest) {
      autoRan.current = true;
      return;
    }
    if (profilesQuery.isPending) return;
    autoRan.current = true;
    runAnalysis(initial.state, vehicleCode);
  }, [initial, profilesQuery.isPending, runAnalysis, vehicleCode]);

  const handleChange = useCallback((patch: Partial<RouteFormState>) => {
    setForm((current) => ({ ...current, ...patch }));
  }, []);

  const result = analysis.data;

  return (
    <div className="space-y-6">
      <PageHeader title={t.routes.title} description={t.routes.subtitle} />

      <RouteForm
        value={form}
        vehicleCode={vehicleCode}
        onChange={handleChange}
        onSubmit={handleSubmit}
        pending={analysis.isPending}
        corridors={corridors}
        profiles={profiles}
        profilesLoading={profilesQuery.isPending}
        profilesFailed={profilesQuery.isError}
      />

      {analysis.isPending ? (
        <AnalysisSkeleton />
      ) : analysis.isError ? (
        <ErrorState
          error={analysis.error}
          onRetry={() => {
            resetAnalysis();
            runAnalysis(form, vehicleCode);
          }}
        />
      ) : result ? (
        <RouteAnalysisResult
          analysis={result.data}
          dataMode={result.dataMode}
          plan={charging.data}
          planPending={charging.isPending}
          planError={charging.isError ? charging.error : null}
          onRetryPlan={() => optimise(toAnalyzeRequest(form, vehicleCode))}
          selectedOrdinal={selectedOrdinal}
          onSelectSegment={setSelectedOrdinal}
          selectedStationId={selectedStationId}
          onSelectStation={setSelectedStationId}
        />
      ) : (
        <EmptyState
          icon={RouteIcon}
          title={t.routes.emptyTitle}
          description={t.routes.emptyBody}
          className="py-16"
        />
      )}
    </div>
  );
}
