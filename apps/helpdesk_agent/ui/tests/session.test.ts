import { describe, expect, it } from "vitest";
import { parseRoomMessage } from "../src/lib/messages";
import {
  INITIAL_SESSION,
  captureCopy,
  isTerminal,
  micAllowed,
  reduceSession,
} from "../src/lib/session";
import type { SessionEvent, VoiceSession } from "../src/lib/session";

/** Feed a session the payloads the pipeline would publish, in order. */
function drive(events: (SessionEvent | Record<string, unknown>)[]): VoiceSession {
  return events.reduce<VoiceSession>((state, item) => {
    const event: SessionEvent =
      "kind" in item ? (item as SessionEvent) : { kind: "data", parsed: parseRoomMessage(JSON.stringify(item)) };
    return reduceSession(state, event);
  }, INITIAL_SESSION);
}

const CONNECT: SessionEvent[] = [{ kind: "connecting" }, { kind: "connected" }];
const NOTICE_PLAYING = { v: 1, type: "notice", state: "playing" };
const NOTICE_UNCONFIRMED = { v: 1, type: "notice", state: "unconfirmed" };
const CHAT_MODE = { v: 1, type: "mode", mode: "chat", reason: "stt_unavailable" };

describe("joining", () => {
  it("is not listening merely because it connected", () => {
    const state = drive(CONNECT);
    expect(state.capture).toBe("awaiting-notice");
    expect(micAllowed(state)).toBe(false);
  });

  it("does not claim the notice is playing before the pipeline has said so", () => {
    // `awaiting-notice`, not `notice-playing`: this client has heard nothing from the
    // pipeline yet, and saying otherwise is what made the bug below possible.
    const state = drive(CONNECT);
    expect(captureCopy(state).detail).toMatch(/not capturing/i);
    expect(captureCopy(state).detail).not.toMatch(/notice .*is playing/i);
  });

  it("never enables the mic on a stray speaker event when no notice was announced", () => {
    // The regression this test exists for. `agent-stopped-speaking` comes from
    // LiveKit's ActiveSpeakersChanged, which fires for ANY remote participant going
    // quiet. Before the fix, `connecting, connected, <any remote speaker stops>`
    // reached `listening` with micAllowed true and copy reading "your audio is being
    // transcribed" -- while GreetingGate was closed and dropping every frame. It is
    // not a hypothetical: the repo ships no consent-notice asset, so `notice: playing`
    // never arrives and this was the ordinary path.
    const stray = drive([...CONNECT, { kind: "agent-stopped-speaking" }]);
    expect(stray.capture).toBe("awaiting-notice");
    expect(micAllowed(stray)).toBe(false);

    // Repeating it does not wear the guard down.
    const persistent = drive([
      ...CONNECT,
      { kind: "agent-stopped-speaking" },
      { kind: "agent-stopped-speaking" },
      { kind: "agent-stopped-speaking" },
    ]);
    expect(micAllowed(persistent)).toBe(false);
  });

  it("starts listening only after the notice has played out", () => {
    const playing = drive([...CONNECT, NOTICE_PLAYING]);
    expect(micAllowed(playing)).toBe(false);
    expect(captureCopy(playing).detail).toMatch(/not capturing yet/i);

    const listening = reduceSession(playing, { kind: "agent-stopped-speaking" });
    expect(listening.capture).toBe("listening");
    expect(micAllowed(listening)).toBe(true);
  });

  it("ignores an agent going quiet when no session is running", () => {
    expect(reduceSession(INITIAL_SESSION, { kind: "agent-stopped-speaking" }).capture).toBe("idle");
  });
});

