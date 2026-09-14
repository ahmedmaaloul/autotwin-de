import { useSyncExternalStore } from "react";

const MOBILE_BREAKPOINT = 768;
const QUERY = `(max-width: ${MOBILE_BREAKPOINT - 1}px)`;

/**
 * Viewport width as an external store.
 *
 * `useSyncExternalStore` rather than `useState` + `useEffect`: the viewport *is* an external
 * system, the server snapshot is explicit (`false` — this is a desktop-first dashboard), and
 * there is no render where the value is wrong and then corrects itself.
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

export function useIsMobile(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
