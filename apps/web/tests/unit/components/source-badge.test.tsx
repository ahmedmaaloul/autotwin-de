import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SourceBadge, type DataOrigin } from "@/components/shared/source-badge";
import { de } from "@/lib/i18n/messages/de";
import { en } from "@/lib/i18n/messages/en";

import { renderWithProviders } from "../render";

/**
 * ADR 004 and BUILD_SPEC §0.2 as a test.
 *
 * "Real data and simulated data are never mixed invisibly." The badge is the mechanism that
 * makes that true on screen. If it ever stops rendering — a refactor that drops the label, a
 * catalogue key that goes missing, a tooltip wrapper that swallows its child — simulated
 * consumption figures start looking like measurements from the Bundesnetzagentur, and nothing
 * else in the UI would catch it.
 */

describe("SourceBadge", () => {
  it("shouts SIMULIERT for simulated data in German", () => {
    renderWithProviders(<SourceBadge origin="simulated" />);
    expect(screen.getByText("SIMULIERT")).toBeInTheDocument();
  });

  it("shouts SIMULATED for simulated data in English", () => {
    renderWithProviders(<SourceBadge origin="simulated" />, { locale: "en" });
    expect(screen.getByText("SIMULATED")).toBeInTheDocument();
  });

  it("keeps the label in upper case in both catalogues", () => {
    // Upper case is the point: it reads as a stamp, not as a caption. A translator who
    // sentence-cases it has weakened the honesty guarantee, so assert it at the source too.
    expect(de.provenance.simulated).toBe(de.provenance.simulated.toUpperCase());
    expect(en.provenance.simulated).toBe(en.provenance.simulated.toUpperCase());
  });

  it.each<[DataOrigin, string, string]>([
    ["official", de.provenance.official, en.provenance.official],
    ["simulated", de.provenance.simulated, en.provenance.simulated],
    ["derived", de.provenance.derived, en.provenance.derived],
  ])("renders the %s origin in both languages", (origin, german, english) => {
    const { unmount } = renderWithProviders(<SourceBadge origin={origin} />);
    expect(screen.getByText(german)).toBeInTheDocument();
    unmount();

    renderWithProviders(<SourceBadge origin={origin} />, { locale: "en" });
    expect(screen.getByText(english)).toBeInTheDocument();
  });

  it("styles simulated data differently from official data", () => {
    // Whatever the tokens are called, the two must not be visually interchangeable — a reader
    // scanning a table has to see the difference before reading the word.
    const official = renderWithProviders(<SourceBadge origin="official" />);
    const officialClass = screen.getByText(de.provenance.official).className;
    official.unmount();

    renderWithProviders(<SourceBadge origin="simulated" />);
    const simulatedClass = screen.getByText(de.provenance.simulated).className;

    expect(simulatedClass).not.toBe(officialClass);
  });

  it("names a known source in full rather than by its slug", () => {
    // "bundesnetzagentur" is a database value. A reader is owed the authority's actual name.
    renderWithProviders(<SourceBadge origin="official" source="bundesnetzagentur" />);
    expect(screen.getByText("Bundesnetzagentur")).toBeInTheDocument();
  });

  it.each<[string, string]>([
    ["dwd", "Deutscher Wetterdienst"],
    ["autobahn", "Autobahn GmbH"],
    ["osrm", "OSRM"],
    ["simulator", "AutoTwin Simulator"],
  ])("maps the %s source onto %s", (source, expected) => {
    renderWithProviders(<SourceBadge origin="official" source={source} />);
    expect(screen.getByText(expected)).toBeInTheDocument();
  });

  it("falls back to the raw source when it is not in the lookup", () => {
    // A new pipeline must show *something*, never an empty node beside the origin label.
    renderWithProviders(<SourceBadge origin="official" source="kraftfahrt-bundesamt" />);
    expect(screen.getByText("kraftfahrt-bundesamt")).toBeInTheDocument();
  });

  it("hides the source name in compact mode but never the origin", () => {
    // Compact is for dense table cells. The provenance label is what may not be dropped.
    renderWithProviders(<SourceBadge origin="simulated" source="simulator" compact />);
    expect(screen.getByText("SIMULIERT")).toBeInTheDocument();
    expect(screen.queryByText("AutoTwin Simulator")).not.toBeInTheDocument();
  });

  it("marks the icon decorative so the label is announced once", () => {
    const { container } = renderWithProviders(<SourceBadge origin="simulated" />);
    const icon = container.querySelector("svg");
    expect(icon).toHaveAttribute("aria-hidden");
  });
});
