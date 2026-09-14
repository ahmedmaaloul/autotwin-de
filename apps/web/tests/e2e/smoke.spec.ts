import { expect, test, type Page } from "@playwright/test";

/**
 * Smoke tests for AutoTwin DE.
 *
 * These assert the property that matters most for a platform built on public open data:
 * **every screen renders something useful even when the backend is unavailable.** They are
 * therefore written to pass with or without the API running — a page that shows its error or
 * empty state is passing, a page that shows a blank column or an unhandled exception is not.
 *
 * Anything that needs live data belongs in a test tagged `@requires-api`, which the CI job
 * skips unless `PLAYWRIGHT_REQUIRE_API=1` is set.
 */

const REQUIRES_API = process.env.PLAYWRIGHT_REQUIRE_API === "1";

/** Fail the test on any uncaught page exception, not just on a missing element. */
async function trackPageErrors(page: Page): Promise<string[]> {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    // Failed API calls are expected when the backend is down — that is what the error states
    // are for. Everything else is a real defect.
    if (/Failed to load resource|net::ERR_|ECONNREFUSED|api\/backend/i.test(text)) return;
    errors.push(text);
  });
  return errors;
}

const PAGES = [
  { path: "/", heading: "Übersicht" },
  { path: "/live", heading: "Live Twin" },
  { path: "/routes", heading: "Routen" },
  { path: "/charging", heading: "Ladeinfrastruktur" },
  { path: "/analytics", heading: "Analysen" },
  { path: "/simulation", heading: "Simulation" },
  { path: "/ml", heading: "ML Lab" },
  { path: "/data-quality", heading: "Datenqualität" },
  { path: "/system", heading: "Systemstatus" },
] as const;

test.describe("every page renders", () => {
  for (const { path, heading } of PAGES) {
    test(`${path} shows its heading and no uncaught errors`, async ({ page }) => {
      const errors = await trackPageErrors(page);

      await page.goto(path);
      await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();

      // The shell must always be present.
      await expect(page.getByRole("navigation")).toBeVisible();
      await expect(page.getByRole("main")).toBeVisible();

      // Give client components a moment to settle into their loading/empty/error state.
      await page.waitForLoadState("networkidle").catch(() => undefined);

      // No blank page: main must contain real text, not just a spinner.
      const mainText = (await page.getByRole("main").innerText()).trim();
      expect(mainText.length).toBeGreaterThan(40);

      expect(errors, `uncaught errors on ${path}: ${errors.join(" | ")}`).toEqual([]);
    });
  }
});

test.describe("shell", () => {
  test("sidebar links to every section", async ({ page }) => {
    await page.goto("/");
    const nav = page.getByRole("navigation");
    for (const label of [
      "Übersicht",
      "Live Twin",
      "Routen",
      "Ladeinfrastruktur",
      "Analysen",
      "Simulation",
      "ML Lab",
      "Datenqualität",
    ]) {
      await expect(nav.getByRole("link", { name: label })).toBeVisible();
    }
  });

  test("language toggle switches the interface to English and persists", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Übersicht", level: 1 })).toBeVisible();

    await page.getByTestId("language-toggle").click();
    await page.getByRole("menuitem", { name: /English/ }).click();

    await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en-GB");

    // The choice is a cookie, so it must survive a reload and a navigation.
    await page.reload();
    await expect(page.getByRole("heading", { name: "Overview", level: 1 })).toBeVisible();
    await page.goto("/charging");
    await expect(page.getByRole("heading", { name: "Charging Infrastructure", level: 1 })).toBeVisible();
  });

  test("theme toggle switches to dark and back", async ({ page }) => {
    await page.goto("/");
    const html = page.locator("html");

    await page.getByRole("button", { name: /Erscheinungsbild|Appearance/ }).click();
    await page.getByRole("menuitem", { name: /Dunkel|Dark/ }).click();
    await expect(html).toHaveClass(/dark/);

    await page.getByRole("button", { name: /Erscheinungsbild|Appearance/ }).click();
    await page.getByRole("menuitem", { name: /Hell|Light/ }).click();
    await expect(html).not.toHaveClass(/dark/);
  });

  test("command palette opens with the keyboard and navigates", async ({ page }) => {
    await page.goto("/");
    await page.keyboard.press(process.platform === "darwin" ? "Meta+k" : "Control+k");
    await expect(page.getByRole("dialog")).toBeVisible();

    await page.getByRole("option", { name: /Ladeinfrastruktur/ }).first().click();
    await expect(page).toHaveURL(/\/charging/);
  });
});

test.describe("honesty", () => {
  test("the live page states that telemetry is simulated", async ({ page }) => {
    await page.goto("/live");
    // ADR 004: simulated telemetry must never be presentable as real fleet data.
    await expect(page.getByText(/SIMULIERTE FAHRZEUGTELEMETRIE/i)).toBeVisible();
  });

  test("the ML page states that the model was trained on simulated data", async ({ page }) => {
    await page.goto("/ml");
    await expect(page.getByText(/simulierter Telemetrie|simulated telemetry/i)).toBeVisible();
  });
});

test.describe("accessibility floor", () => {
  test("no horizontal overflow at 360 px", async ({ page }) => {
    await page.setViewportSize({ width: 360, height: 800 });
    for (const { path } of PAGES) {
      await page.goto(path);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, `horizontal overflow on ${path}`).toBeLessThanOrEqual(1);
    }
  });

  test("keyboard focus is visible on the first interactive element", async ({ page }) => {
    await page.goto("/");
    await page.keyboard.press("Tab");
    const focused = page.locator(":focus-visible");
    await expect(focused).toBeVisible();
  });
});

test.describe("route analysis @requires-api", () => {
  test.skip(!REQUIRES_API, "needs a running backend with demo data");

  test("Frankfurt am Main → Stuttgart produces a full analysis", async ({ page }) => {
    await page.goto("/routes");

    // The flagship corridor is pre-filled; the reader only has to press the button.
    await expect(page.getByRole("button", { name: /Route analysieren/ })).toBeEnabled();
    await page.getByRole("button", { name: /Route analysieren/ }).click();

    // The analysis calls OSRM and the ML model; allow real time for it.
    await expect(page.getByText(/Ladezustand bei Ankunft/)).toBeVisible({ timeout: 90_000 });

    // The Streckenband is the signature output and must be present and interactive.
    const strip = page.getByRole("img", { name: /Streckenband/ });
    await expect(strip).toBeVisible();

    // A plausible distance for the A5/A8 corridor.
    const body = await page.getByRole("main").innerText();
    expect(body).toMatch(/\b(19\d|20\d|21\d)\b/);
  });

  test("charging stations load and a station opens its detail panel", async ({ page }) => {
    await page.goto("/charging");
    await expect(page.getByRole("tab", { name: /Tabelle/ })).toBeVisible();
    await page.getByRole("tab", { name: /Tabelle/ }).click();
    await expect(page.getByRole("table")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByRole("row").nth(1)).toBeVisible();
  });

  test("simulation controls start and stop a run", async ({ page }) => {
    await page.goto("/simulation");
    const start = page.getByRole("button", { name: /^Starten$/ });
    await expect(start).toBeVisible();
    await start.click();
    await expect(page.getByText(/Läuft|Running/)).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: /^Stoppen$/ }).click();
  });
});
