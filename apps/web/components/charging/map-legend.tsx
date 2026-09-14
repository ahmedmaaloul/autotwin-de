"use client";

import { useTranslations } from "@/providers/locale-provider";
import type { ChargingCategory } from "@/types/domain";

import { categoryBand, categoryLabel, CATEGORY_COLOR } from "./charging-vocabulary";

const CATEGORIES: ChargingCategory[] = ["normal", "fast", "ultra_fast"];

/**
 * What the map is actually encoding.
 *
 * The charging layer carries two different visual grammars at two zoom levels — clusters
 * coloured by the *share* of fast charging inside them, individual sites coloured by category
 * and sized by power — and neither is guessable. An unexplained colour ramp is decoration;
 * an explained one is a measurement.
 */
export function MapLegend() {
  const t = useTranslations();

  // Top-left is the only free corner: MapLibre puts the zoom control top-right, the scale bar
  // bottom-left and the OpenStreetMap attribution bottom-right. That attribution is a licence
  // obligation (DATA_LICENSES.md), so nothing of ours is allowed to sit on top of it.
  return (
    <aside
      aria-label={t.common.legend}
      className="border-border bg-card/95 absolute top-2 left-2 z-10 max-w-[15rem] space-y-3 rounded border p-2.5 text-[0.6875rem] shadow-sm backdrop-blur-[2px]"
    >
      <div className="space-y-1.5">
        <p className="eyebrow">{t.charging.legendClusters}</p>
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-1" aria-hidden>
            <Dot color="var(--muted-foreground)" size={8} />
            <Dot color="var(--primary)" size={11} />
            <Dot color="var(--success)" size={14} />
          </span>
          <span className="text-muted-foreground leading-tight">
            {t.charging.legendFastShareLow} → {t.charging.legendFastShareHigh}
          </span>
        </div>
        <p className="text-muted-foreground leading-tight">{t.charging.legendClustersHint}</p>
      </div>

      <div className="border-border space-y-1.5 border-t pt-2.5">
        <p className="eyebrow">{t.charging.legendStations}</p>
        <ul className="space-y-1">
          {CATEGORIES.map((category, index) => (
            <li key={category} className="flex items-center gap-2">
              <Dot color={CATEGORY_COLOR[category]} size={4 + index * 3} />
              <span className="leading-tight">{categoryLabel(category, t)}</span>
              <span className="text-muted-foreground ml-auto font-mono tabular-nums">
                {categoryBand(category, t)}
              </span>
            </li>
          ))}
        </ul>
        <p className="text-muted-foreground leading-tight">{t.charging.legendStationsHint}</p>
      </div>
    </aside>
  );
}

function Dot({ color, size }: { color: string; size: number }) {
  return (
    <span
      aria-hidden
      className="border-card inline-block shrink-0 rounded-full border"
      style={{ backgroundColor: color, width: size + 4, height: size + 4 }}
    />
  );
}
