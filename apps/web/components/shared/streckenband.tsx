"use client";

import { useMemo, useRef, useState } from "react";

import { formatNumber } from "@/lib/i18n/format";
import { useLocale, useTranslations } from "@/providers/locale-provider";
import { cn } from "@/lib/utils";
import type { ChargingStop, EnergyIntensity, RouteAnalysis, TrafficEvent } from "@/types/domain";

/**
 * The Streckenband — a route strip diagram.
 *
 * German road and rail engineers have used this form for a century: the journey flattened onto
 * a single linear scale, with everything that happens along it stacked in registers above and
 * below the line. It is the signature component of AutoTwin DE (docs/DESIGN_SYSTEM.md §7) and
 * the one place the design is allowed to be loud.
 *
 *   Verkehr   ▲ roadworks            ▲ congestion
 *             ─────────────────────────────────────────
 *   Energie   ████▓▓▓░░░▓▓████▓▓░░░░░░░░▓▓▓████████████   ← one rect per segment
 *   SOC       ╲__________________________________________  ← state of charge across the band
 *             ─┼──────────┼──────────┼──────────┼────────
 *   Laden      │ ⚡300 kW              ⚡150 kW
 *   km         0         50        100        150     204
 *
 * Drawn as plain SVG rather than with a charting library: Recharts can draw a bar chart, but it
 * cannot express four registers sharing one distance axis with independent interaction, and
 * bending it into that shape would cost more than the 200 lines below.
 *
 * Accessibility is not bolted on. The energy band is a `role="group"` of focusable rects, each
 * with an `aria-label` stating its numbers, so the whole diagram is navigable with the keyboard
 * and legible to a screen reader — which a canvas-based chart never is.
 */

const VIEW_WIDTH = 1000;
const BAND_TOP = 34;
const BAND_HEIGHT = 26;
const AXIS_Y = BAND_TOP + BAND_HEIGHT + 22;
const VIEW_HEIGHT = 132;

const INTENSITY_FILL: Record<EnergyIntensity, string> = {
  low: "var(--energy-low)",
  medium: "var(--energy-medium)",
  high: "var(--energy-high)",
  critical: "var(--energy-critical)",
};

export interface StreckenbandProps {
  analysis: RouteAnalysis;
  stops?: ChargingStop[];
  selectedOrdinal?: number | null;
  onSelectSegment?: (ordinal: number | null) => void;
  className?: string;
}

