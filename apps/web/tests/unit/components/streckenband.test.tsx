import { fireEvent, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Streckenband } from "@/components/shared/streckenband";
import { de } from "@/lib/i18n/messages/de";
import { en } from "@/lib/i18n/messages/en";

import {
  FOUR_SEGMENTS,
  lineThrough,
  makeAnalysis,
  makeSegment,
  makeStop,
  makeTrafficEvent,
} from "../fixtures/route-analysis";
import { renderWithProviders } from "../render";

/**
 * The Streckenband is the signature instrument of the product (DESIGN_SYSTEM §7), and the one
 * component where being wrong is invisible: a mis-scaled rect still looks like a route strip.
 *
 * Two contracts are tested here.
 *
 *  1. Accessibility. The band is a `role="group"` of focusable segments, each labelled with its
 *     own numbers. That is the only way the diagram is readable without a mouse or without
 *     sight, and it is the reason the component is SVG rather than canvas.
 *  2. Geometry. Every coordinate below is derived by hand: the component draws into a
 *     1000-unit viewBox, so the default 200 km fixture scales at exactly 5 units per kilometre,
 *     and a 50 km segment is 250 units wide. Nothing here is copied from a rendered snapshot.
 */

/** viewBox width, from the component's own coordinate system. */
const VIEW_WIDTH = 1000;
/** The energy band occupies y = 34 … 60 (DESIGN_SYSTEM §7: fixed ~132 px height). */
const BAND_TOP = 34;
const BAND_BOTTOM = 60;

function segmentButtons(): HTMLElement[] {
  const band = screen.getByRole("group", { name: de.routes.segments });
  return within(band).queryAllByRole("button");
}

/** Numeric axis labels, in document order. Register captions are words, so digits isolate ticks. */
function tickValues(container: HTMLElement): number[] {
  return [...container.querySelectorAll("text")]
    .map((node) => node.textContent ?? "")
    .filter((text) => /^\d+$/.test(text))
    .map(Number);
}

/** The SOC trajectory is the only stroked, unfilled path in the diagram. */
function socPathData(container: HTMLElement): string {
  const paths = [...container.querySelectorAll("path")].filter(
    (path) => path.getAttribute("fill") === "none",
  );
  expect(paths).toHaveLength(1);
  return paths[0].getAttribute("d") ?? "";
}

function pathPoints(d: string): { x: number; y: number }[] {
  return d
    .replace(/^M /, "")
    .split(" L ")
    .map((pair) => {
      const [x, y] = pair.split(",").map(Number);
      return { x, y };
    });
}

