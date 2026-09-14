import { mkdir } from "node:fs/promises";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

/**
 * Capture the screenshots used in the README and the docs.
 *
 * This is a test rather than a script on purpose: it runs the same flows the smoke tests run,
 * so a screenshot can never show a screen that does not actually work. Run it with
 * `PLAYWRIGHT_CAPTURE=1 pnpm e2e screenshots` against a backend with demo data loaded.
 */

const CAPTURE = process.env.PLAYWRIGHT_CAPTURE === "1";
const OUT = path.resolve(process.cwd(), "../../docs/images");

test.describe("screenshots", () => {
  test.skip(!CAPTURE, "set PLAYWRIGHT_CAPTURE=1 to regenerate documentation screenshots");
  test.use({ viewport: { width: 1680, height: 1050 }, deviceScaleFactor: 2 });

  test.beforeAll(async () => {
    await mkdir(OUT, { recursive: true });
  });

  /** MapLibre needs its tiles and the SSE stream needs a tick before anything is worth shooting. */
  async function settle(page: Page, ms = 6000): Promise<void> {
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await page.waitForTimeout(ms);
  }

  test("dashboard", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Übersicht", level: 1 })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "dashboard.png"), fullPage: false });
  });

  test("route analysis", async ({ page }) => {
    await page.goto("/routes");
    await page.getByRole("button", { name: "Route analysieren" }).first().click();

    // The analysis calls the routing provider, PostGIS and the ML model; give it real time.
    await expect(page.getByText(/LADEZUSTAND BEI ANKUNFT/i)).toBeVisible({ timeout: 90_000 });
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "route-analysis.png") });

    // The Streckenband is the signature component — capture it on its own too.
    const strip = page.getByRole("img", { name: /Streckenband/i }).first();
    await strip.scrollIntoViewIfNeeded();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: path.join(OUT, "streckenband.png") });
  });

  test("charging infrastructure", async ({ page }) => {
    await page.goto("/charging");
    await expect(page.getByRole("heading", { name: "Ladeinfrastruktur", level: 1 })).toBeVisible();
    await settle(page, 9000);
    await page.screenshot({ path: path.join(OUT, "charging.png") });
  });

  test("live digital twin", async ({ page }) => {
    await page.goto("/live");
    await expect(page.getByText(/SIMULIERTE FAHRZEUGTELEMETRIE — keine realen/)).toBeVisible();
    await settle(page, 9000);
    await page.screenshot({ path: path.join(OUT, "live-twin.png") });
  });

  test("data quality", async ({ page }) => {
    await page.goto("/data-quality");
    await expect(page.getByRole("heading", { name: "Datenqualität", level: 1 })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "data-quality.png") });
  });

  test("ml lab", async ({ page }) => {
    await page.goto("/ml");
    await expect(page.getByRole("heading", { name: "ML Lab", level: 1 })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "ml-lab.png") });
  });

  test("analytics", async ({ page }) => {
    await page.goto("/analytics");
    await expect(page.getByRole("heading", { name: "Analysen", level: 1 })).toBeVisible();
    await settle(page, 9000);
    await page.screenshot({ path: path.join(OUT, "analytics.png") });
  });

  test("dashboard in dark mode", async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Übersicht", level: 1 })).toBeVisible();
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "dashboard-dark.png") });
  });
});
