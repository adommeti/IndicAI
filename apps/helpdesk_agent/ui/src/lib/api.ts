import type { ChatTurnResponse, LanguageTag, Me, Replay } from "./types";

/** Same-origin by default: the FastAPI app serves this bundle from "/", so the
 *  contract paths are reachable unprefixed. Overridable for a split deployment. */
const BASE = import.meta.env.VITE_API_BASE ?? "";

/** Every non-2xx carries its status, because 403 is a first-class, expected
 *  outcome here and not an error to shout about: an employee without uc1's
 *  `governance` role simply cannot replay a session. The server decides that;
 *  this client only reports it. */
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

  get missing(): boolean {
    return this.status === 404;
  }
}

/** Supplies the `Authorization` header, when this build signs in with MSAL.
 *  Installed once by `App`; defaults to nothing so a module imported in a test
 *  never reaches for a token. */
let headers: () => Promise<Record<string, string>> = async () => ({});

export function setAuthHeaders(provider: () => Promise<Record<string, string>>): void {
  headers = provider;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const auth = await headers();
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...auth, ...(init?.headers ?? {}) },
      // Cookie-session deployments (SSO middleware in front) need this; a bearer
      // deployment ignores it.
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

export const api = {
  /** Identity and roles. The ONLY source of role information in this app — no
   *  token is parsed, no role is read from a URL or a cookie in the browser. */
  me: () => request<Me>("/me"),

  chatTurn: (body: { session_id?: string; utterance: string; language: LanguageTag }) =>
    request<ChatTurnResponse>("/chat/turn", { method: "POST", body: JSON.stringify(body) }),

  /** 403 without uc1's replay role — for a session that does not exist too, so
   *  the endpoint cannot be used to find out which session ids are real. The UI
   *  never pre-judges that: it asks, and renders whatever the server answers. */
  replay: (sessionId: string) =>
    request<Replay>(`/sessions/${encodeURIComponent(sessionId)}/replay`),
};

export { request as rawRequest };
