"use client";

import { Camera } from "lucide-react";
import { useEffect, useState } from "react";

import { loadManifest, SNAPSHOT_ENABLED } from "@/lib/api/snapshot";
import { formatDateTime, formatNumber } from "@/lib/i18n/format";
import { translate } from "@/lib/i18n/messages";
import { useLocale, useTranslations } from "@/providers/locale-provider";

/**
 * The one banner the hosted demo is never allowed to lose.
 *
 * It states the date the snapshot was captured and that no backend is running behind the page,
 * so a reader who arrives from a CV link cannot mistake frozen data for a live system. It
 * renders nothing in a normal build, where the API is real.
 */
export function SnapshotBanner() {
  const t = useTranslations();
  const locale = useLocale();
  const [capturedAt, setCapturedAt] = useState<string | null>(null);
  const [stations, setStations] = useState<number | null>(null);

  useEffect(() => {
    if (!SNAPSHOT_ENABLED) return;
    let cancelled = false;
    // State is set from the promise callback, never synchronously in the effect body.
    void loadManifest().then((manifest) => {
      if (cancelled) return;
      setCapturedAt(manifest.captured_at);
      const total = manifest.counts.charging_stations_total;
      setStations(typeof total === "number" ? total : null);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!SNAPSHOT_ENABLED) return null;

  const date = capturedAt
    ? formatDateTime(capturedAt, locale, { dateStyle: "long", timeStyle: "short" })
    : "…";

  return (
    <div
      role="status"
      className="border-primary/30 bg-primary/8 text-foreground flex items-start gap-2.5 border-b px-4 py-2 text-xs md:px-6"
    >
      <Camera className="text-primary mt-0.5 size-3.5 shrink-0" aria-hidden />
      <p>
        <span className="font-medium">{translate(t, "snapshot.title", { date })}</span>{" "}
        <span className="text-muted-foreground">
          {translate(t, "snapshot.body", {
            stations: stations == null ? "…" : formatNumber(stations, locale),
          })}
        </span>
      </p>
    </div>
  );
}
