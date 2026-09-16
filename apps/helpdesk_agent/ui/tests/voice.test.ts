import { describe, expect, it } from "vitest";
import { readGrant, resolveVoice } from "../src/lib/voice";

describe("where the join details come from", () => {
  it("uses a configured token endpoint", () => {
    expect(resolveVoice({ VITE_LIVEKIT_TOKEN_PATH: "/voice/token" }, false)).toEqual({
      kind: "endpoint",
      path: "/voice/token",
      url: null,
    });
  });

  it("refuses a token endpoint on another origin", () => {
    // Minting a join token requires the LiveKit API secret. Posting an identity to
    // a third-party origin to get one is not something to do by configuration typo.
    const config = resolveVoice(
      { VITE_LIVEKIT_TOKEN_PATH: "https://tokens.example.com/mint" },
      true,
    );
    expect(config.kind).toBe("unavailable");
  });

  it("offers a pasted token only in a local dev-bypass build", () => {
    const local = resolveVoice({}, true);
    expect(local).toMatchObject({ kind: "manual", url: "ws://localhost:7880" });
    expect(local.kind === "manual" && local.reason).toMatch(/voice_demo\.md/);
  });

  it("says voice is unavailable in a deployed build with no endpoint, and why", () => {
    // The pinned uc1/P6 contract has no token endpoint, so this is the shipped
    // default. Chat still works; the UI says so rather than showing a dead mic.
    const config = resolveVoice({}, false);
    expect(config.kind).toBe("unavailable");
    expect(config.kind === "unavailable" && config.reason).toMatch(/VITE_LIVEKIT_TOKEN_PATH/);
    expect(config.kind === "unavailable" && config.reason).toMatch(/Chat below is unaffected/);
  });
});

describe("reading a grant", () => {
  it("takes the url from the response, or the configured fallback", () => {
    expect(readGrant({ token: "jwt", url: "wss://lk.example", room: "helpdesk-1" }, null)).toEqual({
      token: "jwt",
      url: "wss://lk.example",
      room: "helpdesk-1",
    });
    expect(readGrant({ token: "jwt" }, "ws://localhost:7880")).toMatchObject({
      url: "ws://localhost:7880",
      room: "",
    });
  });

  it("refuses anything that is not a usable grant", () => {
    expect(() => readGrant({ url: "ws://x" }, null)).toThrow(/no token/i);
    expect(() => readGrant({ token: 42 }, "ws://x")).toThrow(/no token/i);
    expect(() => readGrant({ token: "jwt" }, null)).toThrow(/nowhere to connect/i);
    expect(() => readGrant("jwt", null)).toThrow(/JSON object/i);
    expect(() => readGrant(null, null)).toThrow(/JSON object/i);
  });
});
