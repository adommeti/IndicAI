import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { installApi } from "./fixtures";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = process.env.SHOTS_DIR
  ? path.resolve(process.env.SHOTS_DIR)
  : path.resolve(HERE, "../../../../docs/build/screenshots/uc3");
const LIVE = process.env.E2E_LIVE === "1";

async function shot(page: import("@playwright/test").Page, name: string) {
  await page.screenshot({ path: path.join(SHOTS, name), fullPage: true, type: "jpeg", quality: 80 });
}

test.beforeAll(() => mkdirSync(SHOTS, { recursive: true }));

test("a reviewer triages the queue, sees the evidence in context and records a disposition", async ({
  page,
}) => {
  const fixture = LIVE ? null : await installApi(page, "compliance_reviewer");
  await page.goto("/#/queue");

  // The queue is severity-first: the top row is a HIGH flag.
  await expect(page.getByTestId("queue")).toBeVisible();
  const rows = page.locator("[data-flag]");
  await expect(rows.first()).toContainText("HIGH");
  await shot(page, "01-queue.jpg");

  // Keyboard is the reviewer's primary input: arrow down walks the queue.
  await rows.first().focus();
  await page.keyboard.press("ArrowDown");
  await expect(rows.nth(1)).toBeFocused();

  await rows.first().click();
  await expect(page.getByTestId("flag-detail")).toBeVisible();

  // The flagged span, the English rendering, the reasoning and the clause are all
  // on the page — a reviewer must not have to leave to find any of them.
  await expect(page.getByTestId("evidence-span")).not.toBeEmpty();
  await expect(page.getByTestId("english-rendering")).not.toBeEmpty();
  await expect(page.getByTestId("reasoning")).not.toBeEmpty();
  await expect(page.getByTestId("policy-clause")).not.toBeEmpty();

  // ...and the span is marked inside the transcript, not merely quoted beside it.
  await expect(page.locator("[data-testid^='segment-'] mark").first()).toBeVisible();
  await shot(page, "02-flag-detail.jpg");

  // Audio is only fetched when asked for, and lands on the flagged moment.
  await page.getByTestId("load-audio").click();
  const audio = page.getByTestId("audio");
  await expect(audio).toBeVisible();
  await page.getByTestId("seek-evidence").click();
  await expect
    .poll(async () => audio.evaluate((element: HTMLAudioElement) => element.currentTime))
    .toBeGreaterThan(0);

  // The ruling.
  await page.getByTestId("disposition-confirmed").check();
  await page.getByTestId("note").fill("Guaranteed return promised on a market-linked product; cash settlement requested.");
  await page.getByTestId("submit-disposition").click();

  // The receipt shows the chain sequence the server assigned, and the row joins
  // the history rather than replacing anything.
  await expect(page.getByTestId("receipt")).toContainText(/seq \d+/);
  await expect(page.getByTestId("history")).toContainText("Confirmed");
  await expect(page.getByTestId("current-disposition")).toContainText("Confirmed");
  await shot(page, "03-disposition-recorded.jpg");

  if (fixture) {
    expect(fixture.recorded).toHaveLength(1);
    expect(fixture.recorded[0]?.disposition).toBe("confirmed");
  }

  // A second, different ruling appends: nothing is edited away.
  await page.getByTestId("disposition-needs_more_context").check();
  await page.getByTestId("note").fill("Reopening: the client may already have been a qualified investor.");
  await page.getByTestId("submit-disposition").click();
  await expect(page.getByTestId("history").locator("li")).toHaveCount(2);
  await expect(page.getByTestId("history")).toContainText("superseded");
});

test("the script preference switches the transcript and survives a reload", async ({ page }) => {
  test.skip(LIVE, "needs the fixture transcript, which carries a known romanisation");
  await installApi(page, "compliance_reviewer");
  await page.goto("/#/queue/flag-h1");

  const line = page.getByTestId("segment-2");
  await expect(line).toContainText("गारंटीड");

  await page.getByTestId("script-latn").click();
  await expect(line).toContainText("guaranteed");
  // The Tamil line has no romanisation: it falls back and says so rather than
  // going blank.
  await expect(page.getByTestId("roman-fallback")).toBeVisible();
  await shot(page, "04-latin-script.jpg");

  await page.reload();
  await expect(page.getByTestId("segment-2")).toContainText("guaranteed");
});

test("a lead also gets the QA sample and precision over time", async ({ page }) => {
  await installApi(page, "compliance_lead");
  await page.goto("/#/queue");
  await expect(page.getByTestId("nav-queue")).toBeVisible();
  await expect(page.getByTestId("nav-qa")).toBeVisible();

  await page.getByTestId("nav-qa").click();
  await expect(page.getByRole("heading", { name: "Random QA sample" })).toBeVisible();
  await page.locator("[data-flag]").first().click();
  await expect(page.getByTestId("flag-detail")).toBeVisible();
  await shot(page, "05-lead-qa-sample.jpg");

  await page.getByTestId("nav-metrics").click();
  await expect(page.getByTestId("over-time")).toBeVisible();
  // A category with nothing decided reads "unmeasured", never 0.0%.
  await expect(page.getByTestId("precision-off_channel_contact")).toContainText("unmeasured");
  await shot(page, "06-lead-metrics.jpg");
});

test("governance gets metrics and chain status, and never asks for a transcript", async ({
  page,
}) => {
  const fixture = await installApi(page, "governance");
  await page.goto("/#/queue");

  // Deep-linked into the queue, a governance session is put on the view its role
  // actually has endpoints for instead of being left on a refusal.
  await expect(page.getByTestId("chain-verdict")).toBeVisible();
  await expect(page.getByTestId("governance-scope")).toBeVisible();
  await expect(page.getByTestId("nav-queue")).toHaveCount(0);
  await expect(page.getByTestId("nav-qa")).toHaveCount(0);
  await shot(page, "07-governance.jpg");

  // No transcript, no audio, anywhere on the page.
  await expect(page.locator("[data-testid^='segment-']")).toHaveCount(0);
  await expect(page.locator("audio")).toHaveCount(0);

  // And nothing was even requested. The service refuses these endpoints for this
  // role in any case — that is what the API role tests prove, and that, not this,
  // is the control. This assertion is about not generating pointless refusals.
  expect(fixture.requested.filter((path) => path.startsWith("/flags"))).toEqual([]);
  expect(fixture.requested).toContain("/audit/chain_status");
});

test("a refusal from the service reads as a role answer, not an outage", async ({ page }) => {
  // The roles say reviewer, but the service refuses the queue anyway — a role
  // changed mid-session, say. The server decides; the UI reports it plainly.
  await installApi(page, "compliance_reviewer");
  await page.route("**/flags**", (route) =>
    route.fulfill({ status: 403, contentType: "application/json", body: '{"detail":"forbidden"}' }),
  );
  await page.goto("/#/queue");

  await expect(page.getByRole("heading", { name: "Not available for your role" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await shot(page, "08-role-refusal.jpg");
});
