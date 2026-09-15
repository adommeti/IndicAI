import { defineConfig } from "@playwright/test";

/** E2E runs only with E2E=1 (see .claude/rules/ui.md). It drives the built UI
 *  against `training_localizer.demo`, which serves the real review assembly and
 *  the real LOCKED rule from memory. */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE ?? "http://127.0.0.1:4173",
    viewport: { width: 1440, height: 900 },
    screenshot: "off",
    // The cloud image ships one Chromium build (PLAYWRIGHT_BROWSERS_PATH);
    // point at it rather than downloading a second copy per Playwright bump.
    launchOptions: process.env.PW_CHROMIUM
      ? { executablePath: process.env.PW_CHROMIUM }
      : undefined,
  },
  webServer: {
    command: "npm run preview -- --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
