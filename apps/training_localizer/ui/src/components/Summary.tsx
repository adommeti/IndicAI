import { progress } from "../lib/review";
import type { Summary as SummaryData } from "../lib/types";

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded border border-border bg-surface px-3 py-2">
      <div className={`text-lg font-semibold tabular-nums ${tone ?? ""}`}>{value}</div>
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
    </div>
  );
}

export function SummaryBar({ summary }: { summary: SummaryData }) {
  const pct = progress(summary);
  return (
    <div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="approved" value={`${summary.approved}/${summary.segments}`} />
        <Stat
          label="flagged"
          value={String(summary.flagged)}
          tone={summary.flagged ? "text-warn" : undefined}
        />
        <Stat
          label="locked mismatch"
          value={String(summary.locked_mismatches)}
          tone={summary.locked_mismatches ? "text-danger" : "text-ok"}
        />
        <Stat
          label="glossary misses"
          value={String(summary.glossary_misses)}
          tone={summary.glossary_misses ? "text-danger" : "text-ok"}
        />
        <Stat label="locked" value={String(summary.locked)} />
        <Stat
          label="fidelity"
          value={summary.fidelity_mean === null ? "—" : summary.fidelity_mean.toFixed(2)}
          tone={summary.fidelity_mean === null ? "text-muted" : undefined}
        />
      </div>
      <div
        className="mt-2 h-1.5 w-full overflow-hidden rounded bg-surface-2"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Segments approved"
      >
        <div className="h-full bg-ok transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
