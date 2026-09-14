import { useSyncExternalStore } from "react";

const noop = () => () => {};

/**
 * True once the component has hydrated in the browser.
 *
 * Used by the handful of components whose correct output is genuinely unknowable on the server
 * — a theme icon, a relative timestamp. `useSyncExternalStore` gives a different value for the
 * server and client snapshot without a setState-in-effect cascade.
 */
export function useMounted(): boolean {
  return useSyncExternalStore(
    noop,
    () => true,
    () => false,
  );
}
