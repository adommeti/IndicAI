import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { SESSION_ID, installApi } from "./fixtures";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHOTS = process.env.SHOTS_DIR
  ? path.resolve(process.env.SHOTS_DIR)
  : path.resolve(HERE, "../../../../docs/build/screenshots/uc1");

async function shot(page: import("@playwright/test").Page, name: string) {
  await page.screenshot({ path: path.join(SHOTS, name), fullPage: true, type: "jpeg", quality: 80 });
}

/** The browser smoke for uc1's primary flow.
 *
 *  Build the bundle with the e2e variables first, because VITE_* values are baked
 *  in at build time:  `npm run build -- --mode e2e && E2E=1 npm run e2e`.
 *
 *  NOT RUN during the build: this session has no browser, so nothing below has
 *  ever been executed and no assertion in it has ever passed. `E2E=1` on a machine
 *  with Chromium runs it.
 *
 *  It covers what the browser can be held to without a LiveKit server: sign-in,
 *  the chat turn, the Hindi script preference changing the language tag the agent
 *  is asked for and surviving a reload, and the replay endpoint's refusal being
 *  rendered as an answer rather than a blank page. The room's data messages are
 *  covered by the vitest reducer tests, because there is no honest way to fake an
 *  SFU data channel here. */
test.describe("helpdesk widget", () => {
  test.skip(process.env.E2E !== "1", "browser smoke: set E2E=1 to run it");
  test.beforeAll(() => mkdirSync(SHOTS, { recursive: true }));

  test("an employee asks in Hindi, switches to Latin script and is answered in it", async ({
    page,
  }) => {
    const fixture = await installApi(page, "none");
    await page.goto("/#/chat");

    await expect(page.getByTestId("identity")).toContainText("asha@example.com");
    await expect(page.getByTestId("roles")).toContainText("no role assigned");
    // A build served from 127.0.0.1 with VITE_AUTH_DEV_BYPASS=true says so, in
    // a banner that cannot be dismissed.
    await expect(page.getByTestId("dev-bypass")).toBeVisible();
    await expect(page.getByTestId("bypass-refused")).toHaveCount(0);
    await shot(page, "01-chat-empty.jpg");

    await page.getByTestId("utterance").fill("घर से वीपीएन कनेक्ट करने का तरीका बता दीजिए।");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("chat-agent")).toContainText("वीपीएन");
    await expect(page.getByTestId("chat-action")).toContainText("knowledge base");
    await expect(page.getByTestId("chat-citations")).toContainText("VPN-001");
    expect(fixture.chat[0]?.language).toBe("hi-IN");
    await shot(page, "02-chat-answer.jpg");

    // The script toggle is not a font switch: it changes the tag the agent is
    // asked to reply in, and the reply comes back romanised.
    await page.getByTestId("script-latn").click();
    await page.getByTestId("utterance").fill("ticket bana dijiye");
    await page.getByTestId("send").click();
    expect(fixture.chat[1]?.language).toBe("hi-Latn");
    await expect(page.getByTestId("chat-agent").last()).toContainText("portal kholiye");
    // A `file_ticket` decision whose filing has not come back says exactly that.
    await expect(page.getByTestId("ticket-pending")).toBeVisible();
    await shot(page, "03-latin-script.jpg");

    // Persisted per user, keyed by the employee_id /me reported.
    await page.reload();
    await expect(page.getByTestId("script-latn")).toHaveAttribute("aria-pressed", "true");

    // Telugu has one script, so the toggle is not offered for it.
    await page.getByTestId("language-te").click();
    await expect(page.getByTestId("script-toggle")).toHaveCount(0);
  });

  test("the voice panel never shows a live microphone it cannot back up", async ({ page }) => {
    await installApi(page, "none");
    await page.goto("/#/chat");

    // No token endpoint is configured in this build, so the local page offers the
    // documented hand-minted token path and no microphone control at all until a
    // room is joined and the pipeline has spoken.
    await expect(page.getByTestId("voice-panel")).toBeVisible();
    await expect(page.getByTestId("manual-join")).toBeVisible();
    await expect(page.getByTestId("mic")).toHaveCount(0);
    await expect(page.getByTestId("join")).toBeDisabled();
    await shot(page, "04-voice-not-connected.jpg");
  });

  test("replay is not reachable by hiding the tab: the service answers", async ({ page }) => {
    const fixture = await installApi(page, "none");
    await page.goto("/#/chat");
    await expect(page.getByTestId("nav-replay")).toHaveCount(0);

    // Deep-linked anyway. The page asks, the service refuses, and the refusal is
    // rendered as a role answer — not an outage, and not a blank screen.
    await page.goto(`/#/replay/${SESSION_ID}`);
    await expect(page.getByTestId("role-notice")).toBeVisible();
    await expect(page.getByRole("alert")).toHaveCount(0);
    expect(fixture.requested).toContain(`/sessions/${SESSION_ID}/replay`);
    await shot(page, "05-replay-refused.jpg");
  });

  test("governance replays the whole chain", async ({ page }) => {
    await installApi(page, "governance");
    await page.goto("/#/chat");
    await page.getByTestId("nav-replay").click();

    await page.getByTestId("session-id").fill(SESSION_ID);
    await page.getByTestId("open-replay").click();

    await expect(page.getByTestId("replay-session")).toContainText(SESSION_ID);
    await expect(page.getByTestId("replay-turn-0")).toContainText("वीपीएन");
    await expect(page.getByTestId("replay-turn-0").getByTestId("latency")).toContainText("900 ms");
    // No audio is kept at rest, and no Langfuse link is configured: both are said
    // once, in words, rather than rendered as a dead player and a dead link.
    await expect(page.getByTestId("no-audio")).toBeVisible();
    await expect(page.getByTestId("trace-link")).toHaveCount(0);
    await expect(page.getByTestId("replay-turn-0").getByTestId("trace-id")).toContainText(
      "0c9f1d2e3a4b5c6d", // pragma: allowlist secret
    );
    // Turn 2 had no voice leg: `voice.stt_ms` is absent, not zero.
    await expect(page.getByTestId("replay-turn-1").getByTestId("latency")).not.toContainText(
      "voice.stt_ms",
    );
    await expect(page.getByTestId("replay-turn-1")).toContainText("ZM-41822");
    await shot(page, "06-replay-chain.jpg");
  });

  test("keyboard alone gets from the composer to the reply", async ({ page }) => {
    await installApi(page, "none");
    await page.goto("/#/chat");
    await page.getByTestId("utterance").focus();
    await page.keyboard.type("VPN help please");
    // Enter sends; Shift+Enter would have inserted a newline.
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("chat-agent")).toBeVisible();
  });
});
