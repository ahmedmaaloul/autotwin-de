import { useSyncExternalStore } from "react";

/**
 * A clock that ticks once a minute, shared by every component that displays a relative time.
 *
 * One interval for the whole page rather than one per `<DataFreshness>`: a dashboard can easily
 * show a dozen of them, and twelve independent timers waking the main thread at different
 * offsets is exactly the kind of thing that makes a map stutter.
 *
 * The snapshot is the minute bucket, so React re-renders only when the displayed value can
 * actually have changed.
 */
const listeners = new Set<() => void>();
let interval: ReturnType<typeof setInterval> | null = null;
let snapshot = 0;

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  if (interval === null) {
    snapshot = Math.floor(Date.now() / 60_000);
    interval = setInterval(() => {
      snapshot = Math.floor(Date.now() / 60_000);
      for (const listener of listeners) listener();
    }, 60_000);
  }
  return () => {
    listeners.delete(onChange);
    if (listeners.size === 0 && interval !== null) {
      clearInterval(interval);
      interval = null;
    }
  };
}

export function useMinuteTick(): number {
  return useSyncExternalStore(
    subscribe,
    () => snapshot,
    () => 0,
  );
}
