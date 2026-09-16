/** Shapes from the pinned uc1/P6 API contract and from the uc1 voice pipeline.
 *
 *  Nothing here is invented: every field appears either in the contract, in
 *  `apps/helpdesk_agent/api.py`, or in the data messages
 *  `apps/helpdesk_agent/voice_pipeline.py` publishes into the LiveKit room. */

/** uc1 recognises exactly one role (`helpdesk_agent/roles.py: KNOWN_ROLES`) and
 *  drops every other scope, so this is the whole vocabulary /me can return.
 *
 *  It is uc1-scoped and it means the OPPOSITE of uc3's role of the same name: in
 *  uc1 `governance` GRANTS the full session replay, in uc3 it SUBTRACTS content
 *  access. Read `helpdesk_agent/roles.py` before reusing the word, and never
 *  import uc3's role handling here. */
export type Role = "governance";

export interface Me {
  /** Issuer-qualified subject from the SSO claim — not a display name. */
  employee_id: string;
  /** The recognised subset of the claim's scopes. May be empty, and an empty
   *  list is an answer ("you hold no role here"), not a failure. */
  roles: Role[];
}

/** The five language tags `helpdesk_agent.graph` accepts. `hi-Latn` is Hindi
 *  written in Latin script: the same language, a different script, and the graph
 *  replies in whichever of the two was asked for. */
export type LanguageTag = "hi-IN" | "hi-Latn" | "te-IN" | "ta-IN" | "en-IN";

export type Action = "answer" | "clarify" | "file_ticket";

export interface Ticket {
  title: string;
  description: string;
  category: "IT" | "HR" | "Facilities";
  urgency: "low" | "normal" | "high";
}

export interface Decision {
  action: Action;
  reply_text: string;
  cited_article_ids: string[];
  ticket: Ticket | null;
}

export interface ChatTurnResponse {
  session_id: string;
  decision: Decision;
  /** Null whenever no Zammad ticket was filed — including for a `file_ticket`
   *  decision whose filing is still queued. Null is not "no ticket wanted". */
  ticket_id: string | null;
}

/** One turn of `GET /sessions/{id}/replay`. Everything the chain carries. */
export interface ReplayTurn {
  turn_index: number;
  utterance: string;
  language: string;
  decision_json: Record<string, unknown> | null;
  retrieval_json: Record<string, unknown> | null;
  /** Stage timings in milliseconds. A stage that did not run is absent, never
   *  zero: `voice.stt_ms` missing means there was no voice leg, and rendering it
   *  as 0 ms would report a measurement nobody took. */
  latency_ms: Record<string, number> | null;
  model: string | null;
  prompt_version: string | null;
  policy_version: string | null;
  trace_id: string | null;
  /** Deep link to the Langfuse trace, or null when the deployment has not
   *  configured one. A null here means "no link", not "no trace": `trace_id` is
   *  still the handle an auditor takes to Langfuse. */
  langfuse_url: string | null;
  /** ALWAYS null in this build. uc1 keeps no audio at rest — the voice pipeline
   *  streams audio to the vendor and to the room and stores none of it — so there
   *  is nothing for a URL to point at. The UI says that in those words rather
   *  than rendering a dead player. */
  audio_url: string | null;
  ticket: { status: string; ticket_number: string | null } | null;
}

export interface Replay {
  session_id: string;
  employee_id: string;
  created_at: string;
  turns: ReplayTurn[];
}
