import type { AuthMode, MsalMode } from "./auth";

/** The MSAL half of sign-in, loaded only when it is actually used.
 *
 *  `@azure/msal-browser` is imported dynamically so that (a) a dev-bypass build
 *  never pays for it, and (b) the unit tests — which exercise the decision in
 *  `auth.ts`, not Microsoft's library — never have to stand it up in jsdom.
 *
 *  The browser holds an access token for the API's exposed scope and nothing else.
 *  It does not read the token, does not parse a claim out of it, and never derives
 *  a role from it: roles come from `GET /me` (`.claude/rules/ui.md`). Whatever this
 *  file believes about the user, the API decides.
 */

type Client = import("@azure/msal-browser").PublicClientApplication;
type Account = import("@azure/msal-browser").AccountInfo;

let client: Client | null = null;

async function instance(mode: MsalMode): Promise<Client> {
  if (client) return client;
  const msal = await import("@azure/msal-browser");
  const app = new msal.PublicClientApplication({
    auth: {
      clientId: mode.clientId,
      authority: mode.authority,
      redirectUri: window.location.origin,
      // The SPA lands back on itself; navigating to the hash it was on when it
      // left would re-run a deep link before /me has answered.
      navigateToLoginRequestUrl: false,
    },
    cache: {
      // Session storage, not local: the token dies with the tab. On a shared
      // helpdesk workstation the next person gets a sign-in prompt, not the
      // previous employee's session.
      cacheLocation: "sessionStorage",
      storeAuthStateInCookie: false,
    },
  });
  await app.initialize();
  await app.handleRedirectPromise();
  client = app;
  return app;
}

function activeAccount(app: Client): Account | null {
  return app.getActiveAccount() ?? app.getAllAccounts()[0] ?? null;
}

/** An `Authorization` header for the API, or `{}` when this build does not use
 *  MSAL. A redirect (which never returns) is the deliberate outcome of having no
 *  account yet — the caller must treat this as possibly navigating away. */
export async function authHeader(mode: AuthMode): Promise<Record<string, string>> {
  if (mode.kind !== "msal") return {};
  const app = await instance(mode);
  const account = activeAccount(app);
  if (!account) {
    await app.loginRedirect({ scopes: [mode.scope] });
    return {};
  }
  app.setActiveAccount(account);
  try {
    const result = await app.acquireTokenSilent({ account, scopes: [mode.scope] });
    return { authorization: `Bearer ${result.accessToken}` };
  } catch {
    // Consent, MFA, a revoked session: all of them need the user, and none of
    // them is something to retry silently.
    await app.acquireTokenRedirect({ account, scopes: [mode.scope] });
    return {};
  }
}

/** The signed-in account's username, for the header. Display only — the identity
 *  that matters is the `employee_id` the API reports from the verified claim, and
 *  that is what the app keys anything on. */
export async function signedInName(mode: AuthMode): Promise<string | null> {
  if (mode.kind !== "msal") return null;
  const app = await instance(mode);
  return activeAccount(app)?.username ?? null;
}

export async function signOut(mode: AuthMode): Promise<void> {
  if (mode.kind !== "msal") return;
  const app = await instance(mode);
  const account = activeAccount(app);
  await app.logoutRedirect(account ? { account } : undefined);
}
