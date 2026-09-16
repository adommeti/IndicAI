import { describe, expect, it } from "vitest";
import {
  bypassRefusedHere,
  devBypassRequested,
  isLoopbackHost,
  resolveAuth,
  unauthenticatedHint,
} from "../src/lib/auth";
import type { AuthEnv } from "../src/lib/auth";

const ENTRA: AuthEnv = {
  VITE_MSAL_CLIENT_ID: "11111111-2222-3333-4444-555555555555",
  VITE_MSAL_TENANT: "contoso.onmicrosoft.com",
  VITE_API_SCOPE: "api://helpdesk/access_as_user",
};

describe("the dev bypass flag", () => {
  it("is only the exact string true", () => {
    expect(devBypassRequested({ VITE_AUTH_DEV_BYPASS: "true" })).toBe(true);
    for (const value of ["1", "yes", "TRUE", "True", " true", "", undefined])
      expect(devBypassRequested({ VITE_AUTH_DEV_BYPASS: value })).toBe(false);
  });

  it("applies on loopback origins and nowhere else", () => {
    for (const host of ["localhost", "127.0.0.1", "::1", "[::1]", "app.localhost", "0.0.0.0"])
      expect(isLoopbackHost(host)).toBe(true);
    for (const host of [
      "helpdesk.contoso.com",
      "10.0.0.5",
      "192.168.1.20",
      "localhost.evil.example",
      "",
    ])
      expect(isLoopbackHost(host)).toBe(false);
  });

  it("is honoured locally", () => {
    expect(resolveAuth({ VITE_AUTH_DEV_BYPASS: "true" }, "localhost").kind).toBe("dev-bypass");
    expect(resolveAuth({ VITE_AUTH_DEV_BYPASS: "true" }, "127.0.0.1").kind).toBe("dev-bypass");
  });

  it("is refused off loopback, and the build falls back to real sign-in", () => {
    // The accident this guards: a bundle built with the flag gets deployed. It
    // must not sign anybody in there; it must sign everybody in the normal way.
    const mode = resolveAuth({ ...ENTRA, VITE_AUTH_DEV_BYPASS: "true" }, "helpdesk.contoso.com");
    expect(mode.kind).toBe("msal");
    expect(bypassRefusedHere({ VITE_AUTH_DEV_BYPASS: "true" }, "helpdesk.contoso.com")).toBe(true);
    expect(bypassRefusedHere({ VITE_AUTH_DEV_BYPASS: "true" }, "localhost")).toBe(false);
  });

  it("fails loudly, never silently open, when there is nothing to fall back to", () => {
    const mode = resolveAuth({ VITE_AUTH_DEV_BYPASS: "true" }, "helpdesk.contoso.com");
    expect(mode.kind).toBe("unconfigured");
    expect(mode.kind === "unconfigured" && mode.reason).toMatch(/loopback/i);
  });
});

describe("Entra ID configuration", () => {
  it("builds the tenant authority", () => {
    const mode = resolveAuth(ENTRA, "helpdesk.contoso.com");
    expect(mode).toEqual({
      kind: "msal",
      clientId: ENTRA.VITE_MSAL_CLIENT_ID,
      authority: "https://login.microsoftonline.com/contoso.onmicrosoft.com",
      scope: ENTRA.VITE_API_SCOPE,
    });
  });

  it("lets an explicit authority win, for sovereign and B2C clouds", () => {
    const mode = resolveAuth(
      { ...ENTRA, VITE_MSAL_AUTHORITY: "https://login.microsoftonline.us/contoso" },
      "helpdesk.contoso.com",
    );
    expect(mode.kind === "msal" && mode.authority).toBe("https://login.microsoftonline.us/contoso");
  });

  it("refuses a half-configured build rather than signing in without an API scope", () => {
    // A token for Graph is not a token this API accepts, so "no scope" is an
    // error, not a default.
    const mode = resolveAuth({ ...ENTRA, VITE_API_SCOPE: "" }, "helpdesk.contoso.com");
    expect(mode.kind).toBe("unconfigured");
  });

  it("refuses a build with no client id or tenant at all", () => {
    expect(resolveAuth({}, "helpdesk.contoso.com").kind).toBe("unconfigured");
    expect(resolveAuth({ VITE_MSAL_CLIENT_ID: "x" }, "helpdesk.contoso.com").kind).toBe(
      "unconfigured",
    );
  });
});

describe("what a 401 is told to the user", () => {
  it("points a local build at the API's own flag, not at the browser's", () => {
    const hint = unauthenticatedHint(resolveAuth({ VITE_AUTH_DEV_BYPASS: "true" }, "localhost"));
    expect(hint).toMatch(/AUTH__DEV_BYPASS=true/);
    expect(hint).toMatch(/grants nothing on its own/i);
  });

  it("reads as an expired session in a deployed build", () => {
    expect(unauthenticatedHint(resolveAuth(ENTRA, "helpdesk.contoso.com"))).toMatch(/expired/i);
  });

  it("repeats the configuration problem when there is one", () => {
    const mode = resolveAuth({}, "helpdesk.contoso.com");
    expect(unauthenticatedHint(mode)).toBe(mode.kind === "unconfigured" ? mode.reason : "");
  });
});
