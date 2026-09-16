import type { Page, Route } from "@playwright/test";

/** A stand-in for the helpdesk API, built from the shapes in the pinned uc1/P6
 *  contract (`GET /me`, `POST /chat/turn`, `GET /sessions/{id}/replay`).
 *
 *  It exists so the primary flow can be driven in a real browser without Postgres,
 *  Qdrant or a vendor key. It is NOT a security model: the role behaviour here only
 *  mirrors what `helpdesk_agent/roles.py` already enforces and what the API's own
 *  tests prove. Set E2E_LIVE=1 to skip all of it and drive a real API.
 *
 *  What it deliberately cannot stub is the LiveKit room: the data messages arrive
 *  over a WebRTC data channel from a real SFU, not over HTTP, so there is nothing
 *  here to intercept. Their handling is covered by the vitest reducer tests
 *  (`tests/messages.test.ts`, `tests/session.test.ts`), and this file does not
 *  pretend otherwise. */

export type FixtureRole = "governance" | "none";

export const SESSION_ID = "8e0b3a1e-0000-4000-8000-000000000000";

export interface ChatCall {
  utterance: string;
  language: string;
  session_id?: string;
}

export interface Fixture {
  /** Every chat turn the UI posted, in order — the assertion that the script
   *  toggle changes the language tag the agent is asked to reply in. */
  chat: ChatCall[];
  /** Paths the browser actually requested. */
  requested: string[];
}

const REPLIES: Record<string, string> = {
  "hi-IN": "वीपीएन से जुड़ने के लिए कंपनी पोर्टल खोलिए और 'Connect' दबाइए।",
  "hi-Latn": "VPN se judne ke liye company portal kholiye aur 'Connect' dabaiye.",
  "te-IN": "VPN కోసం కంపెనీ పోర్టల్ తెరిచి 'Connect' నొక్కండి.",
  "ta-IN": "VPN-க்கு நிறுவனத்தின் போர்ட்டலைத் திறந்து 'Connect' அழுத்தவும்.",
  "en-IN": "Open the company portal and press Connect to join the VPN.",
};

export async function installApi(page: Page, role: FixtureRole): Promise<Fixture> {
  const fixture: Fixture = { chat: [], requested: [] };
  const roles = role === "governance" ? ["governance"] : [];

  const json = (route: Route, body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  await page.route("**/me", (route) => {
    fixture.requested.push("/me");
    return json(route, { employee_id: "sso|asha@example.com", roles });
  });

  await page.route("**/chat/turn", async (route) => {
    fixture.requested.push("/chat/turn");
    const body = route.request().postDataJSON() as ChatCall;
    fixture.chat.push(body);
    const ticketTurn = fixture.chat.length > 1;
    return json(route, {
      session_id: SESSION_ID,
      decision: ticketTurn
        ? {
            action: "file_ticket",
            reply_text: REPLIES[body.language] ?? REPLIES["en-IN"],
            cited_article_ids: [],
            ticket: {
              title: "VPN client will not connect from home",
              description: "Employee reports the VPN client fails to connect from a home network.",
              category: "IT",
              urgency: "normal",
            },
          }
        : {
            action: "answer",
            reply_text: REPLIES[body.language] ?? REPLIES["en-IN"],
            cited_article_ids: ["VPN-001"],
            ticket: null,
          },
      // Null even on the ticket turn: the decision asked for a filing, the filing
      // has not come back, and the UI must not report it as filed.
      ticket_id: null,
    });
  });

  await page.route("**/sessions/*/replay", (route) => {
    fixture.requested.push(new URL(route.request().url()).pathname);
    if (role !== "governance")
      // The service answers 403 for a caller without the role — and for a session
      // id that does not exist too, so the endpoint is not an oracle for which
      // sessions are real. This stub mirrors that; it does not implement it.
      return json(route, { detail: "Replay requires an additional role" }, 403);
    return json(route, {
      session_id: SESSION_ID,
      employee_id: "sso|asha@example.com",
      created_at: "2026-09-15T09:12:00+00:00",
      turns: [
        {
          turn_index: 0,
          utterance: "घर से वीपीएन कनेक्ट करने का तरीका बता दीजिए।",
          language: "hi-IN",
          decision_json: {
            action: "answer",
            reply_text: REPLIES["hi-IN"],
            cited_article_ids: ["VPN-001"],
            ticket: null,
          },
          retrieval_json: { query: "vpn connect home", chunks: [{ article_id: "VPN-001", score: 0.71 }] },
          latency_ms: { retrieve: 11.0, decide: 900.0, "voice.stt_ms": 310.0 },
          model: "claude-sonnet-5",
          prompt_version: "9f2c4e1b7a02",
          policy_version: "2026-09-01",
          trace_id: "0c9f1d2e3a4b5c6d",
          // Always null in this build: LANGFUSE_PROJECT_ID is not configured
          // anywhere in the repo, so a link would 404 and the id is the handle.
          langfuse_url: null,
          // Always null: uc1 keeps no audio at rest.
          audio_url: null,
          ticket: null,
        },
        {
          turn_index: 1,
          utterance: "ठीक है, टिकट बना दीजिए।",
          language: "hi-IN",
          decision_json: {
            action: "file_ticket",
            reply_text: "आपका टिकट बना दिया गया है।",
            cited_article_ids: [],
            ticket: {
              title: "VPN client will not connect from home",
              description: "Employee reports the VPN client fails to connect from a home network.",
              category: "IT",
              urgency: "normal",
            },
          },
          retrieval_json: { query: "vpn ticket", chunks: [] },
          // No voice leg on this turn: `voice.stt_ms` is ABSENT rather than 0.
          latency_ms: { retrieve: 9.0, decide: 1450.0 },
          model: "claude-sonnet-5",
          prompt_version: "9f2c4e1b7a02",
          policy_version: "2026-09-01",
          trace_id: "1d8e2f3a4b5c6d7e",
          langfuse_url: null,
          audio_url: null,
          ticket: { status: "filed", ticket_number: "ZM-41822" },
        },
      ],
    });
  });

  return fixture;
}
