import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MetricCard } from "@/components/shared/metric-card";

import { renderWithProviders } from "../render";

/**
 * The metric tile is the densest reading surface in the product, and its one structural
 * promise is that the value and its unit are separate nodes: that is what lets a column of
 * tiles align on the decimal point instead of on the end of "kWh/100 km".
 */

describe("MetricCard", () => {
  it("renders the value and the unit as separate nodes", () => {
    renderWithProviders(
      <MetricCard label="Ø Energieverbrauch" value="18,7" unit="kWh/100 km" />,
    );

    const value = screen.getByText("18,7");
    const unit = screen.getByText("kWh/100 km");

    expect(value).toHaveAttribute("data-slot", "metric-value");
    expect(unit).not.toBe(value);
    // If the unit were inside the value node, the two would share a font and a baseline box
    // and the column alignment would be gone.
    expect(value).not.toContainElement(unit);
    expect(value.textContent).toBe("18,7");
  });

  it("renders no unit node when the metric is dimensionless", () => {
    const { container } = renderWithProviders(<MetricCard label="Fahrzeuge" value="312" />);
    const value = container.querySelector('[data-slot="metric-value"]');
    expect(value?.parentElement?.childElementCount).toBe(1);
  });

  it("shows a skeleton instead of a stale value while loading", () => {
    renderWithProviders(<MetricCard label="Ladestationen" value="116.440" loading />);
    // Rendering the previous value during a refetch would present old data as current.
    expect(screen.queryByText("116.440")).not.toBeInTheDocument();
  });

  it("renders the label, and the footer when one is supplied", () => {
    renderWithProviders(
      <MetricCard label="Ladestationen" value="116.440" footer="Stand: 14.09.2026" />,
    );
    expect(screen.getByText("Ladestationen")).toBeInTheDocument();
    expect(screen.getByText("Stand: 14.09.2026")).toBeInTheDocument();
  });

  describe("delta", () => {
    function deltaNode(): HTMLElement {
      const value = document.querySelector('[data-slot="metric-value"]');
      const node = value?.parentElement?.lastElementChild;
      if (!(node instanceof HTMLElement)) throw new Error("no delta node rendered");
      return node;
    }

    it("prefixes a rise with an explicit plus sign", () => {
      // Without the "+", "5.7 %" reads as an absolute share rather than a change.
      renderWithProviders(<MetricCard label="x" value="1" delta={5.7} />);
      expect(deltaNode().textContent).toContain("+");
    });

    it("does not claim a rise when the change is exactly zero", () => {
      renderWithProviders(<MetricCard label="x" value="1" delta={0} />);
      expect(deltaNode().textContent).not.toContain("+");
      // German decimal comma, like every other number in the product.
      expect(deltaNode().textContent).toContain("0,0");
    });

    it("reads the same sign as good or bad depending on the metric", () => {
      // Consumption rising is bad; charger count rising is good. The sign alone cannot decide,
      // so +5 with positiveIsGood=false must be styled like -5 with positiveIsGood=true.
      const risingGood = renderWithProviders(
        <MetricCard label="x" value="1" delta={5} positiveIsGood />,
      );
      const risingGoodClass = deltaNode().className;
      risingGood.unmount();

      const risingBad = renderWithProviders(
        <MetricCard label="x" value="1" delta={5} positiveIsGood={false} />,
      );
      const risingBadClass = deltaNode().className;
      risingBad.unmount();

      const fallingBad = renderWithProviders(
        <MetricCard label="x" value="1" delta={-5} positiveIsGood />,
      );
      const fallingBadClass = deltaNode().className;
      fallingBad.unmount();

      expect(risingGoodClass).not.toBe(risingBadClass);
      expect(risingBadClass).toBe(fallingBadClass);
    });

    it("treats a zero change as the good side of the threshold", () => {
      // `delta >= 0 === positiveIsGood` — exactly on the boundary, no change is not a regression.
      const zero = renderWithProviders(<MetricCard label="x" value="1" delta={0} positiveIsGood />);
      const zeroClass = deltaNode().className;
      zero.unmount();

      renderWithProviders(<MetricCard label="x" value="1" delta={1} positiveIsGood />);
      expect(deltaNode().className).toBe(zeroClass);
    });

    it("points the arrow down for a fall and up for a rise", () => {
      const falling = renderWithProviders(<MetricCard label="x" value="1" delta={-5} />);
      expect(document.querySelector(".lucide-trending-down")).not.toBeNull();
      falling.unmount();

      renderWithProviders(<MetricCard label="x" value="1" delta={5} />);
      expect(document.querySelector(".lucide-trending-up")).not.toBeNull();
    });

    it.each<[string, number | null | undefined]>([
      ["null", null],
      ["undefined", undefined],
      ["NaN", Number.NaN],
      ["Infinity", Number.POSITIVE_INFINITY],
    ])("renders no delta at all for %s", (_label, delta) => {
      // A delta computed from a zero baseline is Infinity; "Infinity %" must never ship.
      const { container } = renderWithProviders(
        <MetricCard label="x" value="1" delta={delta} />,
      );
      expect(container.textContent).not.toContain("NaN");
      expect(container.textContent).not.toContain("Infinity");
      expect(container.textContent).not.toContain("%");
    });

    /**
     * REGRESSION. The delta used `toFixed(1)`, which always emits a dot, so a German tile
     * read "18,7" for its value and "+5.7 %" for its delta — two conventions in one card.
     */
    it("formats the delta with the German decimal comma", () => {
      renderWithProviders(<MetricCard label="x" value="1" delta={5.7} />);
      expect(deltaNode().textContent).toContain("+5,7");
    });
  });
});
