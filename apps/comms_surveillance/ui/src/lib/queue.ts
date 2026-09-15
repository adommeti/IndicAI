import type { Disposition, FlagSummary, Severity } from "./types";

export const SEVERITIES: Severity[] = ["high", "medium", "low"];

const SEVERITY_RANK: Record<Severity, number> = { high: 0, medium: 1, low: 2 };

export function severityRank(severity: Severity): number {
  return SEVERITY_RANK[severity] ?? 99;
}

/** The server already returns the queue in this order. Re-applying it here is
 *  cheap and keeps the list stable after a local edit (a disposition posted from
 *  the detail pane must not shuffle the row out from under the cursor). */
export function sortQueue<T extends FlagSummary>(flags: readonly T[]): T[] {
  return [...flags].sort(
    (a, b) =>
      severityRank(a.severity) - severityRank(b.severity) ||
      a.created_at.localeCompare(b.created_at) ||
      a.flag_id.localeCompare(b.flag_id),
  );
}

export interface QueueFilter {
  severity: Severity | "";
  undispositioned: boolean;
}

export const EMPTY_FILTER: QueueFilter = { severity: "", undispositioned: false };

/** Mirrors the server's ?severity=&undispositioned= narrowing. The fetch still
 *  sends those params; this runs locally so a just-posted disposition leaves the
 *  "undispositioned only" list immediately instead of after a refetch. */
export function filterQueue<T extends FlagSummary>(
  flags: readonly T[],
  filter: QueueFilter,
): T[] {
  return flags.filter(
    (flag) =>
      (!filter.severity || flag.severity === filter.severity) &&
      (!filter.undispositioned || flag.disposition === null),
  );
}

export interface QueueCounts {
  total: number;
  high: number;
  medium: number;
  low: number;
  open: number;
}

export function queueCounts(flags: readonly FlagSummary[]): QueueCounts {
  return {
    total: flags.length,
    high: flags.filter((f) => f.severity === "high").length,
    medium: flags.filter((f) => f.severity === "medium").length,
    low: flags.filter((f) => f.severity === "low").length,
    open: flags.filter((f) => f.disposition === null).length,
  };
}

/** Patch one row after a disposition is accepted. Append-only on the server: the
 *  summary only ever carries the LATEST ruling, so the row is replaced, not
 *  removed, and the history in the detail pane keeps every earlier one. */
export function applyDisposition<T extends FlagSummary>(
  flags: readonly T[],
  flagId: string,
  disposition: Disposition,
): T[] {
  return flags.map((flag) => (flag.flag_id === flagId ? { ...flag, disposition } : flag));
}

export function nextFlagId(flags: readonly FlagSummary[], current: string | null): string | null {
  if (flags.length === 0) return null;
  const index = flags.findIndex((f) => f.flag_id === current);
  if (index < 0) return flags[0]?.flag_id ?? null;
  return flags[index + 1]?.flag_id ?? null;
}

export function timecode(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  return `${h > 0 ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`;
}

export function shortId(value: string, keep = 8): string {
  return value.length <= keep ? value : `${value.slice(0, keep)}…`;
}

export interface DispositionOption {
  value: Disposition;
  label: string;
  /** Only confirmed / false_positive are decided outcomes: the other two are
   *  excluded from both the numerator and the denominator of precision. */
  decided: boolean;
  hint: string;
}

export const DISPOSITION_OPTIONS: DispositionOption[] = [
  {
    value: "confirmed",
    label: "Confirmed",
    decided: true,
    hint: "A real breach of the cited clause. Counts toward precision.",
  },
  {
    value: "false_positive",
    label: "False positive",
    decided: true,
    hint: "The detector was wrong. Counts against precision.",
  },
  {
    value: "needs_more_context",
    label: "Needs more context",
    decided: false,
    hint: "Undecided. Excluded from precision entirely — not a pass and not a fail.",
  },
  {
    value: "escalated",
    label: "Escalated",
    decided: false,
    hint: "Handed to the lead or to legal. Excluded from precision entirely.",
  },
];

export function dispositionLabel(value: Disposition | null): string {
  if (value === null) return "Open";
  return DISPOSITION_OPTIONS.find((option) => option.value === value)?.label ?? value;
}

export const MAX_NOTE = 4000;

/** The only hard client-side rule is the cap the contract states, mirrored so the
 *  form can refuse before spending a round trip and an audit row on a note the
 *  server would reject. The server remains the one that enforces it. */
export function noteProblem(note: string): string | null {
  if (note.length > MAX_NOTE) return `Notes are capped at ${MAX_NOTE} characters.`;
  return null;
}

/** Advisory, never blocking: the contract does not require a note, but a
 *  permanent chain row with no rationale is the one a later audit cannot read. */
export function noteAdvice(note: string): string | null {
  return note.trim().length === 0
    ? "No note. This row is permanent and append-only — a later reader will have only the disposition."
    : null;
}
