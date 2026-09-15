import type {
  AudioGrant,
  ChainStatus,
  Disposition,
  DispositionReceipt,
  FalseNegativeEstimate,
  FlagDetail,
  FlagSummary,
  Me,
  PrecisionMetrics,
  QaFlag,
  Severity,
} from "./types";

/** Same-origin by default: the FastAPI app mounts this bundle at "/", so the
 *  contract paths are reachable unprefixed. Overridable for a split deployment. */
const BASE = import.meta.env.VITE_API_BASE ?? "";

/** Every non-2xx carries its status, because 403 is a first-class, expected
 *  outcome here and not an error to shout about: a governance user simply has no
 *  transcript access. The server decides that; this client only reports it. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  get forbidden(): boolean {
    return this.status === 403;
  }

  get unauthenticated(): boolean {
    return this.status === 401;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
      credentials: "include",
    });
  } catch (cause) {
    // A transport failure is not a permission failure; keep them distinguishable.
    throw new ApiError(0, path, cause instanceof Error ? cause.message : String(cause));
  }
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new ApiError(response.status, path, text.slice(0, 400) || response.statusText);
  }
  return (await response.json()) as T;
}

export interface FlagQuery {
  severity?: Severity | "";
  undispositioned?: boolean;
}

function flagsQuery(query: FlagQuery): string {
  const params = new URLSearchParams();
  if (query.severity) params.set("severity", query.severity);
  if (query.undispositioned) params.set("undispositioned", "true");
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export const api = {
  /** Identity and roles. The ONLY source of role information in this app —
   *  nothing is ever read from a token, a cookie or a query string in the browser. */
  me: () => request<Me>("/me"),

  flags: (query: FlagQuery = {}) =>
    request<{ flags: FlagSummary[] }>(`/flags${flagsQuery(query)}`).then((body) => body.flags),

  flag: (flagId: string) => request<FlagDetail>(`/flags/${encodeURIComponent(flagId)}`),

  /** Issues a fresh short-lived grant every time it is called. The URL is held in
   *  component state for the life of the player and is deliberately never written
   *  to storage, the address bar, or a console line. */
  audio: (flagId: string) =>
    request<AudioGrant>(`/flags/${encodeURIComponent(flagId)}/audio`),

  disposition: (flagId: string, body: { disposition: Disposition; note: string }) =>
    request<DispositionReceipt>(`/flags/${encodeURIComponent(flagId)}/dispositions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  qaSample: () => request<{ items: QaFlag[] }>("/qa-sample").then((body) => body.items),

  precision: () => request<PrecisionMetrics>("/metrics/precision"),

  falseNegatives: () => request<FalseNegativeEstimate>("/metrics/false_negative_estimate"),

  chainStatus: () => request<ChainStatus>("/audit/chain_status"),
};
