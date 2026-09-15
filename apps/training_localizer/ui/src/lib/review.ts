import type { Segment, Summary } from "./types";

/** The minimum the API accepts. Mirrored here so the dialog can disable its own
 *  submit button rather than round-tripping to be told no; the API is still the
 *  one that enforces it. */
export const MIN_OVERRIDE_REASON = 10;

export type Filter = "all" | "flagged" | "unapproved";

export function applyFilter(segments: Segment[], filter: Filter): Segment[] {
  switch (filter) {
    case "flagged":
      return segments.filter((s) => s.flagged);
    case "unapproved":
      return segments.filter((s) => !s.approved);
    default:
      return segments;
  }
}

/** Segments "approve all remaining" would touch: not yet approved, and not a
 *  locked mismatch. A bulk action must never be the thing that sets aside a
 *  compliance statement -- those stay for the reviewer to handle one at a time
 *  with a reason. */
export function bulkApprovable(segments: Segment[]): Segment[] {
  return segments.filter((s) => !s.approved && !s.locked_mismatch);
}

export function blockedFromBulk(segments: Segment[]): Segment[] {
  return segments.filter((s) => !s.approved && s.locked_mismatch);
}

export function needsOverride(segment: Segment, text: string): boolean {
  return segment.locked && segment.locked_id !== null && text.trim() !== "" && segment.locked_mismatch
    ? true
    : false;
}

export function timecode(ms: number): string {
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function progress(summary: Summary): number {
  return summary.segments === 0 ? 0 : Math.round((summary.approved / summary.segments) * 100);
}

/** Recompute the summary client-side after an approval, so the header updates
 *  without another round trip. Kept in one place and unit-tested because a
 *  drifting count is exactly the kind of thing a reviewer stops trusting. */
export function summarise(segments: Segment[]): Summary {
  const scored = segments.filter((s) => s.qa_score !== null);
  return {
    segments: segments.length,
    approved: segments.filter((s) => s.approved).length,
    flagged: segments.filter((s) => s.flagged).length,
    locked: segments.filter((s) => s.locked).length,
    locked_mismatches: segments.filter((s) => s.locked_mismatch).length,
    glossary_misses: segments.reduce((n, s) => n + s.glossary_misses.length, 0),
    fidelity_mean:
      scored.length === 0
        ? null
        : scored.reduce((n, s) => n + (s.qa_score ?? 0), 0) / scored.length,
  };
}
