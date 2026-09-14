"use client";

import { FlaskConical } from "lucide-react";

import { SourceBadge } from "@/components/shared/source-badge";
import { useTranslations } from "@/providers/locale-provider";

import { EnergyBucketChart } from "./energy-bucket-chart";
import { NoticePanel } from "./notice-panel";

/**
 * How driving conditions relate to energy consumption.
 *
 * The three dimensions the physical model actually reasons in — temperature (battery and cabin
 * conditioning), speed (aerodynamic drag), traffic (stop-and-go) — each as its own question.
 * The banner is not boilerplate: these curves look like measurements, and the reader is entitled
 * to know before reading them that they came out of the simulator (ADR 004).
 */
export function EnergyAnalyticsPanel() {
  const t = useTranslations();

  return (
    <div className="space-y-4">
      <NoticePanel
        icon={FlaskConical}
        tone="caution"
        title={t.analytics.energy}
        actions={<SourceBadge origin="simulated" source="simulator" />}
      >
        {t.analytics.simulatedTelemetryNotice}
      </NoticePanel>

      <div className="grid gap-4 xl:grid-cols-3">
        <EnergyBucketChart
          dimension="temperature"
          title={t.analytics.temperatureVsConsumption}
          question={t.analytics.energyQuestionTemperature}
          axisTitle={`${t.analytics.axisTemperature} (${t.units.celsius})`}
        />
        <EnergyBucketChart
          dimension="speed"
          title={t.analytics.speedVsConsumption}
          question={t.analytics.energyQuestionSpeed}
          axisTitle={`${t.analytics.axisSpeed} (${t.units.kmh})`}
        />
        <EnergyBucketChart
          dimension="traffic"
          title={t.analytics.trafficVsConsumption}
          question={t.analytics.energyQuestionTraffic}
          axisTitle={t.analytics.axisTraffic}
        />
      </div>
    </div>
  );
}
