"use client";

import { useEffect, useRef, useState } from "react";

export interface Sample {
  at: number;
  value: number;
}

/**
 * A rolling window of a polled value, kept in component state.
 *
 * This is derived *UI* state, not server state: the API reports an instantaneous rate and has no
 * opinion about history, so the history exists only for as long as someone is watching the
 * page. Keeping it in TanStack Query would be dishonest about what the server knows.
 *
 * Sampling runs on its own interval rather than reacting to the query updating. Two reasons: a
 * time series needs evenly spaced points to be readable, and publishing state from an effect
 * body is exactly the cascading-render pattern React 19 rejects — here `setState` runs in a
 * timer callback.
 */
export function useSampleHistory(
  value: number | null | undefined,
  { intervalMs = 3000, size = 60 }: { intervalMs?: number; size?: number } = {},
): Sample[] {
  const [samples, setSamples] = useState<Sample[]>([]);

  const latestRef = useRef(value);
  useEffect(() => {
    latestRef.current = value;
  }, [value]);

  useEffect(() => {
    const id = window.setInterval(() => {
      const current = latestRef.current;
      if (current == null || !Number.isFinite(current)) return;
      setSamples((previous) => {
        const next = [...previous, { at: Date.now(), value: current }];
        return next.length > size ? next.slice(next.length - size) : next;
      });
    }, intervalMs);

    return () => window.clearInterval(id);
  }, [intervalMs, size]);

  return samples;
}
