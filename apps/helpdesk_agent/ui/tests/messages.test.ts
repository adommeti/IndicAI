import { describe, expect, it } from "vitest";
import { SUPPORTED_DATA_VERSION, parseRoomMessage } from "../src/lib/messages";

function encode(value: unknown): Uint8Array {
  return new TextEncoder().encode(JSON.stringify(value));
}

describe("the five shapes voice_pipeline.py publishes", () => {
  it("reads a partial transcript", () => {
    const parsed = parseRoomMessage(
      JSON.stringify({
        v: 1,
        type: "transcript",
        final: false,
        text: "घर से वीपीएन",
        language: "hi-IN",
        turn: 0,
      }),
    );
    expect(parsed).toEqual({
      ok: true,
      message: {
        type: "transcript",
        final: false,
        text: "घर से वीपीएन",
        language: "hi-IN",
        turn: 0,
      },
    });
  });

  it("reads a final transcript off the wire as bytes", () => {
    const parsed = parseRoomMessage(
      encode({
        v: 1,
        type: "transcript",
        final: true,
        text: "घर से वीपीएन कनेक्ट करने का तरीका बता दीजिए।",
        language: "hi-IN",
        turn: 2,
      }),
    );
    expect(parsed.ok).toBe(true);
    expect(parsed.ok && parsed.message).toMatchObject({ final: true, turn: 2 });
  });

  it("accepts a transcript whose language the recogniser never identified", () => {
    const parsed = parseRoomMessage(
      JSON.stringify({ v: 1, type: "transcript", final: false, text: "hi", language: null, turn: 0 }),
    );
    expect(parsed.ok && parsed.message).toMatchObject({ language: null });
  });

  it("reads a decision, including one the graph left unset", () => {
    expect(
      parseRoomMessage(JSON.stringify({ v: 1, type: "decision", action: "file_ticket", turn: 1 })),
    ).toEqual({ ok: true, message: { type: "decision", action: "file_ticket", turn: 1 } });
    expect(
      parseRoomMessage(JSON.stringify({ v: 1, type: "decision", action: null, turn: 1 })),
    ).toEqual({ ok: true, message: { type: "decision", action: null, turn: 1 } });
  });

  it("reads both notice states", () => {
    for (const state of ["playing", "unconfirmed"] as const) {
      expect(parseRoomMessage(JSON.stringify({ v: 1, type: "notice", state }))).toEqual({
        ok: true,
        message: { type: "notice", state },
      });
    }
  });

  it("reads the chat-mode switch and keeps the pipeline's reason", () => {
    expect(
      parseRoomMessage(JSON.stringify({ v: 1, type: "mode", mode: "chat", reason: "stt_unavailable" })),
    ).toEqual({ ok: true, message: { type: "mode", mode: "chat", reason: "stt_unavailable" } });
  });

  it("reads a per-turn error", () => {
    expect(
      parseRoomMessage(JSON.stringify({ v: 1, type: "error", stage: "decide", recoverable: true })),
    ).toEqual({ ok: true, message: { type: "error", stage: "decide", recoverable: true } });
  });
});

describe("refusals", () => {
  it("refuses a schema version it does not implement", () => {
    const parsed = parseRoomMessage(
      JSON.stringify({ v: 2, type: "transcript", final: true, text: "x", language: "hi-IN", turn: 0 }),
    );
    expect(parsed).toMatchObject({ ok: false, reason: "unsupported-version", version: 2 });
  });

  it("does not treat an unversioned payload as version 1", () => {
    // The producer stamps every message (`_message` in voice_pipeline.py). A
    // payload that is not stamped came from something else, and guessing that it
    // is v1 is how half a transcript gets rendered.
    const parsed = parseRoomMessage(
      JSON.stringify({ type: "transcript", final: true, text: "x", language: "hi-IN", turn: 0 }),
    );
    expect(parsed).toMatchObject({ ok: false, reason: "unsupported-version", version: null });
  });

  it("refuses a version that is not a number", () => {
    expect(parseRoomMessage(JSON.stringify({ v: "1", type: "notice", state: "playing" }))).toMatchObject(
      { ok: false, reason: "unsupported-version" },
    );
  });

  it("refuses a message type it cannot render", () => {
    expect(parseRoomMessage(JSON.stringify({ v: 1, type: "vitals", bpm: 60 }))).toMatchObject({
      ok: false,
      reason: "unknown-type",
      version: 1,
    });
  });

  it("refuses a notice state it cannot read — the consent control is not guessable", () => {
    expect(parseRoomMessage(JSON.stringify({ v: 1, type: "notice", state: "maybe" }))).toMatchObject({
      ok: false,
      reason: "invalid-payload",
    });
  });

  it("refuses a transcript whose finality is not a boolean", () => {
    // "final": "true" must not be read as final. A partial rendered as settled is
    // the employee reading a guess as what they said.
    expect(
      parseRoomMessage(
        JSON.stringify({ v: 1, type: "transcript", final: "true", text: "x", language: null, turn: 0 }),
      ),
    ).toMatchObject({ ok: false, reason: "invalid-payload" });
  });

  it("refuses a transcript with no turn number and does not default it to 0", () => {
    expect(
      parseRoomMessage(JSON.stringify({ v: 1, type: "transcript", final: true, text: "x" })),
    ).toMatchObject({ ok: false, reason: "invalid-payload" });
  });

  it("refuses an unknown decision action rather than showing it as something else", () => {
    expect(
      parseRoomMessage(JSON.stringify({ v: 1, type: "decision", action: "escalate", turn: 0 })),
    ).toMatchObject({ ok: false, reason: "invalid-payload" });
  });

  it("refuses a mode message that is not the chat switch", () => {
    expect(
      parseRoomMessage(JSON.stringify({ v: 1, type: "mode", mode: "voice", reason: "" })),
    ).toMatchObject({ ok: false, reason: "invalid-payload" });
  });

  it("refuses an error whose recoverability is missing", () => {
    expect(parseRoomMessage(JSON.stringify({ v: 1, type: "error", stage: "decide" }))).toMatchObject({
      ok: false,
      reason: "invalid-payload",
    });
  });

  it("refuses payloads that are not JSON objects, and never throws", () => {
    expect(parseRoomMessage("not json")).toMatchObject({ ok: false, reason: "malformed" });
    expect(parseRoomMessage("[1,2,3]")).toMatchObject({ ok: false, reason: "malformed" });
    expect(parseRoomMessage('"hello"')).toMatchObject({ ok: false, reason: "malformed" });
    expect(parseRoomMessage("")).toMatchObject({ ok: false, reason: "malformed" });
  });

  it("pins the version this client implements against the pipeline's constant", () => {
    // voice_pipeline.DATA_MESSAGE_VERSION = 1. If that changes, this client has to
    // be taught the new shapes, and this line is where it is noticed.
    expect(SUPPORTED_DATA_VERSION).toBe(1);
  });
});
