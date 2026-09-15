import { expect, test } from "@playwright/test";
import { installApi } from "../e2e/fixtures";

const OUT = "/tmp/claude-0/-home-user-IndicAI/ab52bcd3-aea1-5575-bdbe-333a14879685/scratchpad/uc3-shots";

test.use({ colorScheme: "dark" });

test("dark detail", async ({ page }) => {
  await installApi(page, "compliance_reviewer");
  await page.goto("/#/queue/flag-h1");
  await expect(page.getByTestId("flag-detail")).toBeVisible();
  await page.screenshot({ path: `${OUT}/dark-detail.jpg`, fullPage: true, type: "jpeg", quality: 80 });
});

test("dark metrics", async ({ page }) => {
  await installApi(page, "compliance_lead");
  await page.goto("/#/metrics");
  await expect(page.getByTestId("chain-verdict")).toBeVisible();
  await page.screenshot({ path: `${OUT}/dark-metrics.jpg`, fullPage: true, type: "jpeg", quality: 80 });
});

test("narrow queue", async ({ page }) => {
  await page.setViewportSize({ width: 400, height: 900 });
  await installApi(page, "compliance_reviewer");
  await page.goto("/#/queue/flag-h1");
  await expect(page.getByTestId("flag-detail")).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  await page.screenshot({ path: `${OUT}/narrow.jpg`, fullPage: true, type: "jpeg", quality: 80 });
});
