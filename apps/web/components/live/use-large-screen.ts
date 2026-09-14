import { useSyncExternalStore } from "react";

/** Tailwind's `lg` breakpoint — where the vehicle panel stops being a sheet and becomes a column. */
const LARGE_BREAKPOINT = 1024;
const QUERY = `(min-width: ${LARGE_BREAKPOINT}px)`;

/**
 * Whether the viewport is wide enough for the fixed vehicle column.
 *
 * `useSyncExternalStore` for the same reason `use-mobile.ts` does it: the viewport is an
 * external system, the server snapshot is explicit, and there is no intermediate render where
 * the value is wrong and then corrects itself in an effect.
 *
 * The server snapshot is `false`, so the first client render agrees with the HTML and the
 * sheet — which is only ever opened by a user selecting a vehicle — cannot flash open during
 * hydration.
 */
function subscribe(onChange: () => void): () => void {
  const media = window.matchMedia(QUERY);
  media.addEventListener("change", onChange);
  return () => media.removeEventListener("change", onChange);
}

function getSnapshot(): boolean {
  return window.matchMedia(QUERY).matches;
}

function getServerSnapshot(): boolean {
  return false;
}

export function useIsLargeScreen(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