describe("the accessibility contract", () => {
  it("renders one focusable element per segment", () => {
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    const buttons = segmentButtons();

    expect(buttons).toHaveLength(FOUR_SEGMENTS.length);
    for (const button of buttons) {
      // tabindex 0 puts every segment in the natural tab order — the band is navigable with
      // nothing but the keyboard.
      expect(button).toHaveAttribute("tabindex", "0");
    }
  });

  it.each<[number, string]>([
    [0, "Abschnitt 1, von 0 km, 17,2 kWh/100 km, Ladezustand 78 %"],
    [1, "Abschnitt 2, von 50 km, 21,4 kWh/100 km, Ladezustand 63 %"],
    [2, "Abschnitt 3, von 100 km, 24,9 kWh/100 km, Ladezustand 46 %"],
    [3, "Abschnitt 4, von 150 km, 31,6 kWh/100 km, Ladezustand 25 %"],
  ])("labels segment %s with its own consumption and state of charge", (index, expected) => {
    // The label is assembled from the catalogue plus `formatNumber`, so it is also the place a
    // locale mistake would surface first: a screen-reader user hearing "21.4" in a German UI.
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    expect(segmentButtons()[index]).toHaveAttribute("aria-label", expected);
  });

  it("labels segments in English when the UI is English", () => {
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />, { locale: "en" });
    const band = screen.getByRole("group", { name: en.routes.segments });
    expect(within(band).queryAllByRole("button")[1]).toHaveAttribute(
      "aria-label",
      "Segment 2, from 50 km, 21.4 kWh/100 km, State of charge 63 %",
    );
  });

  it("names the diagram after the corridor it shows", () => {
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    // A screen reader announcing "image" would tell the reader nothing; the endpoints do.
    expect(
      screen.getByRole("img", { name: "Streckenband: Frankfurt am Main – Stuttgart" }),
    ).toBeInTheDocument();
  });

  it("marks a focused segment with a visible stroke", () => {
    // The rects set `focus-visible:outline-none`, so the stroke IS the focus indicator. If it
    // stopped being applied, keyboard users would lose all sense of position in the band.
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    const target = segmentButtons()[2];
    expect(target).toHaveAttribute("stroke", "none");

    fireEvent.focusIn(target);
    expect(segmentButtons()[2]).toHaveAttribute("stroke", "var(--foreground)");
  });

  it("survives an analysis with no segments at all", () => {
    // An empty band is a legitimate state (routing succeeded, segmentation did not). It must
    // render the hint rather than throw or draw a degenerate path.
    renderWithProviders(<Streckenband analysis={makeAnalysis({ segments: [] })} />);
    expect(segmentButtons()).toHaveLength(0);
    expect(screen.getByText(de.routes.stripHint)).toBeInTheDocument();
  });

  it("handles a single-segment route", () => {
    const analysis = makeAnalysis({
      distance_m: 50_000,
      segments: [
        makeSegment({
          ordinal: 0,
          start_offset_km: 0,
          distance_km: 50,
          kwh_per_100km: 19.5,
          soc_at_end_percent: 70,
          energy_intensity: "medium",
        }),
      ],
    });
    renderWithProviders(<Streckenband analysis={analysis} />);
    expect(segmentButtons()).toHaveLength(1);
  });
});

