import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DataModeNotice, EmptyState, ErrorState } from "@/components/shared/states";
import { ApiError } from "@/lib/api/client";
import { de } from "@/lib/i18n/messages/de";
import { en } from "@/lib/i18n/messages/en";

import { renderWithProviders } from "../render";

/**
 * "Degrade, never crash" (BUILD_SPEC §0.3) has a user-facing half: when something fails, the
 * reader must be told what failed and be given something to act on. The request id is the
 * whole point of `X-Request-Id` — it is what turns "it broke" into a line an engineer can
 * grep for in the API log.
 */

describe("ErrorState", () => {
  it("shows the request id carried by the error", () => {
    renderWithProviders(
      <ErrorState error={new ApiError(503, "provider_unavailable", "DWD unreachable", {}, "01JG7XABC")} />,
    );
    // Rendered verbatim and monospaced so it can be copied into a log query.
    expect(screen.getByText(/01JG7XABC/)).toBeInTheDocument();
  });

  it("omits the request id line entirely when the error has none", () => {
    // An empty "request-id:" label would look like a bug in the error reporting itself.
    renderWithProviders(<ErrorState error={new ApiError(500, "http_500", "boom")} />);
    expect(screen.queryByText(/request-id/)).not.toBeInTheDocument();
  });

  it("omits the request id for an error that is not an ApiError", () => {
    renderWithProviders(<ErrorState error={new Error("something went wrong")} />);
    expect(screen.queryByText(/request-id/)).not.toBeInTheDocument();
  });

  it("shows the backend's own message for a server-side failure", () => {
    // The API message names the source that failed ("DWD unreachable"); replacing it with a
    // generic string would throw away the only diagnostic the reader has.
    renderWithProviders(
      <ErrorState error={new ApiError(503, "provider_unavailable", "DWD open data unreachable")} />,
    );
    expect(screen.getByText("DWD open data unreachable")).toBeInTheDocument();
    expect(screen.getByText(de.states.errorTitle)).toBeInTheDocument();
  });

  it("falls back to the catalogue message for a non-ApiError", () => {
    // A raw Error's message is usually a stack-trace fragment, not something to show a user.
    renderWithProviders(<ErrorState error={new TypeError("x.y is not a function")} />);
    expect(screen.getByText(de.states.errorBody)).toBeInTheDocument();
    expect(screen.queryByText("x.y is not a function")).not.toBeInTheDocument();
  });

  it("distinguishes an unreachable backend from a backend that answered with an error", () => {
    // Status 0 means the request never arrived. "Prüfen Sie, ob der Backend-Dienst läuft" is
    // actionable; "500 Internal Server Error" would send the reader looking in the wrong place.
    const offline = renderWithProviders(
      <ErrorState error={new ApiError(0, "network_error", "Failed to fetch")} />,
    );
    expect(screen.getByText(de.states.offlineTitle)).toBeInTheDocument();
    expect(screen.getByText(de.states.offlineBody)).toBeInTheDocument();
    // The raw fetch message is suppressed in favour of the offline copy.
    expect(screen.queryByText("Failed to fetch")).not.toBeInTheDocument();
    offline.unmount();

    renderWithProviders(<ErrorState error={new ApiError(500, "http_500", "500 Internal Server Error")} />);
    expect(screen.getByText(de.states.errorTitle)).toBeInTheDocument();
  });

  it("translates the offline state", () => {
    renderWithProviders(<ErrorState error={new ApiError(0, "network_error", "x")} />, {
      locale: "en",
    });
    expect(screen.getByText(en.states.offlineTitle)).toBeInTheDocument();
  });

  it("offers a retry button only when the caller can retry", async () => {
    const onRetry = vi.fn();
    const withRetry = renderWithProviders(
      <ErrorState error={new ApiError(500, "http_500", "boom")} onRetry={onRetry} />,
    );
    await userEvent.click(screen.getByRole("button", { name: de.common.retry }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    withRetry.unmount();

    renderWithProviders(<ErrorState error={new ApiError(500, "http_500", "boom")} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("uses the catalogue copy by default", () => {
    renderWithProviders(<EmptyState />);
    expect(screen.getByText(de.states.emptyTitle)).toBeInTheDocument();
    expect(screen.getByText(de.states.emptyBody)).toBeInTheDocument();
  });

  it("lets a caller replace both strings", () => {
    renderWithProviders(<EmptyState title="Keine Ladestationen" description="Filter lockern" />);
    expect(screen.getByText("Keine Ladestationen")).toBeInTheDocument();
    expect(screen.queryByText(de.states.emptyTitle)).not.toBeInTheDocument();
  });
});

describe("DataModeNotice", () => {
  it.each<[string, "live" | "cache" | "fixture" | null | undefined]>([
    ["live", "live"],
    ["null", null],
    ["undefined", undefined],
  ])("renders nothing for %s", (_label, mode) => {
    // A banner on every healthy page would train the reader to ignore it.
    const { container } = renderWithProviders(<DataModeNotice mode={mode} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("distinguishes cached data from bundled demo data", () => {
    // ADR 005: the fallback chain is live -> cache -> fixture, and the reader is told which
    // rung they are on, because a cached reading and a demo reading are not equally trustworthy.
    const cached = renderWithProviders(<DataModeNotice mode="cache" />);
    expect(screen.getByText(de.provenance.degradedCache)).toBeInTheDocument();
    cached.unmount();

    renderWithProviders(<DataModeNotice mode="fixture" />);
    expect(screen.getByText(de.provenance.degradedFixture)).toBeInTheDocument();
  });
});
