import type { ReactNode } from "react";
import { ApiError } from "../lib/api";
import { unauthenticatedHint } from "../lib/auth";
import { ROLE_LABELS } from "../lib/roles";
import type { Role } from "../lib/types";

export function Loading({ label, rows = 3 }: { label: string; rows?: number }) {
  return (
    <div role="status" aria-live="polite" className="space-y-2 p-4">
      <p className="text-xs uppercase tracking-wider text-muted">{label}</p>
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className="h-10 animate-pulse rounded border border-border bg-surface-2"
          style={{ animationDelay: `${index * 80}ms` }}
        />
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="rounded border border-dashed border-border-strong bg-surface p-8 text-center">
      <p className="text-base font-semibold">{title}</p>
      <p className="mx-auto mt-2 max-w-prose text-sm text-muted">{body}</p>
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

/** A 403 is an ordinary, expected answer here — not a failure and not an alarm.
 *
 *  The wording is deliberate. Access is decided and applied by the server; this
 *  page is not the control, and must not be described as though hiding the panel
 *  were what keeps the data safe (.claude/rules/apps.md). */
export function RoleNotice({ what, roles }: { what: string; roles?: readonly Role[] }) {
  const named = roles?.length ? roles.map((role) => ROLE_LABELS[role] ?? role).join(", ") : null;
  return (
    <section
      aria-labelledby="role-notice-title"
      className="rounded border border-border bg-surface p-6"
      data-testid="role-notice"
    >
      <p className="text-xs font-semibold uppercase tracking-wider text-muted">Access</p>
      <h2 id="role-notice-title" className="mt-1 text-base font-semibold">
        Not available for your role
      </h2>
      <p className="mt-2 max-w-prose text-sm text-muted">
        Your role{named ? ` (${named})` : ""} does not include access to {what}. The service decides
        that and answers accordingly, so there is nothing here to show. If this is wrong for your
        job, a compliance lead can review your role assignment.
      </p>
    </section>
  );
}

/** Everything that is not a 403. Kept separate so a permission answer never
 *  shows up looking like an outage, or the other way round. */
export function ErrorPanel({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const api = error instanceof ApiError ? error : null;
  const heading =
    api?.status === 0
      ? "Could not reach the service"
      : api?.unauthenticated
        ? "Your session has expired"
        : api
          ? `Request failed (${api.status})`
          : "Something went wrong";
  const detail = error instanceof Error ? error.message : String(error);
  return (
    <section
      role="alert"
      className="rounded border border-danger-edge bg-danger-wash p-4 text-sm"
    >
      <p className="font-semibold text-danger">{heading}</p>
      <p className="mt-1 break-words font-mono text-xs text-muted">{detail}</p>
      {api?.unauthenticated ? (
        <p className="mt-2 max-w-prose text-sm">{unauthenticatedHint()}</p>
      ) : null}
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded border border-border-strong bg-surface px-3 py-1.5 text-xs font-semibold"
        >
          Try again
        </button>
      ) : null}
    </section>
  );
}

/** One place that decides whether a failure is a permission answer or a fault,
 *  so every view treats a 403 the same way. */
export function FailureState({
  error,
  what,
  roles,
  onRetry,
}: {
  error: unknown;
  what: string;
  roles?: readonly Role[];
  onRetry?: () => void;
}) {
  if (error instanceof ApiError && error.forbidden) return <RoleNotice what={what} roles={roles} />;
  return <ErrorPanel error={error} onRetry={onRetry} />;
}