export function Streckenband({
  analysis,
  stops = [],
  selectedOrdinal = null,
  onSelectSegment,
  className,
}: StreckenbandProps) {
  const t = useTranslations();
  const locale = useLocale();
  const [hovered, setHovered] = useState<number | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const totalKm = analysis.route.distance_m / 1000;

  const geometry = useMemo(() => {
    const toX = (km: number) => (totalKm > 0 ? (km / totalKm) * VIEW_WIDTH : 0);

    const segments = analysis.segments.map((segment) => {
      const x = toX(segment.start_offset_km);
      // Minimum 1px so a very short segment never disappears entirely from the band.
      const width = Math.max(toX(segment.distance_km), 1);
      return { segment, x, width };
    });

    // SOC is drawn as a path across the band: 100 % at the top edge, 0 % at the bottom.
    const socY = (percent: number) =>
      BAND_TOP + BAND_HEIGHT - (Math.max(0, Math.min(100, percent)) / 100) * BAND_HEIGHT;

    const socPoints = [
      `${0},${socY(analysis.start_soc_percent)}`,
      ...analysis.segments.map(
        (segment) =>
          `${toX(segment.start_offset_km + segment.distance_km)},${socY(segment.soc_at_end_percent)}`,
      ),
    ];

    const ticks = buildTicks(totalKm).map((km) => ({ km, x: toX(km) }));

    const events = analysis.traffic_events
      .map((event) => {
        const km = estimateEventOffsetKm(event, analysis);
        return km == null ? null : { event, x: toX(km), km };
      })
      .filter((entry): entry is { event: TrafficEvent; x: number; km: number } => entry !== null);

    const chargingStops = stops.map((stop) => ({ stop, x: toX(stop.offset_km) }));

    return { segments, socPath: `M ${socPoints.join(" L ")}`, ticks, events, chargingStops };
  }, [analysis, stops, totalKm]);

  const active = hovered ?? selectedOrdinal;
  const activeSegment =
    active == null ? null : (analysis.segments.find((s) => s.ordinal === active) ?? null);

  return (
    <div className={cn("w-full", className)}>
      <div ref={containerRef} className="scrollbar-thin w-full overflow-x-auto">
        <svg
          viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
          className="h-[132px] w-full min-w-[640px]"
          preserveAspectRatio="none"
          role="img"
          aria-label={`${t.routes.strip}: ${analysis.route.origin.name} – ${analysis.route.destination.name}`}
        >
          {/* register labels ------------------------------------------------ */}
          <g className="fill-muted-foreground font-[family-name:var(--font-condensed)] text-[9px] tracking-wider uppercase">
            <text x={0} y={10}>
              {t.routes.trafficEvents}
            </text>
            <text x={0} y={BAND_TOP - 4}>
              {t.routes.energyIntensity}
            </text>
            <text x={0} y={AXIS_Y + 26}>
              {t.charging.stations}
            </text>
          </g>

          {/* traffic register ----------------------------------------------- */}
          <g>
            {geometry.events.map(({ event, x }, index) => (
              <g key={`${event.id}-${index}`} transform={`translate(${x}, 0)`}>
                <path
                  d="M 0 20 L -4 14 L 4 14 Z"
                  fill={
                    event.severity === "severe" || event.severity === "high"
                      ? "var(--danger)"
                      : "var(--warning)"
                  }
                />
                <line
                  x1={0}
                  y1={20}
                  x2={0}
                  y2={BAND_TOP}
                  stroke="var(--border)"
                  strokeWidth={1}
                  strokeDasharray="2 2"
                />
                <title>
                  {event.road_name ? `${event.road_name} · ` : ""}
                  {event.title}
                </title>
              </g>
            ))}
          </g>

          {/* energy band ----------------------------------------------------- */}
          <g
            role="group"
            aria-label={t.routes.segments}
            onMouseLeave={() => setHovered(null)}
          >
            {geometry.segments.map(({ segment, x, width }) => {
              const isActive = active === segment.ordinal;
              return (
                <rect
                  key={segment.ordinal}
                  x={x}
                  y={BAND_TOP}
                  width={width}
                  height={BAND_HEIGHT}
                  fill={INTENSITY_FILL[segment.energy_intensity]}
                  opacity={active == null || isActive ? 1 : 0.45}
                  stroke={isActive ? "var(--foreground)" : "none"}
                  strokeWidth={isActive ? 1.5 : 0}
                  tabIndex={0}
                  role="button"
                  className="cursor-pointer transition-opacity duration-200 focus-visible:outline-none motion-reduce:transition-none"
                  aria-label={segmentAriaLabel(segment, locale, t)}
                  onMouseEnter={() => setHovered(segment.ordinal)}
                  onFocus={() => setHovered(segment.ordinal)}
                  onBlur={() => setHovered(null)}
                  onClick={() =>
                    onSelectSegment?.(selectedOrdinal === segment.ordinal ? null : segment.ordinal)
                  }
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelectSegment?.(
                        selectedOrdinal === segment.ordinal ? null : segment.ordinal,
                      );
                    }
                  }}
                />
              );
            })}
          </g>

          {/* SOC trajectory --------------------------------------------------- */}
          <path
            d={geometry.socPath}
            fill="none"
            stroke="var(--foreground)"
            strokeWidth={1.5}
            strokeLinejoin="round"
            opacity={0.7}
            vectorEffect="non-scaling-stroke"
          />

          {/* distance axis ---------------------------------------------------- */}
          <line
            x1={0}
            y1={AXIS_Y}
            x2={VIEW_WIDTH}
            y2={AXIS_Y}
            stroke="var(--border)"
            strokeWidth={1}
            vectorEffect="non-scaling-stroke"
          />
          <g className="fill-muted-foreground font-[family-name:var(--font-mono)] text-[9px]">
            {geometry.ticks.map(({ km, x }) => (
              <g key={km}>
                <line
                  x1={x}
                  y1={AXIS_Y - 3}
                  x2={x}
                  y2={AXIS_Y + 3}
                  stroke="var(--border)"
                  strokeWidth={1}
                  vectorEffect="non-scaling-stroke"
                />
                <text
                  x={x}
                  y={AXIS_Y + 14}
                  textAnchor={x === 0 ? "start" : x >= VIEW_WIDTH - 1 ? "end" : "middle"}
                >
                  {km}
                </text>
              </g>
            ))}
          </g>

          {/* charging register ------------------------------------------------ */}
          <g>
            {geometry.chargingStops.map(({ stop, x }, index) => (
              <g key={`${stop.station.id}-${index}`} transform={`translate(${x}, 0)`}>
                <line
                  x1={0}
                  y1={AXIS_Y}
                  x2={0}
                  y2={AXIS_Y + 20}
                  stroke="var(--primary)"
                  strokeWidth={1.5}
                  vectorEffect="non-scaling-stroke"
                />
                <circle cx={0} cy={AXIS_Y + 22} r={3} fill="var(--primary)" />
                <text
                  x={6}
                  y={AXIS_Y + 26}
                  className="fill-primary font-[family-name:var(--font-mono)] text-[9px]"
                >
                  {Math.round(stop.max_power_kw)} kW
                </text>
                <title>{stop.station.operator ?? stop.station.city ?? ""}</title>
              </g>
            ))}
          </g>
        </svg>
      </div>

      {/* detail readout — outside the SVG so it can use real typography */}
      <div className="border-border mt-2 min-h-[2.25rem] border-t pt-2">
        {activeSegment ? (
          <dl className="text-muted-foreground flex flex-wrap items-baseline gap-x-5 gap-y-1 text-xs">
            <SegmentFact
              label={`${t.routes.segment} ${activeSegment.ordinal + 1}`}
              value={`${formatNumber(activeSegment.start_offset_km, locale, { decimals: 0 })}–${formatNumber(activeSegment.start_offset_km + activeSegment.distance_km, locale, { decimals: 0 })} km`}
            />
            <SegmentFact
              label={t.routes.assumedSpeed}
              value={`${formatNumber(activeSegment.assumed_speed_kmh, locale)} km/h`}
            />
            <SegmentFact
              label={t.live.outsideTemp}
              value={
                activeSegment.temperature_c == null
                  ? "–"
                  : `${formatNumber(activeSegment.temperature_c, locale, { decimals: 1 })} °C`
              }
            />
            <SegmentFact
              label={t.routes.trafficSeverity}
              value={activeSegment.traffic_severity ?? "–"}
            />
            <SegmentFact
              label={t.routes.predictedConsumption}
              value={`${formatNumber(activeSegment.kwh_per_100km, locale, { decimals: 1 })} kWh/100 km`}
              emphasis
            />
            <SegmentFact
              label={t.live.soc}
              value={`${formatNumber(activeSegment.soc_at_end_percent, locale, { decimals: 0 })} %`}
              emphasis
            />
          </dl>
        ) : (
          <p className="text-muted-foreground text-xs">{t.routes.stripHint}</p>
        )}
      </div>
    </div>
  );
}

