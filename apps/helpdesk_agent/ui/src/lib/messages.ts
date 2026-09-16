/** The room's data channel, parsed strictly.
 *
 *  `apps/helpdesk_agent/voice_pipeline.py` is the producer and it is committed;
 *  these are its shapes, and it stamps every payload with a schema version:
 *
 *      {"v":1,"type":"transcript","final":false,"text":...,"language":...,"turn":n}
 *      {"v":1,"type":"decision","action":"answer"|"clarify"|"file_ticket","turn":n}
 *      {"v":1,"type":"notice","state":"playing"|"unconfirmed"}
 *      {"v":1,"type":"mode","mode":"chat","reason":"stt_unavailable"}
 *      {"v":1,"type":"error","stage":"decide","recoverable":true}
 *
 *  ## Why this refuses instead of coping
 *
 *  The pipeline's own comment says the version exists "so a UI can refuse a payload
 *  shape it does not know instead of silently rendering half of it". Best-effort
 *  rendering of an unknown version is how a half-rendered transcript reaches an
 *  employee: a v2 that renamed `final`, or moved the text, produces a caption that
 *  looks finished and is not — or a consent notice whose state this client cannot
 *  read. Both are worse than a client that stops and says it is out of date.
 *
 *  So: an unknown `v` is refused whole, an unknown `type` is refused, and a payload
 *  of the right version whose fields are the wrong types is refused. Nothing is
 *  coerced, defaulted or half-read. Every refusal carries a reason, and the caller
 *  surfaces it rather than dropping it on the floor.
 */

/** The only schema version this client can render. Must match
 *  `voice_pipeline.DATA_MESSAGE_VERSION`. */
export const SUPPORTED_DATA_VERSION = 1;

export type NoticeState = "playing" | "unconfirmed";
export type DecisionAction = "answer" | "clarify" | "file_ticket";

export interface TranscriptMessage {
  type: "transcript";
  final: boolean;
  text: string;
  /** What Saaras identified for this segment. Null when it identified nothing —
   *  STT runs on `auto`, so the language is a result, not a setting. */
  language: string | null;
  turn: number;
}

export interface DecisionMessage {
  type: "decision";
  /** Null when the graph returned no action. The pipeline forwards
   *  `decision.get("action")` verbatim, so null is reachable and means "the graph
   *  did not say", not "no decision". */
  action: DecisionAction | null;
  turn: number;
}

export interface NoticeMessage {
  type: "notice";
  state: NoticeState;
}

export interface ModeMessage {
  type: "mode";
  mode: "chat";
  reason: string;
}

export interface ErrorMessage {
  type: "error";
  stage: string;
  recoverable: boolean;
}

export type RoomMessage =
  | TranscriptMessage
  | DecisionMessage
  | NoticeMessage
  | ModeMessage
  | ErrorMessage;

export type RefusalReason =
  /** Not JSON, or not a JSON object. */
  | "malformed"
  /** A schema version this client does not implement. */
  | "unsupported-version"
  /** Version 1, but a `type` this client has no renderer for. */
  | "unknown-type"
  /** Version 1 and a known type, but the fields are not what that type means. */
  | "invalid-payload";

export type ParsedMessage =
  | { ok: true; message: RoomMessage }
  | { ok: false; reason: RefusalReason; detail: string; version: number | null };

const decoder = new TextDecoder();

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function refuse(reason: RefusalReason, detail: string, version: number | null): ParsedMessage {
  return { ok: false, reason, detail, version };
}

function parseTranscript(raw: Record<string, unknown>): ParsedMessage {
  const { final, text, language, turn } = raw;
  if (typeof final !== "boolean")
    return refuse("invalid-payload", "transcript.final is not a boolean", SUPPORTED_DATA_VERSION);
  if (typeof text !== "string")
    return refuse("invalid-payload", "transcript.text is not a string", SUPPORTED_DATA_VERSION);
  if (typeof turn !== "number" || !Number.isFinite(turn))
    return refuse("invalid-payload", "transcript.turn is not a number", SUPPORTED_DATA_VERSION);
  if (language !== null && language !== undefined && typeof language !== "string")
    return refuse("invalid-payload", "transcript.language is not a string", SUPPORTED_DATA_VERSION);
  return {
    ok: true,
    message: { type: "transcript", final, text, language: language ?? null, turn },
  };
}