describe("band geometry", () => {
  it("scales segments linearly onto the 1000-unit band", () => {
    // 200 km over a 1000-unit viewBox is 5 units per kilometre, so each 50 km segment is
    // 250 units wide and starts at 0 / 250 / 500 / 750.
    const { container } = renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    const rects = [...container.querySelectorAll('rect[role="button"]')];

    expect(rects.map((rect) => rect.getAttribute("x"))).toEqual(["0", "250", "500", "750"]);
    expect(rects.map((rect) => rect.getAttribute("width"))).toEqual(["250", "250", "250", "250"]);
    // The band tiles the full width — a gap here would mean kilometres unaccounted for.
    const last = rects.at(-1);
    expect(Number(last?.getAttribute("x")) + Number(last?.getAttribute("width"))).toBe(VIEW_WIDTH);
  });

  it("never lets a very short segment disappear", () => {
    // 0.1 km at 5 units/km is half a unit, which would round away to nothing on screen. The
    // component floors the width at 1 so an urban stub is still visible and still focusable.
    const analysis = makeAnalysis({
      segments: [
        makeSegment({
          ordinal: 0,
          start_offset_km: 0,
          distance_km: 0.1,
          kwh_per_100km: 15,
          soc_at_end_percent: 89,
          energy_intensity: "low",
        }),
      ],
    });
    const { container } = renderWithProviders(<Streckenband analysis={analysis} />);
    expect(container.querySelector('rect[role="button"]')).toHaveAttribute("width", "1");
  });

  it("emits no NaN for a zero-length route", () => {
    // distance_m = 0 makes the km -> x conversion a division by zero. An SVG attribute of
    // "NaN" silently drops the whole element, so the guard is load-bearing.
    const analysis = makeAnalysis({
      distance_m: 0,
      segments: [
        makeSegment({
          ordinal: 0,
          start_offset_km: 0,
          distance_km: 0,
          kwh_per_100km: 0,
          soc_at_end_percent: 90,
          energy_intensity: "low",
        }),
      ],
    });
    const { container } = renderWithProviders(<Streckenband analysis={analysis} />);

    expect(container.innerHTML).not.toContain("NaN");
    expect(container.innerHTML).not.toContain("Infinity");
    expect(segmentButtons()).toHaveLength(1);
  });

  it("draws the SOC trajectory from the start value through every segment end", () => {
    // One point for the starting SOC plus one per segment end = 5 points for 4 segments, at
    // x = 0, 250, 500, 750, 1000.
    const { container } = renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    const points = pathPoints(socPathData(container));

    expect(points.map((point) => point.x)).toEqual([0, 250, 500, 750, 1000]);
    // 90 % start SOC: y = 34 + 26 - 0.90 * 26 = 36.6, measured down from the top of the band.
    expect(points[0].y).toBeCloseTo(36.6, 6);
    // 25 % arrival SOC: y = 34 + 26 - 0.25 * 26 = 53.5.
    expect(points.at(-1)?.y).toBeCloseTo(53.5, 6);
    // SOC falls along the route, so y increases monotonically for this fixture.
    for (let i = 1; i < points.length; i += 1) {
      expect(points[i].y).toBeGreaterThan(points[i - 1].y);
    }
  });

  it("clamps an out-of-range state of charge into the band", () => {
    // A model can emit >100 % (regeneration overshoot) or <0 % (battery exhausted). Neither may
    // be drawn outside the 34..60 band, where it would overlap the traffic or axis registers.
    const analysis = makeAnalysis({
      distance_m: 200_000,
      start_soc_percent: 120,
      segments: [
        makeSegment({
          ordinal: 0,
          start_offset_km: 0,
          distance_km: 100,
          kwh_per_100km: 20,
          soc_at_end_percent: 150,
          energy_intensity: "low",
        }),
        makeSegment({
          ordinal: 1,
          start_offset_km: 100,
          distance_km: 100,
          kwh_per_100km: 40,
          soc_at_end_percent: -20,
          energy_intensity: "critical",
        }),
      ],
    });

    const { container } = renderWithProviders(<Streckenband analysis={analysis} />);
    const points = pathPoints(socPathData(container));

    for (const point of points) {
      expect(point.y).toBeGreaterThanOrEqual(BAND_TOP);
      expect(point.y).toBeLessThanOrEqual(BAND_BOTTOM);
    }
    // Over 100 % pins to the top edge, under 0 % pins to the bottom edge.
    expect(points[0].y).toBe(BAND_TOP);
    expect(points.at(-1)?.y).toBe(BAND_BOTTOM);
  });
});

