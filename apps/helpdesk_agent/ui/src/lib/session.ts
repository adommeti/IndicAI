import type { DecisionAction, ParsedMessage, RefusalReason } from "./messages";

/** The voice session as the browser is entitled to describe it.
 *
 *  ## The one rule this file exists to keep
 *
 *  The microphone control must never look live while the pipeline is not listening.
 *  `GreetingGate` drops every input frame until the consent notice has played out,
 *  and `SarvamSTTProcessor` stops listening permanently when Saaras dies. Both say
 *  so on the data channel, and both keep doing what they do whether or not this
 *  client renders it — the gate is server-side and is the actual control. What this
 *  reducer prevents is the browser *telling the employee something untrue* about it.
 *
 *  So capture states are terminal where the pipeline's are terminal, and the client
 *  never invents a recovery the pipeline did not announce:
 *
 *  - `notice-playing`     the notice is playing; the room is NOT capturing yet.
 *  - `listening`          the notice played out and the gate opened.
 *  - `notice-unconfirmed` the pipeline could not confirm the notice was heard and
 *                         has refused to listen. Nothing in this browser lifts it —
 *                         only a fresh `notice: playing` from the pipeline (which is
 *                         the only thing that clears the flag server-side too) or a
 *                         reconnection. Even then the refusal is remembered and
 *                         stays on screen for the rest of the session.
 *  - `chat-only`          STT is gone. The pipeline says "No content, no recovery",
 *                         so neither does this: voice is over until a new session.
 *  - `incompatible`       a payload this client could not read (see below).
 *
 *  ## Why any unreadable payload halts capture
 *
 *  This is a consent-bearing channel. A client that cannot read everything the
 *  pipeline says cannot honestly claim to know whether the room is capturing, so an
 *  unsupported schema version, an unknown message type, a mistyped field or a
 *  malformed frame all stop capture and say which one it was. The cost is that a
 *  purely additive v1 message type would halt this client until it is taught the
 *  type; that is the deliberate direction to be wrong in.
 */

export type CaptureState =
  | "idle"
  | "connecting"
  | "awaiting-notice"
  | "notice-playing"
  | "listening"
  | "notice-unconfirmed"
  | "chat-only"
  | "incompatible"
  | "agent-error";

export interface VoiceTurn {
  turn: number;
  /** The newest partial transcript, replaced as it is revised. "" when none. */
  partial: string;
  /** Set once, when the pipeline commits to the utterance. */
  final: string | null;
  /** The language Saaras identified, not one this client asked for. */
  language: string | null;
  /** The action the graph took for this turn, once it says so. */
  action: DecisionAction | null;
}

export interface SessionRefusal {
  reason: RefusalReason;
  detail: string;
  version: number | null;
}

export interface TurnError {
  turn: number | null;
  stage: string;
  recoverable: boolean;
}

export interface VoiceSession {
  capture: CaptureState;
  turns: VoiceTurn[];
  /** True once the pipeline has ever refused to confirm the notice, even if a
   *  later announcement moved the session on. Sticky for the whole session. */
  noticeEverUnconfirmed: boolean;
  /** Why voice ended, when it ended: the pipeline's own `reason` string. */
  modeReason: string | null;
  /** The payload that stopped this client, when one did. */
  refusal: SessionRefusal | null;
  /** Non-fatal per-turn failures the pipeline reported (`recoverable: true`). */
  errors: TurnError[];
}

export type SessionEvent =
  | { kind: "connecting" }
  | { kind: "connected" }
  | { kind: "disconnected" }
  | { kind: "data"; parsed: ParsedMessage }
  /** The agent's audio track went quiet.
   *
   *  This is the browser-side twin of the `BotStoppedSpeakingFrame` that makes the
   *  server open its gate: same event, observed independently. It is a DISPLAY
   *  signal only — it moves this client from "notice playing" to "listening" so the
   *  mic control stops saying "wait". It does not open anything; the gate is in the
   *  pipeline and has already decided. */
  | { kind: "agent-stopped-speaking" }
  | { kind: "reset" };

export const INITIAL_SESSION: VoiceSession = {
  capture: "idle",
  turns: [],
  noticeEverUnconfirmed: false,
  modeReason: null,
  refusal: null,
  errors: [],
};