function SegmentFact({
  label,
  value,
  emphasis = false,
}: {
  label: string;
  value: string;
  emphasis?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="eyebrow">{label}</dt>
      <dd
        className={cn(
          "font-mono text-xs tabular-nums",
          emphasis ? "text-foreground font-medium" : "text-muted-foreground",
        )}
      >
        {value}
      </dd>
    </div>
  );
}

function segmentAriaLabel(
  segment: RouteAnalysis["segments"][number],
  locale: ReturnType<typeof useLocale>,
  t: ReturnType<typeof useTranslations>,
): string {
  const km = formatNumber(segment.start_offset_km, locale, { decimals: 0 });
  const consumption = formatNumber(segment.kwh_per_100km, locale, { decimals: 1 });
  const soc = formatNumber(segment.soc_at_end_percent, locale, { decimals: 0 });
  return `${t.routes.segment} ${segment.ordinal + 1}, ${t.common.from} ${km} km, ${consumption} kWh/100 km, ${t.live.soc} ${soc} %`;
}

/**
 * Round tick spacing so the axis reads 0, 50, 100 … rather than 0, 41, 82.
 *
 * The final tick is always the route's true length, because a reader checking "how long is
 * this trip?" should find the answer on the axis. That means the last rounded tick can land
 * close enough to it that the two labels overlap — 200 and 203 render as "20020" at typical
 * widths — so a rounded tick within a third of a step of the end is dropped in its favour.
 */
function buildTicks(totalKm: number): number[] {
  if (totalKm <= 0) return [0];
  const target = 6;
  const raw = totalKm / target;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  // Labels are whole kilometres, so a sub-kilometre step would round several ticks onto the
  // same number: a 3 km route produced 0,1,1,2,2,3 — overlapping labels *and* duplicate React
  // keys. One kilometre is the finest step the axis can actually render.
  const step = Math.max(
    1,
    [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? magnitude * 10,
  );

  const end = Math.round(totalKm);
  const minSpacing = step / 3;

  const ticks: number[] = [];
  for (let km = 0; km < totalKm; km += step) {
    const value = Math.round(km);
    if (value !== 0 && end - value < minSpacing) continue;
    ticks.push(value);
  }
  ticks.push(end);
  // Belt and braces: whatever the step, the rendered labels must be unique, because they are
  // also the React keys.
  return [...new Set(ticks)];
}

/**
 * Place a traffic event on the distance axis.
 *
 * The API matches events to segments server-side; where it has already done so the event
 * carries the segment offset. This fallback projects by nearest segment midpoint so the strip
 * still shows something useful when the match is absent, and returns null rather than guessing
 * when the event is nowhere near the route.
 */
function estimateEventOffsetKm(event: TrafficEvent, analysis: RouteAnalysis): number | null {
  let best: { km: number; distance: number } | null = null;

  for (const segment of analysis.segments) {
    if (!segment.geometry) continue;
    const coords = segment.geometry.coordinates;
    const mid = coords[Math.floor(coords.length / 2)];
    if (!mid) continue;
    const dLon = (mid[0] - event.longitude) * Math.cos((event.latitude * Math.PI) / 180);
    const dLat = mid[1] - event.latitude;
    const distance = Math.hypot(dLon, dLat) * 111;
    if (!best || distance < best.distance) {
      best = { km: segment.start_offset_km + segment.distance_km / 2, distance };
    }
  }

  // ~15 km is generous for a corridor match but tight enough to exclude an event on a parallel
  // Autobahn that happens to be in the same bounding box.
  return best && best.distance <= 15 ? best.km : null;
}