describe("the distance axis", () => {
  function analysisOfLength(km: number) {
    return makeAnalysis({
      distance_m: km * 1000,
      segments: [
        makeSegment({
          ordinal: 0,
          start_offset_km: 0,
          distance_km: km,
          kwh_per_100km: 21.4,
          soc_at_end_percent: 40,
          energy_intensity: "medium",
        }),
      ],
    });
  }

  it("uses round ticks and ends on the true route length", () => {
    // Frankfurt–Stuttgart is 203.4 km. Aiming for ~6 ticks gives a raw step of 33.9 km, which
    // rounds up to 50; the run is then 0, 50, 100, 150 and the rounded 200 is dropped because
    // it would collide with the 203 end label. A reader asking "how long is this trip?" finds
    // the answer on the axis.
    const { container } = renderWithProviders(
      <Streckenband analysis={analysisOfLength(203.4)} />,
    );
    expect(tickValues(container)).toEqual([0, 50, 100, 150, 203]);
  });

  it.each<[number, number[]]>([
    // 100 km: raw step 16.7 -> 20.
    [100, [0, 20, 40, 60, 80, 100]],
    // 102 km: the rounded 100 sits 2 km from the end label, well inside a third of the 20 km
    // step, so it is dropped in favour of the true length.
    [102, [0, 20, 40, 60, 80, 102]],
    // 201 km: same rule one step up — 200 is dropped, 201 survives.
    [201, [0, 50, 100, 150, 201]],
    // 200 km: the loop stops before the end, so nothing needs dropping.
    [200, [0, 50, 100, 150, 200]],
    // 1000 km: raw step 166.7 -> 200.
    [1000, [0, 200, 400, 600, 800, 1000]],
  ])("labels a %s km route as %s", (km, expected) => {
    const { container } = renderWithProviders(<Streckenband analysis={analysisOfLength(km)} />);
    expect(tickValues(container)).toEqual(expected);
  });

  it.each([5, 20, 50, 100, 102, 199, 200, 201, 203.4, 500, 1000])(
    "keeps the axis of a %s km route legible",
    (km) => {
      const { container } = renderWithProviders(<Streckenband analysis={analysisOfLength(km)} />);
      const ticks = tickValues(container);

      // Strictly increasing, so no label is drawn on top of another.
      expect(new Set(ticks).size).toBe(ticks.length);
      expect([...ticks].sort((a, b) => a - b)).toEqual(ticks);
      expect(ticks[0]).toBe(0);
      expect(ticks.at(-1)).toBe(Math.round(km));

      // The final stub may be shorter than the regular step, but not so short that its label
      // crowds its neighbour: a third of the widest gap is the component's own stated bound.
      const gaps = ticks.slice(1).map((value, index) => value - ticks[index]);
      expect(Math.min(...gaps)).toBeGreaterThanOrEqual(Math.max(...gaps) / 3);
    },
  );

  it("degenerates to a single tick for a zero-length route", () => {
    const { container } = renderWithProviders(<Streckenband analysis={analysisOfLength(0)} />);
    expect(tickValues(container)).toEqual([0]);
  });

  /**
   * FINDING (not fixed here — implementation is out of scope for this suite).
   *
   * `buildTicks` picks a step from [1, 2, 2.5, 5, 10] x 10^n, which drops below 1 km for any
   * route shorter than about 6 km, and then rounds every tick to a whole kilometre with
   * `Math.round`. On a 3 km route the step is 0.5 km, so the ticks collapse to
   * 0, 1, 1, 2, 2, 3 — two pairs of labels drawn exactly on top of each other, and two pairs
   * of duplicate React keys inside the axis group.
   *
   * The same rule that already drops a tick colliding with the end label needs to apply
   * between neighbours as well (or the tick values need a decimal when the step is sub-integer).
   * Reachable for any short corridor; the five demo routes are all long enough to hide it.
   */
  it("does not duplicate axis labels on a short route", () => {
    const { container } = renderWithProviders(<Streckenband analysis={analysisOfLength(3)} />);
    const ticks = tickValues(container);
    expect(new Set(ticks).size).toBe(ticks.length);
  });
});

