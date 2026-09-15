import type { Role } from "./types";

/** What a role's endpoints will actually answer, per the pinned contract table.
 *
 *  This is a *request plan*, not a security boundary: the server enforces every
 *  rule and returns 403 regardless of what this file says (.claude/rules/apps.md).
 *  Its only job is to stop the UI asking for something the role cannot have — an
 *  avoidable 403 is noise in the audit log and a confusing screen for the user. */
export interface Capabilities {
  /** GET /flags, /flags/{id}, /flags/{id}/audio, POST dispositions. */
  queue: boolean;
  /** GET /qa-sample — lead only. */
  qaSample: boolean;
  /** GET /metrics/* and /audit/chain_status. */
  metrics: boolean;
}

export function capabilities(roles: readonly Role[]): Capabilities {
  const lead = roles.includes("compliance_lead");
  const reviewer = lead || roles.includes("compliance_reviewer");
  const governance = roles.includes("governance");
  return {
    // `governance` SUBTRACTS, it does not add (ADR 0013). The service refuses
    // transcripts and audio to any principal carrying it, even one that also
    // carries a reviewer role, so a dual-hatted claim must not be planned as a
    // reviewer here. Getting this wrong was not a security hole -- the server
    // still answered 403 -- but the UI would land such a caller on a queue that
    // could never load, suppress the governance scope notice, and fill the
    // audit log with refusals that look like probing.
    queue: reviewer && !governance,
    qaSample: lead && !governance,
    // The contract lets a plain reviewer read /metrics/* too, but P6 assigns the
    // quality analytics to the lead and to governance; a reviewer's screen stays
    // the queue. Widening this is a product decision, not a permission change.
    metrics: lead || governance,
  };
}

export type ViewName = "queue" | "qa" | "metrics";

export interface Route {
  view: ViewName;
  flagId: string | null;
}

/** Hash routing: no router dependency for three views, and a deep link to a flag
 *  stays a link the reviewer can paste into a ticket. */
export function parseRoute(hash: string): Route {
  const clean = hash.replace(/^#\/?/, "").split("?")[0] ?? "";
  const [head, tail] = clean.split("/");
  if (head === "qa") return { view: "qa", flagId: null };
  if (head === "metrics") return { view: "metrics", flagId: null };
  return { view: "queue", flagId: tail ? decodeURIComponent(tail) : null };
}

export function routeHref(view: ViewName, flagId?: string): string {
  if (view === "queue" && flagId) return `#/queue/${encodeURIComponent(flagId)}`;
  return `#/${view}`;
}

export function allowedViews(caps: Capabilities): ViewName[] {
  const views: ViewName[] = [];
  if (caps.queue) views.push("queue");
  if (caps.qaSample) views.push("qa");
  if (caps.metrics) views.push("metrics");
  return views;
}

/** Where a role lands with no hash, and where it is sent when it deep-links into
 *  a view its role has no endpoints for. Null means the account has no view at
 *  all, which is a server-side answer we render rather than guess around. */
export function defaultView(caps: Capabilities): ViewName | null {
  return allowedViews(caps)[0] ?? null;
}

export const ROLE_LABELS: Record<Role, string> = {
  compliance_reviewer: "Compliance reviewer",
  compliance_lead: "Compliance lead",
  governance: "Governance",
};
