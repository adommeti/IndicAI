import type { Language, OverrideRequired, ReviewPayload } from "./types";

/** Roles and identity come from the API, never from the client (.claude/rules/ui.md).
 *  In dev bypass the browser sends nothing special and the server supplies a
 *  fixed test identity; in production the SSO middleware populates it. */
const BASE = import.meta.env.VITE_API_BASE ?? "/api";

export class LockedOverrideRequired extends Error {
  constructor(public detail: OverrideRequired) {
    super(detail.hint);
    this.name = "LockedOverrideRequired";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    credentials: "include",
  });
  if (response.status === 409) {
    const body = await response.json();
    const detail = (body.detail ?? body) as OverrideRequired;
    if (detail?.error === "locked_override_required") throw new LockedOverrideRequired(detail);
  }
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text.slice(0, 400)}`);
  }
  return (await response.json()) as T;
}

export const api = {
  review: (moduleId: string, language: Language) =>
    request<ReviewPayload>(`/modules/${moduleId}/review?language=${language}`),

  approve: (
    moduleId: string,
    segId: number,
    body: { language: Language; text: string; override_reason?: string },
  ) =>
    request<{ version: number; locked_override: boolean }>(
      `/modules/${moduleId}/segments/${segId}/approve`,
      { method: "PUT", body: JSON.stringify(body) },
    ),

  approveQuiz: (moduleId: string, itemId: number, language: string, approved: boolean) =>
    request<{ approved: boolean }>(`/modules/${moduleId}/quiz/${itemId}/approve`, {
      method: "PUT",
      body: JSON.stringify({ language, approved }),
    }),

  status: (moduleId: string) => request<Record<string, unknown>>(`/modules/${moduleId}/status`),
};
