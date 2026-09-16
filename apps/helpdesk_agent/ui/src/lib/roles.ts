import type { Role } from "./types";

/** What this account's endpoints will answer, per the pinned uc1/P6 contract.
 *
 *  This is a *navigation plan*, not a security boundary. The server decides every
 *  question of access and answers 403 on its own account
 *  (`helpdesk_agent/roles.py: enforce_replay`, and `.claude/rules/apps.md`: "UI
 *  hiding is not access control"). All this does is decide which tab to draw, so an
 *  employee is not shown a panel that can only ever be a refusal.
 *
 *  The distinction is not academic here. The replay view is reachable by URL
 *  whether or not its tab is drawn: deep-linking to `#/replay` asks the API and
 *  renders the answer, refusal included. If hiding the tab were doing the work,
 *  that link would be a hole — so it is deliberately not doing the work, and the
 *  e2e spec drives exactly that path.
 *
 *  `governance` here is uc1's role and grants the full session replay. The word
 *  means the opposite in uc3, where it SUBTRACTS content access (ADR 0013). uc1
 *  does not import, mirror or consult uc3's role handling, and a deployment must
 *  map the two names to two different Entra groups — see `helpdesk_agent/roles.py`.
 */
export interface Capabilities {
  /** GET /sessions/{id}/replay. */
  replay: boolean;
}

export function capabilities(roles: readonly Role[]): Capabilities {
  return { replay: roles.includes("governance") };
}

export type ViewName = "chat" | "replay";

export interface Route {
  view: ViewName;
  /** The session id in a `#/replay/<id>` deep link. */
  sessionId: string | null;
}

/** Hash routing: no router dependency for two views, and a deep link to a session
 *  replay stays a link an auditor can paste into a ticket. */
export function parseRoute(hash: string): Route {
  const clean = hash.replace(/^#\/?/, "").split("?")[0] ?? "";
  const [head, tail] = clean.split("/");
  if (head === "replay")
    return { view: "replay", sessionId: tail ? decodeURIComponent(tail) : null };
  return { view: "chat", sessionId: null };
}

export function routeHref(view: ViewName, sessionId?: string): string {
  if (view === "replay" && sessionId) return `#/replay/${encodeURIComponent(sessionId)}`;
  return `#/${view}`;
}

/** Tabs to draw. The chat view is every employee's, including one holding no role
 *  at all — an employee with no grant is the normal case for this app. */
export function allowedViews(caps: Capabilities): ViewName[] {
  return caps.replay ? ["chat", "replay"] : ["chat"];
}

export const ROLE_LABELS: Record<Role, string> = {
  governance: "Governance (uc1 replay)",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role as Role] ?? role;
}
