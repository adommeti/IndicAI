import type { Page, Route } from "@playwright/test";

/** A stand-in for the API, built from the shapes in the pinned uc3/P6 contract.
 *
 *  It exists so the reviewer flow can be driven in a real browser without a
 *  database, MinIO or a vendor key. It is NOT a security model: role behaviour
 *  here only mirrors what the service already enforces and what the API role
 *  tests prove. Set E2E_LIVE=1 to skip all of this and drive a real API. */

export type FixtureRole = "compliance_reviewer" | "compliance_lead" | "governance";

const HINDI_TRANSCRIPT = [
  {
    seg_id: 1,
    speaker: "agent",
    start_ms: 0,
    end_ms: 6_000,
    text: "नमस्ते सर, आपका पोर्टफोलियो देख रहा था।",
    text_roman: "namaste sir, aapka portfolio dekh raha tha.",
  },
  {
    seg_id: 2,
    speaker: "agent",
    start_ms: 6_000,
    end_ms: 14_000,
    text: "मैं आपको गारंटीड रिटर्न दे सकता हूँ, बस नकद में दीजिए।",
    text_roman: "main aapko guaranteed return de sakta hoon, bas nakad mein dijiye.",
  },
  {
    seg_id: 3,
    speaker: "client",
    start_ms: 14_000,
    end_ms: 19_000,
    text: "சரி, நான் யோசிக்கிறேன்.",
    text_roman: null,
  },
];

const FLAGS = [
  {
    flag_id: "flag-h1",
    call_id: "call-2026-0912-0001",
    category: "guaranteed_returns",
    severity: "high" as const,
    speaker: "agent",
    start_ms: 6_000,
    created_at: "2026-09-12T09:00:00Z",
    disposition: null,
  },
  {
    flag_id: "flag-h2",
    call_id: "call-2026-0912-0007",
    category: "undisclosed_commission",
    severity: "high" as const,
    speaker: "agent",
    start_ms: 41_000,
    created_at: "2026-09-13T09:00:00Z",
    disposition: null,
  },
  {
    flag_id: "flag-m1",
    call_id: "call-2026-0911-0004",
    category: "pressure_selling",
    severity: "medium" as const,
    speaker: "agent",
    start_ms: 12_500,
    created_at: "2026-09-11T09:00:00Z",
    disposition: "needs_more_context" as const,
  },
  {
    flag_id: "flag-l1",
    call_id: "call-2026-0910-0002",
    category: "off_channel_contact",
    severity: "low" as const,
    speaker: "client",
    start_ms: 88_000,
    created_at: "2026-09-10T09:00:00Z",
    disposition: "false_positive" as const,
  },
];

/** 0.4s of silence: enough for the player to report metadata and accept a seek. */
function silentWav(): string {
  const sampleRate = 8000;
  const samples = sampleRate * 0.4;
  const bytes = new Uint8Array(44 + samples);
  const view = new DataView(bytes.buffer);
  const ascii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };
  ascii(0, "RIFF");
  view.setUint32(4, 36 + samples, true);
  ascii(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate, true);
  view.setUint16(32, 1, true);
  view.setUint16(34, 8, true);
  ascii(36, "data");
  view.setUint32(40, samples, true);
  bytes.fill(128, 44);
  return `data:audio/wav;base64,${Buffer.from(bytes).toString("base64")}`;
}

export interface Recorded {
  disposition: string;
  note: string;
}

export interface Fixture {
  /** Every disposition the UI posted, in order. */
  recorded: Recorded[];
  /** Endpoints the browser actually requested — the assertion that a governance
   *  session never even asks for a transcript. */
  requested: string[];
}