describe("notice: unconfirmed — the pipeline has refused to listen", () => {
  const refused = drive([...CONNECT, NOTICE_PLAYING, NOTICE_UNCONFIRMED]);

  it("halts capture and says the session is capturing nothing", () => {
    expect(refused.capture).toBe("notice-unconfirmed");
    expect(micAllowed(refused)).toBe(false);
    expect(isTerminal(refused.capture)).toBe(true);
    const copy = captureCopy(refused);
    expect(copy.halted).toBe(true);
    expect(copy.detail).toMatch(/refused to listen for the whole of this session/i);
  });

  it("is not lifted by the agent going quiet", () => {
    // The 30s deadline is a reporting deadline, not a way in (GreetingGate
    // `_check_ack_deadline`). Nothing this browser observes may open it.
    const after = reduceSession(refused, { kind: "agent-stopped-speaking" });
    expect(after.capture).toBe("notice-unconfirmed");
    expect(micAllowed(after)).toBe(false);
  });

  it("is not lifted by transcripts or a decision arriving", () => {
    const after = drive([
      ...CONNECT,
      NOTICE_PLAYING,
      NOTICE_UNCONFIRMED,
      { v: 1, type: "transcript", final: true, text: "कुछ", language: "hi-IN", turn: 0 },
      { v: 1, type: "decision", action: "answer", turn: 0 },
    ]);
    expect(after.capture).toBe("notice-unconfirmed");
    expect(after.turns).toHaveLength(1);
  });

  it("survives the connection dropping", () => {
    expect(reduceSession(refused, { kind: "disconnected" }).capture).toBe("notice-unconfirmed");
  });

  it("moves on only when the pipeline announces a fresh notice, and remembers", () => {
    // `GreetingGate.announce` is the one thing that clears the flag server-side —
    // a second participant joining, say — so it is the one thing that clears it
    // here, and the session still carries the fact that it happened.
    const replayed = reduceSession(refused, {
      kind: "data",
      parsed: parseRoomMessage(JSON.stringify(NOTICE_PLAYING)),
    });
    expect(replayed.capture).toBe("notice-playing");
    expect(replayed.noticeEverUnconfirmed).toBe(true);
    const listening = reduceSession(replayed, { kind: "agent-stopped-speaking" });
    expect(listening.noticeEverUnconfirmed).toBe(true);
    expect(micAllowed(listening)).toBe(true);
  });
});

describe("mode: chat — speech recognition is gone", () => {
  const dead = drive([...CONNECT, NOTICE_PLAYING, { kind: "agent-stopped-speaking" }, CHAT_MODE]);

  it("ends voice, keeps the reason and offers no way back", () => {
    expect(dead.capture).toBe("chat-only");
    expect(dead.modeReason).toBe("stt_unavailable");
    expect(micAllowed(dead)).toBe(false);
    expect(captureCopy(dead).halted).toBe(true);
    expect(captureCopy(dead).detail).toMatch(/does not resume in this session/i);
  });

  it("is not lifted by anything the pipeline says afterwards", () => {
    for (const event of [
      { kind: "agent-stopped-speaking" } as SessionEvent,
      { kind: "data", parsed: parseRoomMessage(JSON.stringify(NOTICE_PLAYING)) } as SessionEvent,
    ]) {
      expect(reduceSession(dead, event).capture).toBe("chat-only");
    }
  });

  it("clears only on a deliberate new session", () => {
    expect(reduceSession(dead, { kind: "reset" })).toEqual(INITIAL_SESSION);
    expect(reduceSession(dead, { kind: "connecting" }).capture).toBe("connecting");
  });
});

describe("payloads this client cannot read", () => {
  const listening = drive([...CONNECT, NOTICE_PLAYING, { kind: "agent-stopped-speaking" }]);

  it("stops capture on an unsupported schema version rather than rendering part of it", () => {
    const state = reduceSession(listening, {
      kind: "data",
      parsed: parseRoomMessage(JSON.stringify({ v: 2, type: "notice", state: "playing" })),
    });
    expect(state.capture).toBe("incompatible");
    expect(micAllowed(state)).toBe(false);
    expect(state.refusal).toMatchObject({ reason: "unsupported-version", version: 2 });
  });

  it("stops capture on an unreadable payload of the right version", () => {
    const state = reduceSession(listening, {
      kind: "data",
      parsed: parseRoomMessage(JSON.stringify({ v: 1, type: "notice", state: "perhaps" })),
    });
    expect(state.capture).toBe("incompatible");
    expect(state.refusal?.reason).toBe("invalid-payload");
  });

  it("does not resume rendering the messages it does understand", () => {
    const state = drive([
      ...CONNECT,
      NOTICE_PLAYING,
      { kind: "agent-stopped-speaking" },
      { v: 2, type: "transcript", final: false, text: "x", language: "hi-IN", turn: 0 },
      { v: 1, type: "transcript", final: true, text: "मैं ठीक हूँ", language: "hi-IN", turn: 0 },
    ]);
    expect(state.capture).toBe("incompatible");
    expect(state.turns).toEqual([]);
  });
});