describe("segment interaction", () => {
  it("shows the hint until a segment is chosen", () => {
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    expect(screen.getByText(de.routes.stripHint)).toBeInTheDocument();
  });

  it("reads out the focused segment's numbers", () => {
    // Focus, not just hover: the readout has to be reachable from the keyboard as well.
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    fireEvent.focusIn(segmentButtons()[2]);

    expect(screen.queryByText(de.routes.stripHint)).not.toBeInTheDocument();
    expect(screen.getByText(de.routes.predictedConsumption)).toBeInTheDocument();
    expect(screen.getByText("24,9 kWh/100 km")).toBeInTheDocument();
    expect(screen.getByText("46 %")).toBeInTheDocument();
    // Segment 3 runs from 100 to 150 km.
    expect(screen.getByText("100–150 km")).toBeInTheDocument();
  });

  it("shows a dash rather than a blank when a segment has no temperature", () => {
    // `temperature_c` is nullable in the database — DWD coverage is not universal.
    const analysis = makeAnalysis({
      segments: [
        makeSegment({
          ordinal: 0,
          start_offset_km: 0,
          distance_km: 200,
          kwh_per_100km: 20,
          soc_at_end_percent: 40,
          energy_intensity: "medium",
          temperature_c: null,
          traffic_severity: null,
        }),
      ],
    });
    renderWithProviders(<Streckenband analysis={analysis} />);
    fireEvent.focusIn(segmentButtons()[0]);

    expect(screen.getByText(de.live.outsideTemp)).toBeInTheDocument();
    // Two facts are unknown (temperature and traffic), and both must say so out loud.
    expect(screen.getAllByText("–")).toHaveLength(2);
  });

  it("clears the readout when focus leaves the band", () => {
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    const target = segmentButtons()[1];
    fireEvent.focusIn(target);
    expect(screen.queryByText(de.routes.stripHint)).not.toBeInTheDocument();

    fireEvent.focusOut(target);
    expect(screen.getByText(de.routes.stripHint)).toBeInTheDocument();
  });

  it("selects a segment on click", () => {
    const onSelectSegment = vi.fn();
    renderWithProviders(
      <Streckenband analysis={makeAnalysis()} onSelectSegment={onSelectSegment} />,
    );
    fireEvent.click(segmentButtons()[2]);
    expect(onSelectSegment).toHaveBeenCalledExactlyOnceWith(2);
  });

  it("toggles the already-selected segment off", () => {
    // Clicking the selected segment again must clear the selection (and the map's fly-to),
    // not re-select it — otherwise there is no way back to the whole-route view.
    const onSelectSegment = vi.fn();
    renderWithProviders(
      <Streckenband
        analysis={makeAnalysis()}
        selectedOrdinal={2}
        onSelectSegment={onSelectSegment}
      />,
    );
    fireEvent.click(segmentButtons()[2]);
    expect(onSelectSegment).toHaveBeenCalledExactlyOnceWith(null);
  });

  it.each<[string, string]>([
    ["Enter", "Enter"],
    ["Space", " "],
  ])("activates a segment with %s", (_label, key) => {
    const onSelectSegment = vi.fn();
    renderWithProviders(
      <Streckenband analysis={makeAnalysis()} onSelectSegment={onSelectSegment} />,
    );
    fireEvent.keyDown(segmentButtons()[1], { key });
    expect(onSelectSegment).toHaveBeenCalledExactlyOnceWith(1);
  });

  it("ignores other keys so arrow navigation and Escape still belong to the browser", () => {
    const onSelectSegment = vi.fn();
    renderWithProviders(
      <Streckenband analysis={makeAnalysis()} onSelectSegment={onSelectSegment} />,
    );
    fireEvent.keyDown(segmentButtons()[1], { key: "Escape" });
    fireEvent.keyDown(segmentButtons()[1], { key: "a" });
    expect(onSelectSegment).not.toHaveBeenCalled();
  });

  it("is inert, not broken, when no selection handler is supplied", () => {
    // The component is also used read-only on the print/summary view.
    renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    expect(() => fireEvent.click(segmentButtons()[0])).not.toThrow();
  });
});