/** States the pipeline has to speak first to leave. No click, no timeout, and no
 *  inference in this browser gets out of one. */
const TERMINAL: ReadonlySet<CaptureState> = new Set<CaptureState>([
  "notice-unconfirmed",
  "chat-only",
  "incompatible",
  "agent-error",
]);

export function isTerminal(state: CaptureState): boolean {
  return TERMINAL.has(state);
}

function upsertTurn(
  turns: VoiceTurn[],
  index: number,
  patch: (turn: VoiceTurn) => VoiceTurn,
): VoiceTurn[] {
  const found = turns.find((turn) => turn.turn === index);
  const base: VoiceTurn = found ?? {
    turn: index,
    partial: "",
    final: null,
    language: null,
    action: null,
  };
  const next = patch(base);
  const rest = found ? turns.filter((turn) => turn.turn !== index) : turns;
  return [...rest, next].sort((a, b) => a.turn - b.turn);
}

function applyRefusal(state: VoiceSession, parsed: Extract<ParsedMessage, { ok: false }>) {
  return {
    ...state,
    capture: "incompatible" as const,
    refusal: { reason: parsed.reason, detail: parsed.detail, version: parsed.version },
  };
}

export function reduceSession(state: VoiceSession, event: SessionEvent): VoiceSession {
  switch (event.kind) {
    case "reset":
      return INITIAL_SESSION;

    case "connecting":
      return { ...INITIAL_SESSION, capture: "connecting" };

    case "connected":
      // Connected is not listening, and it is not "the notice is playing" either --
      // this client has heard NOTHING from the pipeline yet. Claiming the notice was
      // playing here is what let a room with no notice at all reach `listening`: the
      // repo ships no consent-notice asset, so `notice: playing` never arrives, and
      // any remote participant falling quiet was enough to promote. `awaiting-notice`
      // is the honest name for "I do not know what the gate is doing".
      return state.capture === "connecting" ? { ...state, capture: "awaiting-notice" } : state;

    case "disconnected":
      // A terminal state outlives the connection: the reason the session stopped
      // listening is still what the employee needs to read.
      return isTerminal(state.capture) ? state : { ...state, capture: "idle" };

    case "agent-stopped-speaking":
      // Promotes ONLY out of `notice-playing`, which only a received
      // `{"type":"notice","state":"playing"}` can put us in. That guard is the whole
      // point: this event comes from LiveKit's `ActiveSpeakersChanged`, which fires
      // for ANY remote participant going quiet -- a supervisor, a mid-notice pause in
      // the TTS audio, or nothing at all. Without it the deterministic sequence
      // `connecting, connected, <any remote speaker stops>` enabled the microphone and
      // told the employee "your audio is being transcribed" while `GreetingGate` was
      // closed and dropping every frame.
      //
      // It is still an inference, and it is display-only: the gate is server-side. The
      // pipeline publishing an explicit `notice: open` when `GreetingGate._open` fires
      // would remove the inference entirely (docs/build/BLOCKERS.md).
      return state.capture === "notice-playing" ? { ...state, capture: "listening" } : state;

    case "data":
      return reduceData(state, event.parsed);
  }
}

function reduceData(state: VoiceSession, parsed: ParsedMessage): VoiceSession {
  if (!parsed.ok) return applyRefusal(state, parsed);

  // Once this client has refused a payload it does not understand, it does not
  // resume rendering the ones it does: the gap is the problem, not the frame.
  if (state.capture === "incompatible") return state;

  const message = parsed.message;
  switch (message.type) {
    case "notice":
      if (message.state === "unconfirmed")
        return { ...state, capture: "notice-unconfirmed", noticeEverUnconfirmed: true };
      // A fresh announcement. This is the ONLY thing that clears the pipeline's
      // own `notice_unconfirmed` flag (`GreetingGate.announce`), so it is the only
      // thing that moves this client out of a refusal -- and `noticeEverUnconfirmed`
      // keeps the refusal on screen regardless.
      if (state.capture === "chat-only" || state.capture === "agent-error") return state;
      return { ...state, capture: "notice-playing" };

    case "mode":
      // STT is gone and the pipeline has stopped listening. There is no path back
      // in this session, so there is none here either.
      return { ...state, capture: "chat-only", modeReason: message.reason };

    case "transcript": {
      // A transcript arriving in a halted state does not un-halt it. It is still
      // shown -- refusing to render words the employee already said helps nobody --
      // but the capture state is the pipeline's to change, not a side effect of a
      // caption.
      const turns = upsertTurn(state.turns, message.turn, (turn) =>
        message.final
          ? { ...turn, final: message.text, partial: "", language: message.language ?? turn.language }
          : { ...turn, partial: message.text, language: message.language ?? turn.language },
      );
      return { ...state, turns };
    }

    case "decision": {
      const turns = upsertTurn(state.turns, message.turn, (turn) => ({
        ...turn,
        action: message.action,
      }));
      return { ...state, turns };
    }

    case "error": {
      const turn = state.turns.length ? (state.turns[state.turns.length - 1]?.turn ?? null) : null;
      const errors = [...state.errors, { turn, stage: message.stage, recoverable: message.recoverable }];
      // `recoverable: false` has never been sent by the shipped pipeline. If it
      // ever is, it means the agent has stopped -- which is not a per-turn notice.
      return message.recoverable
        ? { ...state, errors }
        : { ...state, errors, capture: "agent-error" };
    }
  }
}

