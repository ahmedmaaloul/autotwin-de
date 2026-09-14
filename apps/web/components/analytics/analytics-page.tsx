"use client";

import { PageHeader } from "@/components/shared/section-header";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useTranslations } from "@/providers/locale-provider";

import { CorridorCoveragePanel } from "./corridor-coverage-panel";
import { EnergyAnalyticsPanel } from "./energy-analytics-panel";
import { RegionalPanel } from "./regional-panel";
import { UnderservedPanel } from "./underserved-panel";

/**
 * Four analyses that share a page because they share a subject: where the infrastructure is
 * thin, and what the energy actually costs.
 *
 * Tabs rather than one long scroll — each panel is a parameterised query, and mounting all four
 * at once would fire four expensive PostGIS aggregations nobody asked for. Radix unmounts the
 * inactive panels, so switching tabs is also what stops the previous analysis from polling.
 */
export function AnalyticsPage() {
  const t = useTranslations();

  return (
    <div className="space-y-6">
      <PageHeader title={t.analytics.title} description={t.analytics.subtitle} />

      <Tabs defaultValue="coverage">
        {/* The list scrolls rather than the page: four German labels do not fit at 360 px. */}
        <div className="-mx-1 max-w-full overflow-x-auto px-1 pb-1">
          <TabsList>
            <TabsTrigger value="coverage">{t.analytics.coverage}</TabsTrigger>
            <TabsTrigger value="underserved">{t.analytics.underserved}</TabsTrigger>
            <TabsTrigger value="energy">{t.analytics.energy}</TabsTrigger>
            <TabsTrigger value="regional">{t.analytics.regional}</TabsTrigger>
          </TabsList>
        </div>

        <TabsContent value="coverage" className="mt-4">
          <CorridorCoveragePanel />
        </TabsContent>
        <TabsContent value="underserved" className="mt-4">
          <UnderservedPanel />
        </TabsContent>
        <TabsContent value="energy" className="mt-4">
          <EnergyAnalyticsPanel />
        </TabsContent>
        <TabsContent value="regional" className="mt-4">
          <RegionalPanel />
        </TabsContent>
      </Tabs>
    </div>
  );
}