describe("the traffic register", () => {
  /**
   * Events are projected onto the axis by nearest segment midpoint, with a 15 km cut-off.
   * One degree of latitude is ~111 km, so an offset of 10/111 degrees is 10 km (inside the
   * corridor) and 20/111 degrees is 20 km (outside it).
   */
  const MID_LAT = 50.1109;
  const MID_LON = 8.6821;

  function analysisWithEventsAt(offsets: number[]) {
    return makeAnalysis({
      distance_m: 200_000,
      segments: [
        makeSegment({
          ordinal: 0,
          start_offset_km: 0,
          distance_km: 50,
          kwh_per_100km: 17.2,
          soc_at_end_percent: 78,
          energy_intensity: "low",
        }),
        // Only this segment carries geometry, so the projection is unambiguous: its midpoint
        // is at 50 + 50/2 = 75 km along the route.
        makeSegment({
          ordinal: 1,
          start_offset_km: 50,
          distance_km: 50,
          kwh_per_100km: 21.4,
          soc_at_end_percent: 63,
          energy_intensity: "medium",
          geometry: lineThrough(MID_LAT, MID_LON),
        }),
      ],
      traffic_events: offsets.map((km, index) =>
        makeTrafficEvent({
          id: `event-${index}`,
          road_name: `A${index}`,
          title: `Störung ${index}`,
          latitude: MID_LAT + km / 111,
          longitude: MID_LON,
        }),
      ),
    });
  }

  function markerTitles(container: HTMLElement): string[] {
    return [...container.querySelectorAll("title")].map((node) => node.textContent ?? "");
  }

  it("shows an event inside the corridor and hides one outside it", () => {
    // 15 km is generous enough for a corridor match and tight enough to exclude an incident on
    // a parallel Autobahn that happens to share a bounding box.
    const { container } = renderWithProviders(
      <Streckenband analysis={analysisWithEventsAt([10, 20])} />,
    );
    const titles = markerTitles(container);

    expect(titles).toContain("A0 · Störung 0");
    expect(titles).not.toContain("A1 · Störung 1");
  });

  it("places the event at the offset of the segment it matched", () => {
    // The only segment with geometry spans 50–100 km, so its midpoint is 75 km, which at
    // 5 units/km is x = 375.
    const { container } = renderWithProviders(
      <Streckenband analysis={analysisWithEventsAt([10])} />,
    );
    const marker = [...container.querySelectorAll("title")].find(
      (node) => node.textContent === "A0 · Störung 0",
    );
    expect(marker?.parentElement?.getAttribute("transform")).toBe("translate(375, 0)");
  });

  it("drops every event when no segment carries geometry", () => {
    // Without geometry there is nothing to project against, and guessing a position would put
    // a roadworks marker on a stretch of road that has none.
    const analysis = makeAnalysis({
      traffic_events: [makeTrafficEvent({ road_name: "A5", title: "Baustelle" })],
    });
    const { container } = renderWithProviders(<Streckenband analysis={analysis} />);
    expect(markerTitles(container)).not.toContain("A5 · Baustelle");
  });

  it("renders an event with no road name without a stray separator", () => {
    const analysis = makeAnalysis({
      distance_m: 200_000,
      segments: [
        makeSegment({
          ordinal: 0,
          start_offset_km: 0,
          distance_km: 200,
          kwh_per_100km: 20,
          soc_at_end_percent: 40,
          energy_intensity: "medium",
          geometry: lineThrough(MID_LAT, MID_LON),
        }),
      ],
      traffic_events: [
        makeTrafficEvent({ road_name: null, title: "Unfall", latitude: MID_LAT, longitude: MID_LON }),
      ],
    });
    const { container } = renderWithProviders(<Streckenband analysis={analysis} />);
    expect(markerTitles(container)).toContain("Unfall");
  });
});

describe("the charging register", () => {
  it("places a stop at its distance along the axis and labels its power", () => {
    // A stop 100 km into a 200 km route sits at the midpoint: x = 500 of 1000.
    const { container } = renderWithProviders(
      <Streckenband analysis={makeAnalysis()} stops={[makeStop({ offset_km: 100 })]} />,
    );

    const label = [...container.querySelectorAll("text")].find(
      (node) => node.textContent === "300 kW",
    );
    expect(label).toBeDefined();
    expect(label?.parentElement?.getAttribute("transform")).toBe("translate(500, 0)");
  });

  it("rounds the power to whole kilowatts", () => {
    // "297,4 kW" is false precision on a tick label two millimetres wide.
    const { container } = renderWithProviders(
      <Streckenband analysis={makeAnalysis()} stops={[makeStop({ max_power_kw: 297.4 })]} />,
    );
    expect([...container.querySelectorAll("text")].map((n) => n.textContent)).toContain("297 kW");
  });

  it("renders no charging register when the route needs no stop", () => {
    const { container } = renderWithProviders(<Streckenband analysis={makeAnalysis()} />);
    expect([...container.querySelectorAll("text")].some((n) => /kW$/.test(n.textContent ?? ""))).toBe(
      false,
    );
  });
});
