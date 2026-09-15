import { useCallback, useRef } from "react";
import { SEVERITIES, dispositionLabel, queueCounts, shortId, timecode } from "../lib/queue";
import type { QueueFilter } from "../lib/queue";
import { routeHref } from "../lib/roles";
import type { FlagSummary, Severity } from "../lib/types";
import { SeverityRule, SeverityTag } from "./Severity";
import { EmptyState } from "./States";

function Counts({ flags }: { flags: readonly FlagSummary[] }) {
  const counts = queueCounts(flags);
  return (
    <p className="px-3 pb-2 font-mono text-xs text-muted" data-testid="queue-counts">
      {counts.total} flags · {counts.high} high · {counts.medium} med · {counts.low} low ·{" "}
      <span className="font-semibold text-ink">{counts.open} open</span>
    </p>
  );
}

export function QueueFilters({
  filter,
  onChange,
}: {
  filter: QueueFilter;
  onChange: (next: QueueFilter) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-border px-3 py-2">
      <div className="flex rounded border border-border" role="group" aria-label="Filter severity">
        {(["", ...SEVERITIES] as (Severity | "")[]).map((value) => (
          <button
            key={value || "all"}
            type="button"
            onClick={() => onChange({ ...filter, severity: value })}
            aria-pressed={filter.severity === value}
            data-testid={`severity-${value || "all"}`}
            className={`px-2.5 py-1 text-xs uppercase tracking-wide first:rounded-l last:rounded-r ${
              filter.severity === value
                ? "bg-accent font-semibold text-[color:var(--accent-ink)]"
                : "text-muted"
            }`}
          >
            {value || "all"}
          </button>
        ))}
      </div>
      <label className="flex items-center gap-2 text-xs text-muted">
        <input
          type="checkbox"
          checked={filter.undispositioned}
          onChange={(event) => onChange({ ...filter, undispositioned: event.target.checked })}
          data-testid="filter-open"
          className="h-4 w-4 accent-[color:var(--accent)]"
        />
        Open only
      </label>
    </div>
  );
}

/** The queue. Rows are links so a flag is deep-linkable into a ticket, and
 *  Arrow Up/Down walk the list without touching the mouse — the whole day of a
 *  reviewer is this list. */
export function FlagQueue({
  flags,
  selectedId,
  label = "Review queue",
}: {
  flags: readonly FlagSummary[];
  selectedId: string | null;
  label?: string;
}) {
  const listRef = useRef<HTMLUListElement>(null);

  const onKeyDown = useCallback((event: React.KeyboardEvent<HTMLUListElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const links = Array.from(listRef.current?.querySelectorAll<HTMLAnchorElement>("a[data-flag]") ?? []);
    if (links.length === 0) return;
    const index = links.indexOf(document.activeElement as HTMLAnchorElement);
    const next = event.key === "ArrowDown" ? index + 1 : index - 1;
    const target = links[Math.max(0, Math.min(links.length - 1, next < 0 ? 0 : next))];
    if (target) {
      event.preventDefault();
      target.focus();
    }
  }, []);

  if (flags.length === 0)
    return (
      <div className="p-4">
        <EmptyState
          title="Nothing in the queue"
          body="No flag matches this filter. Widen the severity filter, or clear “open only” to see flags that have already been dispositioned."
        />
      </div>
    );

  return (
    <>
      <Counts flags={flags} />
      <ul
        ref={listRef}
        onKeyDown={onKeyDown}
        aria-label={label}
        className="divide-y divide-border border-t border-border"
        data-testid="queue"
      >
        {flags.map((flag) => {
          const selected = flag.flag_id === selectedId;
          return (
            <li key={flag.flag_id}>
              <a
                href={routeHref("queue", flag.flag_id)}
                data-flag={flag.flag_id}
                aria-current={selected ? "true" : undefined}
                className={`flex items-stretch gap-3 px-3 py-2.5 ${
                  selected ? "bg-surface-2" : "hover:bg-surface-2"
                }`}
              >
                <SeverityRule severity={flag.severity} />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center justify-between gap-2">
                    <SeverityTag severity={flag.severity} />
                    <span
                      className={`font-mono text-[11px] uppercase tracking-wide ${
                        flag.disposition === null ? "font-semibold text-ink" : "text-muted"
                      }`}
                    >
                      {dispositionLabel(flag.disposition)}
                    </span>
                  </span>
                  <span className="mt-0.5 block truncate text-sm font-medium">{flag.category}</span>
                  <span className="mt-0.5 block font-mono text-[11px] text-muted">
                    {flag.speaker} · call {shortId(flag.call_id)} · {timecode(flag.start_ms)}
                  </span>
                </span>
              </a>
            </li>
          );
        })}
      </ul>
    </>
  );
}
