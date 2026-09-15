import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const MODULE = "00000000-0000-4000-8000-000000000001";
const API = process.env.DEMO_API ?? "http://127.0.0.1:8000";
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = path.resolve(HERE, "../../../../docs/build/screenshots/uc2");

/** JPEG at q80: the prompt caps screenshots at 500 KB and a full-page PNG of a
 *  20-row table is over that. */
async function shot(page: import("@playwright/test").Page, name: string) {
  await page.screenshot({
    path: path.join(SHOTS, name),
    fullPage: true,
    type: "jpeg",
    quality: 80,
  });
}
const url = `/?module=${MODULE}`;
// The demo seeds sec-101 so that these locked segments keep raw machine text and
// therefore mismatch their approved rendering; the bulk action must skip exactly
// these and the reviewer must clear each one with a reason.
const EXPECTED_LOCKED_MISMATCHES = 3;
// Scoped to the table body: the header's "Approve all remaining" button shares
// the `approve-` test-id prefix.
const ROW_APPROVE = "tbody [data-testid^='approve-']";

test.beforeAll(() => mkdirSync(SHOTS, { recursive: true }));
test.beforeEach(async ({ request }) => {
  await request.post(`${API}/api/demo/reset`);
});

test("a reviewer approves 20 segments, and LOCKED mismatches are not bulk-approvable", async ({
  page,
}) => {
  await page.goto(url);
  await expect(page.getByRole("heading", { name: "Segment review" })).toBeVisible();
  await expect(page.locator("tbody tr")).toHaveCount(20);
  await shot(page, "01-review-table.jpg");

  // The flagged filter is how a reviewer triages; it must actually narrow.
  await page.getByTestId("filter-flagged").click();
  const flagged = await page.locator("tbody tr").count();
  expect(flagged).toBeGreaterThan(0);
  expect(flagged).toBeLessThan(20);
  await shot(page, "02-flagged-filter.jpg");
  await page.getByTestId("filter-all").click();

  // Time the whole review: bulk-approve the clean segments, then handle each
  // LOCKED mismatch by hand with an override reason.
  const started = Date.now();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByTestId("approve-all").click();

  // Wait for the bulk pass to settle completely before counting what it left:
  // counting mid-flight would pick up segments it simply had not reached yet.
  await expect
    .poll(async () => page.locator(ROW_APPROVE).count(), { timeout: 60_000 })
    .toBe(EXPECTED_LOCKED_MISMATCHES);

  // Whatever is left is a LOCKED mismatch: the bulk action refused it on purpose.
  const blocked = EXPECTED_LOCKED_MISMATCHES;

  for (let i = 0; i < blocked; i += 1) {
    const button = page.locator(ROW_APPROVE).first();
    await button.click();
    const dialog = page.getByRole("dialog", { name: /LOCKED/i });
    await expect(dialog).toBeVisible();
    if (i === 0) {
      await shot(page, "03-locked-override.jpg");
      // A token reason must not be accepted.
      await dialog.getByLabel("Why must this segment differ?").fill("ok");
      await expect(dialog.getByRole("button", { name: "Override and approve" })).toBeDisabled();
    }
    await dialog
      .getByLabel("Why must this segment differ?")
      .fill("Legal approved revised wording on 2026-09-15, ticket LEG-441.");
    await dialog.getByRole("button", { name: "Override and approve" }).click();
    await expect(dialog).toBeHidden();
  }

  const elapsedSeconds = (Date.now() - started) / 1000;
  await expect(page.locator(ROW_APPROVE)).toHaveCount(0);
  await shot(page, "04-all-approved.jpg");

  // uc2/P3 acceptance: a full 20-segment module inside ten minutes.
  // eslint-disable-next-line no-console
  console.log(`REVIEW_SECONDS=${elapsedSeconds.toFixed(1)} for 20 segments`);
  expect(elapsedSeconds).toBeLessThan(600);
});

test("the API refuses a LOCKED mismatch that the client tries to approve directly", async ({
  request,
}) => {
  const review = await (await request.get(`${API}/api/modules/${MODULE}/review?language=hi-IN`)).json();
  const mismatch = review.segments.find((s: { locked_mismatch: boolean }) => s.locked_mismatch);
  expect(mismatch, "the demo seeds at least one LOCKED mismatch").toBeTruthy();

  const refused = await request.put(
    `${API}/api/modules/${MODULE}/segments/${mismatch.seg_id}/approve`,
    { data: { language: "hi-IN", text: mismatch.translation } },
  );
  expect(refused.status()).toBe(409);
  expect((await refused.json()).detail.error).toBe("locked_override_required");

  const short = await request.put(
    `${API}/api/modules/${MODULE}/segments/${mismatch.seg_id}/approve`,
    { data: { language: "hi-IN", text: mismatch.translation, override_reason: "ok" } },
  );
  expect(short.status(), "a token reason is not an override").toBe(409);
});

test("quiz items can be approved", async ({ page }) => {
  await page.goto(url);
  await page.getByRole("button", { name: /^quiz/i }).click();
  const first = page.locator("[data-testid^='quiz-']").first();
  await expect(first).toBeVisible();
  // The checkbox is controlled: it flips only once the API confirms, so wait for
  // the state rather than for the click.
  await first.getByRole("checkbox").click();
  await expect(first.getByRole("checkbox")).toBeChecked({ timeout: 15_000 });
  await expect(page.getByRole("alert")).toHaveCount(0);
  await shot(page, "05-quiz-review.jpg");
});
