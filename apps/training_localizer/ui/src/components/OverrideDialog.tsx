import { useEffect, useRef, useState } from "react";
import { MIN_OVERRIDE_REASON } from "../lib/review";

export function OverrideDialog({
  lockedId,
  expected,
  onCancel,
  onConfirm,
}: {
  lockedId: string;
  expected: string;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => field.current?.focus(), []);
  const ok = reason.trim().length >= MIN_OVERRIDE_REASON;

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="override-title"
      onKeyDown={(event) => event.key === "Escape" && onCancel()}
    >
      <div className="w-full max-w-xl rounded-lg border border-border bg-surface p-6 shadow-xl">
        <h2 id="override-title" className="text-base font-semibold text-danger">
          Overriding a LOCKED compliance statement
        </h2>
        <p className="mt-2 text-sm text-muted">
          <code className="font-mono">{lockedId}</code> has an approved rendering. Saving different
          text sets aside a control, so the reason is recorded against your name.
        </p>
        <p className="mt-3 text-xs font-semibold uppercase tracking-wide text-muted">
          Approved rendering
        </p>
        <p className="indic mt-1 rounded bg-surface-2 p-3 text-ink">{expected}</p>
        <label htmlFor="override-reason" className="mt-4 block text-sm font-medium">
          Why must this segment differ?
        </label>
        <textarea
          id="override-reason"
          ref={field}
          rows={3}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          className="mt-1 w-full rounded border border-border bg-surface p-2 text-sm"
          placeholder="e.g. Legal approved revised wording on 2026-09-15, ticket LEG-441"
        />
        <p className="mt-1 text-xs text-muted">
          {ok
            ? "Recorded with your reviewer id and the glossary version."
            : `At least ${MIN_OVERRIDE_REASON} characters.`}
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded border border-border px-3 py-1.5 text-sm"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!ok}
            onClick={() => onConfirm(reason.trim())}
            className="rounded bg-danger px-3 py-1.5 text-sm font-semibold text-white disabled:opacity-40"
          >
            Override and approve
          </button>
        </div>
      </div>
    </div>
  );
}
