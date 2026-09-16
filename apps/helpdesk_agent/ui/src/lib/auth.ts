/** Sign-in: Entra ID through MSAL, with a local bypass that cannot travel.
 *
 *  `.claude/rules/ui.md` asks for MSAL (Entra ID) with `VITE_AUTH_DEV_BYPASS=true`
 *  for local work. The risk in a build-time bypass flag is not that someone sets it
 *  on purpose; it is that a build made with it set gets deployed somewhere real and
 *  nobody notices, because everything still appears to work.
 *
 *  Three things make that hard here, and they are the whole of the mechanism:
 *
 *  1. **It is build-time only.** Vite inlines `import.meta.env.VITE_AUTH_DEV_BYPASS`
 *     as a literal, so nothing at runtime — a query string, `localStorage`, a header,
 *     a console line — can switch it on in a bundle that was built without it.
 *  2. **It is refused off a loopback origin.** A bypass build served from a real
 *     hostname falls back to real MSAL sign-in, and says loudly that it did. So the
 *     failure mode of the accident is "nobody can sign in", not "everybody is signed
 *     in as a test identity".
 *  3. **It grants nothing.** It only skips the browser's token acquisition; the API
 *     authenticates every request itself. Against an API without its own bypass a
 *     bypass build simply gets 401s. uc1 does have a server-side half --
 *     `AUTH__DEV_BYPASS` in `helpdesk_agent/roles.py`, refused outside
 *     dev/local/test/ci -- but it is a separate switch that this flag cannot set and
 *     cannot reach. Roles always come from `GET /me`, never from here.
 *
 *  What it cannot do, stated plainly: it cannot create a session, choose an identity,
 *  grant a role, or make the replay endpoint answer. Only the server does those.
 */

export interface AuthEnv {
  VITE_AUTH_DEV_BYPASS?: string | undefined;
  VITE_MSAL_CLIENT_ID?: string | undefined;
  VITE_MSAL_TENANT?: string | undefined;
  VITE_MSAL_AUTHORITY?: string | undefined;
  VITE_API_SCOPE?: string | undefined;
}

export interface MsalMode {
  kind: "msal";
  clientId: string;
  authority: string;
  scope: string;
}

export type AuthMode =
  | { kind: "dev-bypass"; reason: string }
  | MsalMode
  /** No usable sign-in configuration. A blocking screen, never a silent
   *  anonymous mode: an app that quietly stops authenticating is the failure this
   *  whole module exists to prevent. */
  | { kind: "unconfigured"; reason: string };

/** Origins where a developer convenience is allowed to apply.
 *
 *  Loopback only, by name: `localhost`, `127.0.0.1`, `[::1]` and the reserved
 *  `*.localhost` suffix. Deliberately NOT a private-range check — `10.x` and
 *  `192.168.x` are other people's machines, and a bypass that works across the
 *  office LAN is a bypass that ships. */
export function isLoopbackHost(hostname: string): boolean {
  const host = hostname.trim().toLowerCase().replace(/^\[|\]$/g, "");
  return (
    host === "localhost" ||
    host === "127.0.0.1" ||
    host === "::1" ||
    host === "0.0.0.0" ||
    host.endsWith(".localhost")
  );
}

/** Exactly the string "true". "1", "yes" and "TRUE" are not it: a flag with
 *  several spellings is a flag somebody sets by accident. */
export function devBypassRequested(env: AuthEnv): boolean {
  return env.VITE_AUTH_DEV_BYPASS === "true";
}

function msalMode(env: AuthEnv): MsalMode | null {
  const clientId = (env.VITE_MSAL_CLIENT_ID ?? "").trim();
  const tenant = (env.VITE_MSAL_TENANT ?? "").trim();
  const explicit = (env.VITE_MSAL_AUTHORITY ?? "").trim();
  const scope = (env.VITE_API_SCOPE ?? "").trim();
  if (!clientId || !scope || (!tenant && !explicit)) return null;
  // A sovereign or B2C cloud is not on login.microsoftonline.com, so an explicit
  // authority always wins over the tenant shorthand.
  const authority = explicit || `https://login.microsoftonline.com/${tenant}`;
  return { kind: "msal", clientId, authority, scope };
}

/** Which sign-in this build will actually perform, on this origin. */
export function resolveAuth(env: AuthEnv, hostname: string): AuthMode {
  const requested = devBypassRequested(env);
  const msal = msalMode(env);
  if (requested && isLoopbackHost(hostname)) {
    return {
      kind: "dev-bypass",
      reason:
        "Built with VITE_AUTH_DEV_BYPASS=true and served from a loopback origin, so no token is acquired in the browser.",
    };
  }
  if (msal) return msal;
  if (requested) {
    return {
      kind: "unconfigured",
      reason:
        `This bundle was built with VITE_AUTH_DEV_BYPASS=true, which is honoured only on a loopback origin — ` +
        `it is refused on ${hostname || "this origin"} — and no Entra ID configuration was built in to fall back to. ` +
        `Rebuild without the bypass and with VITE_MSAL_CLIENT_ID, VITE_MSAL_TENANT (or VITE_MSAL_AUTHORITY) and VITE_API_SCOPE.`,
    };
  }
  return {
    kind: "unconfigured",
    reason:
      "No sign-in is configured in this bundle. Rebuild with VITE_MSAL_CLIENT_ID, VITE_MSAL_TENANT (or VITE_MSAL_AUTHORITY) and VITE_API_SCOPE, or run locally with VITE_AUTH_DEV_BYPASS=true.",
  };
}

/** True when a bypass build is running somewhere it will not be honoured. The UI
 *  shows this as a warning rather than swallowing it, because the operator who
 *  deployed it is the only person who can fix it. */
export function bypassRefusedHere(env: AuthEnv, hostname: string): boolean {
  return devBypassRequested(env) && !isLoopbackHost(hostname);
}

export function unauthenticatedHint(mode: AuthMode): string {
  switch (mode.kind) {
    case "dev-bypass":
      return (
        "This build skips browser sign-in, but the service still refused the request. The browser flag grants " +
        "nothing on its own: start the API with its own AUTH__DEV_BYPASS=true (refused unless ENV is a known " +
        "non-production value), or run it behind authentication middleware that supplies a verified employee subject."
      );
    case "msal":
      return "Your Entra ID session has expired or the API did not accept the token. Sign in again.";
    case "unconfigured":
      return mode.reason;
  }
}
