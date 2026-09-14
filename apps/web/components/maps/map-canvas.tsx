"use client";

import maplibregl, { type LngLatBoundsLike, type Map as MapLibreMap } from "maplibre-gl";
import { useTheme } from "next-themes";
import {
  createContext,
  use,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { GERMANY_BOUNDS, GERMANY_CENTER } from "@/lib/constants";
import { paletteFor, styleUrlFor, TILE_ATTRIBUTION, type MapPalette, type MapTheme } from "@/lib/map/style";
import { cn } from "@/lib/utils";

import "maplibre-gl/dist/maplibre-gl.css";

interface MapContextValue {
  map: MapLibreMap | null;
  /** True once the style has loaded — sources and layers may only be added after this. */
  ready: boolean;
  theme: MapTheme;
  palette: MapPalette;
  /** Bumped on every style reload so layer components know to re-add themselves. */
  styleEpoch: number;
}

const MapContext = createContext<MapContextValue>({
  map: null,
  ready: false,
  theme: "light",
  palette: paletteFor("light"),
  styleEpoch: 0,
});

/**
 * Access the map from a layer component.
 *
 * Pages never use this. Pages compose `<MapCanvas>` with layer children; only the layer
 * components under `components/maps/layers/` touch the MapLibre instance
 * (docs/DESIGN_SYSTEM.md §8).
 */
export function useMapContext(): MapContextValue {
  return use(MapContext);
}

export interface MapCanvasProps {
  children?: ReactNode;
  className?: string;
  initialBounds?: LngLatBoundsLike;
  initialCenter?: [number, number];
  initialZoom?: number;
  /** Hide the zoom/compass control on small embedded maps. */
  interactive?: boolean;
  showNavigation?: boolean;
  showScale?: boolean;
  onReady?: (map: MapLibreMap) => void;
  ariaLabel: string;
}

/**
 * The single owner of a `maplibregl.Map`.
 *
 * Three behaviours are worth knowing about:
 *
 * 1. **Readiness is `style.load`, not `load`.** `load` fires only after the style *and* the
 *    first full render of every source have completed. That is stricter than what adding a
 *    source or layer actually requires, and it never arrives at all when the render loop is
 *    throttled — a hidden tab, a background window, or a headless browser. The map then paints
 *    correctly while every application layer silently fails to attach. `style.load` fires as
 *    soon as the style is parsed, which is exactly the precondition for `addSource`/`addLayer`.
 *
 * 2. **Theme switching reloads the style.** `setStyle()` discards every source and layer the
 *    application added. `style.load` fires again afterwards, so the same handler bumps
 *    `styleEpoch` and every layer component re-attaches itself. Without that, switching to dark
 *    mode would silently empty the map.
 *
 * 3. **The instance is created once and never re-created.** React re-renders must not tear
 *    down a WebGL context — that is the difference between a map that pans smoothly with 300
 *    vehicles on it and one that stutters.
 */
export function MapCanvas({
  children,
  className,
  initialBounds = GERMANY_BOUNDS,
  initialCenter = GERMANY_CENTER,
  initialZoom = 5.2,
  interactive = true,
  showNavigation = true,
  showScale = true,
  onReady,
  ariaLabel,
}: MapCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  // The instance lives in state, not a ref: layer components read it during render through the
  // context, and a ref read during render is invisible to React's update tracking.
  const [map, setMap] = useState<MapLibreMap | null>(null);
  const [ready, setReady] = useState(false);
  const [styleEpoch, setStyleEpoch] = useState(0);
  const { resolvedTheme } = useTheme();
  const id = useId();

  const theme: MapTheme = resolvedTheme === "dark" ? "dark" : "light";
  const palette = useMemo(() => paletteFor(theme), [theme]);

  // Captured once: the mount effect needs the theme that was current when it ran, and must not
  // re-run when the theme later changes (the second effect handles that).
  const [initialTheme] = useState(theme);
  // The theme whose style is currently applied. A ref, not state: it is written inside an
  // effect and never read during render, so it cannot drive a re-render of its own.
  const appliedThemeRef = useRef(initialTheme);

  const handleReady = useCallback(
    (map: MapLibreMap) => {
      onReady?.(map);
    },
    [onReady],
  );

  useEffect(() => {
    if (!containerRef.current) return;

    const instance = new maplibregl.Map({
      container: containerRef.current,
      style: styleUrlFor(initialTheme),
      center: initialCenter,
      zoom: initialZoom,
      bounds: initialBounds,
      fitBoundsOptions: { padding: 48 },
      interactive,
      attributionControl: false,
      // The basemap is desaturated on purpose; the data layers carry all the colour.
      maxZoom: 18,
      minZoom: 4,
      // Pitch and rotation add nothing to a 2-D infrastructure map and make the north-up
      // reading of a corridor harder, so they are disabled.
      pitchWithRotate: false,
      dragRotate: false,
      touchZoomRotate: true,
    });

    instance.addControl(
      new maplibregl.AttributionControl({ compact: true, customAttribution: TILE_ATTRIBUTION }),
      "bottom-right",
    );
    if (showNavigation) {
      instance.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    }
    if (showScale) {
      instance.addControl(
        new maplibregl.ScaleControl({ maxWidth: 120, unit: "metric" }),
        "bottom-left",
      );
    }

    // Readiness is driven entirely by the map's own events, so the theme effect below never
    // has to write React state synchronously.
    instance.on("styledataloading", () => setReady(false));
    // Fires on the initial style AND after every setStyle(), which is what makes the theme
    // swap self-healing: bump the epoch and the layer components re-attach.
    instance.on("style.load", () => {
      setReady(true);
      setStyleEpoch((epoch) => epoch + 1);
    });
    instance.once("style.load", () => handleReady(instance));
    setMap(instance);

    // Development-only handle. A WebGL map cannot be inspected through the DOM, so without
    // this there is no way to ask "did my layer actually get added?" from the console or from
    // a Playwright test. Never exposed in a production build.
    if (process.env.NODE_ENV !== "production") {
      (window as unknown as { __autotwinMap?: MapLibreMap }).__autotwinMap = instance;
    }

    return () => {
      instance.remove();
      setMap(null);
      setReady(false);
    };
    // Mount-only: a React re-render must never tear down a WebGL context.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Theme changes swap the basemap style; the `style.load` handler above restores readiness
  // and invalidates every application layer.
  useEffect(() => {
    if (!map || theme === appliedThemeRef.current) return;
    appliedThemeRef.current = theme;
    map.setStyle(styleUrlFor(theme));
  }, [map, theme]);

  const value = useMemo<MapContextValue>(
    () => ({ map, ready, theme, palette, styleEpoch }),
    [map, ready, theme, palette, styleEpoch],
  );

  return (
    <div className={cn("bg-muted relative isolate size-full overflow-hidden", className)}>
      <div
        ref={containerRef}
        id={id}
        className="size-full"
        role="application"
        aria-label={ariaLabel}
      />
      <MapContext value={value}>{children}</MapContext>
    </div>
  );
}
