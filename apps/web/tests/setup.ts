import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(cleanup);

// jsdom implements neither matchMedia nor ResizeObserver; next-themes, Radix and
// MapLibre all reach for them during render.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
});

class ResizeObserverStub {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;

// MapLibre GL requires WebGL, which jsdom does not provide. Unit tests never render the
// real map — they render the declarative layer descriptors instead.
vi.mock("maplibre-gl", () => ({
  default: {
    Map: class {
      on() {}
      off() {}
      remove() {}
      addControl() {}
      getCanvas() {
        return { style: {} };
      }
    },
    NavigationControl: class {},
    ScaleControl: class {},
    AttributionControl: class {},
    LngLatBounds: class {
      extend() {
        return this;
      }
    },
  },
}));