/** Whether the microphone control may be offered as live.
 *
 *  Not a permission and not a control: the pipeline drops input audio whatever this
 *  returns. It decides what the button is allowed to *claim*. */
export function micAllowed(state: VoiceSession): boolean {
  return state.capture === "listening";
}

export interface CaptureCopy {
  /** Short label for the mic control. */
  label: string;
  /** One sentence the employee can act on. */
  detail: string;
  /** Halted states are shown as a boxed, non-dismissible panel. */
  halted: boolean;
}

/** One place that turns a capture state into words, so the mic button, the banner
 *  and the screen-reader announcement can never disagree about what is happening. */
export function captureCopy(state: VoiceSession): CaptureCopy {
  switch (state.capture) {
    case "idle":
      return {
        label: "Start voice",
        detail: "Voice is not connected. Chat below works either way.",
        halted: false,
      };
    case "connecting":
      return { label: "Connecting", detail: "Joining the room.", halted: false };
    case "awaiting-notice":
      return {
        label: "Waiting for the agent",
        detail:
          "Connected. The agent has not started the recorded notice yet, so the room is not capturing and your microphone is not being read.",
        halted: false,
      };
    case "notice-playing":
      return {
        label: "Notice playing",
        detail:
          "The recorded notice telling you this call is recorded and transcribed is playing. The room is not capturing yet — your microphone is not being read.",
        halted: false,
      };
    case "listening":
      return {
        label: "Listening",
        detail: "The notice finished. Speak; your audio is being transcribed.",
        halted: false,
      };
    case "notice-unconfirmed":
      return {
        label: "Not listening",
        detail:
          "The agent could not confirm the recorded notice was played, so it has refused to listen for the whole of this session. Nothing you say is being captured. Leave and rejoin to have the notice played again.",
        halted: true,
      };
    case "chat-only":
      return {
        label: "Voice ended",
        detail:
          "Speech recognition became unavailable, so the agent stopped listening. It does not resume in this session — carry on in chat below, or start a new session.",
        halted: true,
      };
    case "incompatible":
      return {
        label: "Voice stopped",
        detail:
          "This page could not read a message from the agent, so it has stopped reporting on the microphone rather than guess. Reload to pick up the current version of the page.",
        halted: true,
      };
    case "agent-error":
      return {
        label: "Agent stopped",
        detail:
          "The agent reported an unrecoverable failure and has stopped. Start a new session, or carry on in chat below.",
        halted: true,
      };
  }
}

/** A refusal in the words the person reading it needs. */
export function refusalCopy(refusal: SessionRefusal): string {
  switch (refusal.reason) {
    case "unsupported-version":
      return refusal.version === null
        ? "The agent sent a message with no schema version. This page renders version 1 only."
        : `The agent is publishing schema v${refusal.version}; this page renders v1 only. It is out of date — reload, and if that does not help the deployed page needs rebuilding.`;
    case "unknown-type":
      return `The agent sent a message type this page has no renderer for (${refusal.detail}).`;
    case "invalid-payload":
      return `The agent sent a message whose fields are not what this page expects (${refusal.detail}).`;
    case "malformed":
      return `A message from the agent could not be read at all (${refusal.detail}).`;
  }
}
