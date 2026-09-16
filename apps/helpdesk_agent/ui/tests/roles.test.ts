import { describe, expect, it } from "vitest";
import { allowedViews, capabilities, parseRoute, roleLabel, routeHref } from "../src/lib/roles";
import type { Role } from "../src/lib/types";

describe("capabilities", () => {
  it("opens replay for uc1's governance role", () => {
    expect(capabilities(["governance"])).toEqual({ replay: true });
    expect(allowedViews(capabilities(["governance"]))).toEqual(["chat", "replay"]);
  });

  it("gives an employee with no role the helpdesk, which is the normal case", () => {
    // `GET /me` returning {"roles": []} is an answer, not a failure: most
    // employees hold nothing, and the chat panel is still theirs.
    expect(capabilities([])).toEqual({ replay: false });
    expect(allowedViews(capabilities([]))).toEqual(["chat"]);
  });

  it("ignores a role name uc1 has no rule for", () => {
    // The API drops unrecognised scopes (`roles.recognised_roles`), so this should
    // never arrive; if it ever does, it grants nothing here either.
    expect(capabilities(["compliance_lead" as Role])).toEqual({ replay: false });
  });

  it("does not treat uc3's subtractive reading of the same word as applying here", () => {
    // uc3: `governance` DENIES content. uc1: it GRANTS replay. Same word, opposite
    // effect, two different Entra groups (helpdesk_agent/roles.py).
    expect(capabilities(["governance"]).replay).toBe(true);
    expect(roleLabel("governance")).toMatch(/uc1/);
  });
});

describe("routing", () => {
  it("defaults to the chat view", () => {
    expect(parseRoute("")).toEqual({ view: "chat", sessionId: null });
    expect(parseRoute("#/")).toEqual({ view: "chat", sessionId: null });
    expect(parseRoute("#/nonsense")).toEqual({ view: "chat", sessionId: null });
  });

  it("reads a deep link to one session", () => {
    expect(parseRoute("#/replay/8e0b3a1e-0000-4000-8000-000000000000")).toEqual({
      view: "replay",
      sessionId: "8e0b3a1e-0000-4000-8000-000000000000",
    });
  });

  it("decodes an id that needed escaping and round-trips", () => {
    expect(parseRoute("#/replay/a%2Fb").sessionId).toBe("a/b");
    expect(parseRoute(routeHref("replay", "a/b"))).toEqual({ view: "replay", sessionId: "a/b" });
    expect(parseRoute(routeHref("chat"))).toEqual({ view: "chat", sessionId: null });
  });

  it("keeps the replay route addressable with no role, because hiding is not the control", () => {
    // The tab is not drawn without the role; this URL still parses, the view still
    // asks, and the server's 403 is what the user sees. That is deliberate.
    expect(parseRoute("#/replay").view).toBe("replay");
    expect(allowedViews(capabilities([]))).not.toContain("replay");
  });
});
