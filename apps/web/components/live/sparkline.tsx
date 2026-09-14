"use client";

import { useMemo } from "react";
import { Line, LineChart, ResponsiveContainer, YAxis } from "recharts";

import { cn } from "@/lib/utils";

/**
 * A 44 px trace of one telemetry channel.
 *
 * No axes, no grid, no tooltip: the numeric value is already shown beside it, so the line only
 * has to answer "and which way is it going?". Keeping it this bare is what makes it cheap
 * enough to render three of them next to a map that is animating several hundred markers —
 * animation is off, and the series is capped by the caller.
 *
 * The domain is padded by a twentieth of the range so a flat line sits in the middle of the
 * band instead of being clipped to the baseline.
 */
export function Sparkline({
  values,
  color,
  ariaLabel,
  className,
}: {
  values: number[];
  color: string;
  ariaLabel: string;
  className?: string;
}) {
  const data = useMemo(() => values.map((value, index) => ({ index, value })), [values]);

  const domain = useMemo<[number, number]>(() => {
    if (values.length === 0) return [0, 1];
    const min = Math.min(...values);
    const max = Math.max(...values);
    const padding = Math.max((max - min) / 20, Math.abs(max) / 50, 0.5);
    return [min - padding, max + padding];
  }, [values]);

  return (
    <div className={cn("h-11 w-full", className)} role="img" aria-label={ariaLabel}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 3, right: 1, bottom: 3, left: 1 }}>
          <YAxis hide domain={domain} />
          <Line
            type="monotone"
            dataKey="value"
            stroke={color}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
