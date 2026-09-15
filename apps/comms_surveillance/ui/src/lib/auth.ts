/** Sign-in.
 *
 *  Identity is established in front of this app, not inside it: the pinned uc3/P6
 *  contract puts the SSO claim on the request (`request.scope["auth"]`) and has
 *  GET /me report the verified subject and its roles. So the browser holds no
 *  token, parses no claim and grants nothing; it sends credentials and asks /me.
 *
 *  There is consequently NO MSAL redirect wired up here. If this app is ever
 *  deployed without the middleware in front of it, sign-in has to be added — it
 *  is not silently working today, and a 401 says so in those words rather than
 *  looking like an outage.
 */

/** Mirrors the API's AUTH__DEV_BYPASS for local work. It enables nothing in the
 *  browser — the server decides whether the bypass applies and refuses it when
 *  ENV=prod — and is used only to explain a 401 accurately. */
export function devBypassExpected(): boolean {
  return import.meta.env.VITE_AUTH_DEV_BYPASS === "true";
}

export function unauthenticatedHint(): string {
  return devBypassExpected()
    ? "VITE_AUTH_DEV_BYPASS is set for this build, but the service still refused the request. Start the API with AUTH__DEV_BYPASS=true, or sign in through the identity provider in front of it."
    : "Sign-in is handled by the identity provider in front of this service. Reload the page to authenticate again.";
}
