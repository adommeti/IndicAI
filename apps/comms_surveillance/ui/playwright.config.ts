import { defineConfig } from "@playwright/test";

/** E2E runs only with E2E=1 (.claude/rules/ui.md); `npm run e2e` drives the built
 *  bundle through `vite preview`.
 *
 *  By default the API is stubbed at the network boundary from the shapes in the
 *  pinned uc3/P6 contract, so the reviewer flow is exercised end to end in the
 *  browser without a database, MinIO or a vendor key. Point E2E_LIVE=1 at a
 *  running API (with AUTH__DEV_BYPASS=true) to run the same spec against it. */
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  fullyParallel: false,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE ?? "http://127.0.0.1:4174",
    viewport: { width: 1440, height: 900 },
    screenshot: "off",
    // The cloud image ships one Chromium build (PLAYWRIGHT_BROWSERS_PATH);
    // point at it rather than downloading a second copy per Playwright bump.
    launchOptions: process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : undefined,
  },
  webServer: {
    command: "npm run preview -- --port 4174 --strictPort",
    url: "http://127.0.0.1:4174",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