export async function installApi(page: Page, role: FixtureRole): Promise<Fixture> {
  const fixture: Fixture = { recorded: [], requested: [] };
  const flags = FLAGS.map((flag) => ({ ...flag }));
  const history: Record<string, Recorded[]> = {};
  let seq = 41;

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  const forbidden = (route: Route) =>
    json(route, { detail: `role ${role} may not access this resource` }, 403);

  const canReview = role !== "governance";

  await page.route("**/me", (route) =>
    json(route, { identity: "asha.reviewer@example.com", roles: [role], dev_bypass: true }),
  );

  await page.route("**/flags**", async (route) => {
    const url = new URL(route.request().url());
    fixture.requested.push(url.pathname);
    if (!canReview) return forbidden(route);

    const detail = url.pathname.match(/\/flags\/([^/]+)$/);
    const audio = url.pathname.match(/\/flags\/([^/]+)\/audio$/);
    const post = url.pathname.match(/\/flags\/([^/]+)\/dispositions$/);

    if (post && route.request().method() === "POST") {
      const body = route.request().postDataJSON() as Recorded;
      fixture.recorded.push(body);
      const id = post[1] ?? "";
      history[id] = [body, ...(history[id] ?? [])];
      const flag = flags.find((item) => item.flag_id === id);
      if (flag) flag.disposition = body.disposition as typeof flag.disposition;
      seq += 1;
      return json(route, { disposition_id: `d-${seq}`, seq, row_hash: `9f${seq}c4e1b7a02d`.padEnd(64, "0") }, 201);
    }

    if (audio) return json(route, { url: silentWav(), expires_in_s: 300, start_ms: 6_000 });

    if (detail) {
      const id = detail[1] ?? "";
      const flag = flags.find((item) => item.flag_id === id) ?? flags[0]!;
      return json(route, {
        ...flag,
        evidence_span: "गारंटीड रिटर्न",
        english_rendering: "I can give you a guaranteed return, just pay in cash.",
        reasoning:
          "The agent promises a guaranteed return on a market-linked product and asks for cash settlement.",
        policy_clause:
          "Policy 4.2 — no representative may promise or imply an assured return on a market-linked product.",
        transcript: HINDI_TRANSCRIPT,
        dispositions: history[id] ?? [],
      });
    }

    const severity = url.searchParams.get("severity");
    const open = url.searchParams.get("undispositioned") === "true";
    return json(route, {
      flags: flags.filter(
        (flag) =>
          (!severity || flag.severity === severity) && (!open || flag.disposition === null),
      ),
    });
  });

  await page.route("**/qa-sample", (route) => {
    fixture.requested.push(new URL(route.request().url()).pathname);
    if (role !== "compliance_lead") return forbidden(route);
    return json(route, { items: flags.slice(0, 2).map((flag) => ({ ...flag, qa_sampled: true })) });
  });

  await page.route("**/metrics/precision", (route) => {
    fixture.requested.push("/metrics/precision");
    return json(route, {
      by_category: [
        { category: "guaranteed_returns", confirmed: 18, false_positive: 3, decided: 21, precision: 18 / 21 },
        { category: "pressure_selling", confirmed: 4, false_positive: 6, decided: 10, precision: 0.4 },
        // Nothing decided: the UI must print "unmeasured", never 0.0.
        { category: "off_channel_contact", confirmed: 0, false_positive: 0, decided: 0, precision: null },
      ],
      over_time: [
        { bucket: "2026-W35", category: "guaranteed_returns", confirmed: 5, false_positive: 2, decided: 7, precision: 5 / 7 },
        { bucket: "2026-W36", category: "guaranteed_returns", confirmed: 6, false_positive: 1, decided: 7, precision: 6 / 7 },
        { bucket: "2026-W37", category: "guaranteed_returns", confirmed: 7, false_positive: 0, decided: 7, precision: 1 },
        { bucket: "2026-W35", category: "pressure_selling", confirmed: 2, false_positive: 3, decided: 5, precision: 0.4 },
        // W36 missing on purpose: the line must break, not interpolate.
        { bucket: "2026-W37", category: "pressure_selling", confirmed: 2, false_positive: 3, decided: 5, precision: 0.4 },
      ],
      unmeasured: ["off_channel_contact"],
    });
  });

  await page.route("**/metrics/false_negative_estimate", (route) => {
    fixture.requested.push("/metrics/false_negative_estimate");
    return json(route, { sampled: 120, missed: 4, rate: 4 / 120, unmeasured: false });
  });

  await page.route("**/audit/chain_status", (route) => {
    fixture.requested.push("/audit/chain_status");
    return json(route, {
      tables: [
        { table: "flags", rows: 4210, ok: true, anchor_ok: true, reason: null },
        { table: "dispositions", rows: 1877, ok: true, anchor_ok: true, reason: null },
        { table: "calls", rows: 903, ok: true, anchor_ok: true, reason: null },
      ],
      breaks: 0,
      ok: true,
      checked_at: "2026-09-15T18:30:00Z",
    });
  });

  return fixture;
}
