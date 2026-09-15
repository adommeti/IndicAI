import type { Segment } from "../lib/types";

export function LockedBadge({ segment }: { segment: Segment }) {
  if (!segment.locked) return null;
  const bad = segment.locked_mismatch;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide ${
        bad ? "bg-danger/15 text-danger ring-1 ring-danger/40" : "bg-ok/15 text-ok ring-1 ring-ok/40"
      }`}
      title={
        bad
          ? `LOCKED: this does not match the approved rendering for ${segment.locked_id}`
          : `LOCKED: matches the approved rendering for ${segment.locked_id}`
      }
    >
      <span aria-hidden="true">{bad ? "▲" : "▼"}</span>
      {bad ? "LOCKED — mismatch" : "LOCKED"}
    </span>
  );
}

export function ScoreBadge({ score }: { score: number | null }) {
  if (score === null)
    return (
      <span className="text-[11px] text-muted" title="The fidelity judge has not run">
        unmeasured
      </span>
    );
  const tone = score <= 2 ? "text-danger" : score < 4 ? "text-warn" : "text-ok";
  return (
    <span className={`font-mono text-sm font-semibold ${tone}`} title={`Fidelity ${score} of 5`}>
      {score}/5
    </span>
  );
}

export function GlossaryChips({ segment }: { segment: Segment }) {
  if (segment.glossary_hits.length === 0 && segment.glossary_misses.length === 0)
    return <span className="text-[11px] text-muted">—</span>;
  return (
    <div className="flex flex-wrap gap-1">
      {segment.glossary_misses.map((term) => (
        <span
          key={`miss-${term}`}
          className="rounded bg-danger/15 px-1.5 py-0.5 text-[11px] font-medium text-danger ring-1 ring-danger/30"
          title="Required by the glossary and missing from this translation"
        >
          {term}
        </span>
      ))}
      {segment.glossary_hits.map((term) => (
        <span
          key={`hit-${term}`}
          className="rounded bg-surface-2 px-1.5 py-0.5 text-[11px] text-muted"
          title="Glossary term present"
        >
          {term}
        </span>
      ))}
    </div>
  );
}
