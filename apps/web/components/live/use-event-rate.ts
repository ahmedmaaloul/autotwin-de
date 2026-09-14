"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Events per second, measured from a monotonically increasing counter.
 *
 * The SSE hook counts telemetry events but deliberately does not time them — a rate is a
 * property of the *display*, not of the stream, and computing it inside the subscription would
 * force a render on every sample.
 *
 * The measurement runs on its own one-second interval rather than reacting to the counter
 * changing. That is both correct (a rate needs a fixed window; reacting to arrivals would
 * report a rate of zero exactly when the stream goes quiet) and required by the React 19 rule
 * against publishing state from an effect body: `setState` here happens in a timer callback.
 */
export function useEventRate(totalEvents: number, windowMs = 1000): number {
  const [rate, setRate] = useState(0);

  // The newest counter value, kept out of the interval's dependency list so the timer is
  // installed once instead of being torn down and re-created on every telemetry frame.
  const latestRef = useRef(totalEvents);
  useEffect(() => {
    latestRef.current = totalEvents;
  }, [totalEvents]);

  useEffect(() => {
    let previousCount = latestRef.current;
    let previousAt = Date.now();

    const id = window.setInterval(() => {
      const now = Date.now();
      const count = latestRef.current;
      const elapsedSeconds = (now - previousAt) / 1000;
      if (elapsedSeconds > 0) {
        // A restarted stream resets the counter; clamp rather than report a negative rate.
        const delta = Math.max(0, count - previousCount);
        setRate(delta / elapsedSeconds);
      }
      previousCount = count;
      previousAt = now;
    }, windowMs);

    return () => window.clearInterval(id);
  }, [windowMs]);

  return rate;
}
