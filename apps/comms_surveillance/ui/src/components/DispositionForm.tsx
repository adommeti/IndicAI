import { useRef, useState } from "react";
import { api } from "../lib/api";
import {
  DISPOSITION_OPTIONS,
  MAX_NOTE,
  newIdempotencyKey,
  noteAdvice,
  noteProblem,
} from "../lib/queue";
import type { Disposition, DispositionReceipt, DispositionRow } from "../lib/types";
import { FailureState } from "./States";

/** The ruling.
 *
 *  Append-only: this form never edits anything. A changed mind is a new row, the
 *  history below keeps every earlier one, and the receipt shows the chain
 *  sequence the server assigned — so a reviewer can see their ruling actually
 *  joined the hash chain rather than taking it on trust. */
export function DispositionForm({
  flagId,
  current,
  onRecorded,
}: {
  flagId: string;
  current: Disposition | null;
  onRecorded: (row: DispositionRow, receipt: DispositionReceipt) => void;
}) {
  const [disposition, setDisposition] = useState<Disposition>(current ?? "confirmed");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const intentKey = useRef<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [receipt, setReceipt] = useState<DispositionReceipt | null>(null);

  const problem = noteProblem(note);
  const advice = noteAdvice(note);
  const selected = DISPOSITION_OPTIONS.find((option) => option.value === disposition);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (problem) return;
    setBusy(true);
    setError(null);
    try {
      // Minted per INTENT, not per request: the key is kept in a ref so a retry after
      // a dropped connection sends the same one and is answered with the original
      // receipt, instead of appending a second permanent ruling to a table nobody can
      // correct. Cleared only after a ruling is recorded, so the next decision on this
      // flag gets a new key. Disabling the button while in flight -- the only guard
      // before this -- does nothing about a refresh or a proxy retry.
      if (!intentKey.current) intentKey.current = newIdempotencyKey();
      const next = await api.disposition(flagId, { disposition, note }, intentKey.current);
      intentKey.current = null;
      setReceipt(next);
      setNote("");
      onRecorded(
        {
          disposition,
          note,
          reviewer_id: "you",
          created_at: new Date().toISOString(),
        },
        next,
      );
    } catch (cause) {
      setError(cause);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={submit}
      aria-labelledby="disposition-heading"
      className="rounded border border-border bg-surface p-4"
      data-testid="disposition-form"
    >
      <h3 id="disposition-heading" className="text-sm font-semibold">
        Record a disposition
      </h3>
      <p className="mt-1 text-xs text-muted">
        {current
          ? "This flag already has a ruling. Recording another adds a row; it does not replace the last one."
          : "Rulings are appended to the audit chain and cannot be edited or deleted."}
      </p>

      <fieldset className="mt-3">
        <legend className="sr-only">Disposition</legend>
        <div className="grid gap-1.5 sm:grid-cols-2">
          {DISPOSITION_OPTIONS.map((option) => (
            <label
              key={option.value}
              className={`flex cursor-pointer items-start gap-2 rounded border p-2 text-sm ${
                disposition === option.value
                  ? "border-accent bg-surface-2"
                  : "border-border hover:bg-surface-2"
              }`}
            >
              <input
                type="radio"
                name="disposition"
                value={option.value}
                checked={disposition === option.value}
                onChange={() => setDisposition(option.value)}
                data-testid={`disposition-${option.value}`}
                className="mt-0.5 accent-[color:var(--accent)]"
              />
              <span className="min-w-0">
                <span className="block font-medium">{option.label}</span>
                <span className="mt-0.5 block font-mono text-[10px] uppercase tracking-wide text-muted">
                  {option.decided ? "counts in precision" : "not a decided outcome"}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      {selected ? <p className="mt-2 text-xs text-muted">{selected.hint}</p> : null}

      <div className="mt-3">
        <label htmlFor="note" className="block text-xs font-semibold uppercase tracking-wider">
          Note
        </label>
        <textarea
          id="note"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          rows={4}
          maxLength={MAX_NOTE + 1}
          data-testid="note"
          placeholder="What did you check, and what decided it?"
          className="mt-1 w-full rounded border border-border bg-bg p-2 text-sm"
        />
        <p className="mt-1 flex justify-between gap-3 text-xs text-muted">
          <span aria-live="polite">{problem ?? advice ?? ""}</span>
          <span className="font-mono shrink-0">
            {note.length}/{MAX_NOTE}
          </span>
        </p>
      </div>

      {error ? (
        <div className="mt-3">
          <FailureState error={error} what="recording a disposition" />
        </div>
      ) : null}

      {receipt ? (
        <p
          className="mt-3 rounded border border-ok-edge bg-ok-wash p-2 font-mono text-xs text-ok"
          role="status"
          data-testid="receipt"
        >
          Recorded · seq {receipt.seq} · row {receipt.row_hash.slice(0, 12)}…
        </p>
      ) : null}

      <button
        type="submit"
        disabled={busy || problem !== null}
        className="mt-3 rounded bg-accent px-4 py-2 text-sm font-semibold text-[color:var(--accent-ink)] disabled:opacity-50"
        data-testid="submit-disposition"
      >
        {busy ? "Recording…" : "Record disposition"}
      </button>
    </form>
  );
}

export function DispositionHistory({ rows }: { rows: readonly DispositionRow[] }) {
  return (
    <section aria-labelledby="history-heading" className="rounded border border-border bg-surface p-4">
      <h3 id="history-heading" className="text-sm font-semibold">
        Disposition history
      </h3>
      {rows.length === 0 ? (
        <p className="mt-2 text-sm text-muted">
          No ruling yet. This flag is still open.
        </p>
      ) : (
        <ol className="mt-2 divide-y divide-border" data-testid="history">
          {rows.map((row, index) => (
            <li key={`${row.created_at}-${index}`} className="py-2">
              <p className="flex flex-wrap items-baseline gap-2">
                <span className="text-sm font-semibold">
                  {DISPOSITION_OPTIONS.find((option) => option.value === row.disposition)?.label ??
                    row.disposition}
                </span>
                {index === 0 ? (
                  <span className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide">
                    current
                  </span>
                ) : (
                  <span className="font-mono text-[10px] uppercase tracking-wide text-muted">
                    superseded
                  </span>
                )}
                <span className="ml-auto font-mono text-[11px] text-muted">
                  {row.reviewer_id} · {row.created_at}
                </span>
              </p>
              {row.note ? <p className="mt-1 whitespace-pre-wrap text-sm">{row.note}</p> : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