const ACTIONS = new Set<string>(["answer", "clarify", "file_ticket"]);

function parseDecision(raw: Record<string, unknown>): ParsedMessage {
  const { action, turn } = raw;
  if (typeof turn !== "number" || !Number.isFinite(turn))
    return refuse("invalid-payload", "decision.turn is not a number", SUPPORTED_DATA_VERSION);
  if (action !== null && action !== undefined && !ACTIONS.has(String(action)))
    return refuse(
      "invalid-payload",
      `decision.action ${JSON.stringify(action)} is not one of answer, clarify, file_ticket`,
      SUPPORTED_DATA_VERSION,
    );
  return {
    ok: true,
    message: {
      type: "decision",
      action: action === null || action === undefined ? null : (action as DecisionAction),
      turn,
    },
  };
}

function parseNotice(raw: Record<string, unknown>): ParsedMessage {
  const { state } = raw;
  // A notice state this client cannot read is the one refusal that matters most:
  // it is the consent control, and guessing at it is not an option.
  if (state !== "playing" && state !== "unconfirmed")
    return refuse(
      "invalid-payload",
      `notice.state ${JSON.stringify(state)} is not one of playing, unconfirmed`,
      SUPPORTED_DATA_VERSION,
    );
  return { ok: true, message: { type: "notice", state } };
}

function parseMode(raw: Record<string, unknown>): ParsedMessage {
  const { mode, reason } = raw;
  if (mode !== "chat")
    return refuse(
      "invalid-payload",
      `mode.mode ${JSON.stringify(mode)} is not "chat"`,
      SUPPORTED_DATA_VERSION,
    );
  return {
    ok: true,
    message: { type: "mode", mode: "chat", reason: typeof reason === "string" ? reason : "" },
  };
}

function parseError(raw: Record<string, unknown>): ParsedMessage {
  const { stage, recoverable } = raw;
  if (typeof stage !== "string")
    return refuse("invalid-payload", "error.stage is not a string", SUPPORTED_DATA_VERSION);
  if (typeof recoverable !== "boolean")
    return refuse("invalid-payload", "error.recoverable is not a boolean", SUPPORTED_DATA_VERSION);
  return { ok: true, message: { type: "error", stage, recoverable } };
}

/** Parse one data-channel payload. Never throws. */
export function parseRoomMessage(input: string | Uint8Array | ArrayBuffer): ParsedMessage {
  let text: string;
  try {
    text =
      typeof input === "string"
        ? input
        : decoder.decode(input instanceof ArrayBuffer ? new Uint8Array(input) : input);
  } catch (cause) {
    return refuse("malformed", `payload is not decodable text (${String(cause)})`, null);
  }

  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return refuse("malformed", "payload is not JSON", null);
  }
  if (!isObject(raw)) return refuse("malformed", "payload is not a JSON object", null);

  const version = raw.v;
  if (typeof version !== "number" || !Number.isFinite(version))
    // An unversioned payload is not "version 1 by default". The producer stamps
    // every message; something that does not is something else.
    return refuse("unsupported-version", "payload carries no numeric schema version", null);
  if (version !== SUPPORTED_DATA_VERSION)
    return refuse(
      "unsupported-version",
      `payload is schema v${version}; this client implements v${SUPPORTED_DATA_VERSION} only`,
      version,
    );

  switch (raw.type) {
    case "transcript":
      return parseTranscript(raw);
    case "decision":
      return parseDecision(raw);
    case "notice":
      return parseNotice(raw);
    case "mode":
      return parseMode(raw);
    case "error":
      return parseError(raw);
    default:
      return refuse(
        "unknown-type",
        `no renderer for message type ${JSON.stringify(raw.type)}`,
        version,
      );
  }
}
