import { defineConfig } from "@playwright/test";

/** E2E runs only with E2E=1 (.claude/rules/ui.md); `npm run e2e` drives the built
 *  bundle through `vite preview`.
 *
 *  The API and the LiveKit room are both stubbed at the network boundary from the
 *  shapes in the pinned uc1/P6 contract and from `voice_pipeline.py`'s published
 *  data messages, so the primary flow is exercised in a browser without a database,
 *  a LiveKit server or a vendor key. NOTHING here was executed during the build:
 *  this session has no browser (docs/build/BLOCKERS.md). */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE ?? "http://127.0.0.1:4175",
    viewport: { width: 1440, height: 900 },
    screenshot: "off",
    // The cloud image ships one Chromium build (PLAYWRIGHT_BROWSERS_PATH);
    // point at it rather than downloading a second copy per Playwright bump.
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : undefined,
  },
  webServer: {
    command: "npm run preview -- --port 4175 --strictPort",
    url: "http://127.0.0.1:4175",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