describe("transcripts", () => {
  it("revises a partial in place and settles it on the final", () => {
    const state = drive([
      ...CONNECT,
      NOTICE_PLAYING,
      { kind: "agent-stopped-speaking" },
      { v: 1, type: "transcript", final: false, text: "घर से", language: "hi-IN", turn: 0 },
      { v: 1, type: "transcript", final: false, text: "घर से वीपीएन", language: "hi-IN", turn: 0 },
      {
        v: 1,
        type: "transcript",
        final: true,
        text: "घर से वीपीएन कनेक्ट करने का तरीका बता दीजिए।",
        language: "hi-IN",
        turn: 0,
      },
    ]);
    expect(state.turns).toHaveLength(1);
    expect(state.turns[0]).toMatchObject({
      turn: 0,
      partial: "",
      final: "घर से वीपीएन कनेक्ट करने का तरीका बता दीजिए।",
      language: "hi-IN",
    });
  });

  it("keeps turns separate and in order, whatever order they arrive in", () => {
    const state = drive([
      ...CONNECT,
      { v: 1, type: "transcript", final: true, text: "second", language: "en-IN", turn: 1 },
      { v: 1, type: "transcript", final: true, text: "first", language: "en-IN", turn: 0 },
    ]);
    expect(state.turns.map((turn) => turn.turn)).toEqual([0, 1]);
    expect(state.turns.map((turn) => turn.final)).toEqual(["first", "second"]);
  });

  it("keeps the last identified language when a later segment identifies none", () => {
    const state = drive([
      ...CONNECT,
      { v: 1, type: "transcript", final: false, text: "मैं", language: "hi-IN", turn: 0 },
      { v: 1, type: "transcript", final: false, text: "मैं ठीक", language: null, turn: 0 },
    ]);
    expect(state.turns[0]?.language).toBe("hi-IN");
  });

  it("attaches the decision to its own turn", () => {
    const state = drive([
      ...CONNECT,
      { v: 1, type: "transcript", final: true, text: "a", language: "en-IN", turn: 0 },
      { v: 1, type: "decision", action: "file_ticket", turn: 0 },
    ]);
    expect(state.turns[0]?.action).toBe("file_ticket");
  });

  it("records a decision for a turn whose transcript was empty", () => {
    // `_finalize` publishes nothing when the utterance came back empty, but the
    // graph can still have run: the decision must not create a phantom turn 0 that
    // swallows the next one's caption.
    const state = drive([...CONNECT, { v: 1, type: "decision", action: "clarify", turn: 3 }]);
    expect(state.turns).toEqual([
      { turn: 3, partial: "", final: null, language: null, action: "clarify" },
    ]);
  });
});

describe("errors", () => {
  it("records a recoverable failure without ending the session", () => {
    const state = drive([
      ...CONNECT,
      NOTICE_PLAYING,
      { kind: "agent-stopped-speaking" },
      { v: 1, type: "transcript", final: true, text: "a", language: "en-IN", turn: 0 },
      { v: 1, type: "error", stage: "decide", recoverable: true },
    ]);
    expect(state.capture).toBe("listening");
    expect(state.errors).toEqual([{ turn: 0, stage: "decide", recoverable: true }]);
  });

  it("halts on an unrecoverable one", () => {
    const state = drive([
      ...CONNECT,
      NOTICE_PLAYING,
      { kind: "agent-stopped-speaking" },
      { v: 1, type: "error", stage: "decide", recoverable: false },
    ]);
    expect(state.capture).toBe("agent-error");
    expect(micAllowed(state)).toBe(false);
  });
});

describe("the invariant", () => {
  it("offers the microphone in exactly one capture state", () => {
    const states: VoiceSession["capture"][] = [
      "idle",
      "connecting",
      "notice-playing",
      "listening",
      "notice-unconfirmed",
      "chat-only",
      "incompatible",
      "agent-error",
    ];
    const live = states.filter((capture) => micAllowed({ ...INITIAL_SESSION, capture }));
    expect(live).toEqual(["listening"]);
  });

  it("gives every halted state words that do not claim the room is listening", () => {
    for (const capture of ["notice-unconfirmed", "chat-only", "incompatible", "agent-error"] as const) {
      const copy = captureCopy({ ...INITIAL_SESSION, capture });
      expect(copy.halted).toBe(true);
      expect(copy.label.toLowerCase()).not.toContain("listening...");
      expect(copy.detail.length).toBeGreaterThan(40);
    }
  });
});
