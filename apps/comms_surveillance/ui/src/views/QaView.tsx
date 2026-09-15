import { useCallback, useState } from "react";
import { FlagDetailPane } from "../components/FlagDetail";
import { FlagQueue } from "../components/FlagQueue";
import { FailureState, Loading } from "../components/States";
import { api } from "../lib/api";
import { applyDisposition, queueCounts, sortQueue } from "../lib/queue";
import type { ScriptPref } from "../lib/script";
import type { QaFlag, Role } from "../lib/types";
import { useAsync } from "../lib/useAsync";

/** Lead only: the random QA sample.
 *
 *  This is not a second queue to clear — it is the sample the false-negative
 *  estimate and the precision numbers are built from, so it is deliberately not
 *  filterable. Choosing which sampled calls to look at would bias the estimate
 *  the sample exists to produce. */
export function QaView({ scriptPref, roles }: { scriptPref: ScriptPref; roles: readonly Role[] }) {
  const load = useCallback(() => api.qaSample().then(sortQueue), []);
  const { data, error, loading, reload, patch } = useAsync<QaFlag[]>(load);
  const [selected, setSelected] = useState<string | null>(null);

  const counts = data ? queueCounts(data) : null;

  return (
    <div className="grid gap-4 lg:grid-cols-[22rem_1fr]">
      <section
        aria-label="QA sample"
        className="h-fit overflow-hidden rounded border border-border bg-surface lg:sticky lg:top-4 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto"
      >
        <div className="border-b border-border px-3 py-2">
          <h2 className="text-sm font-semibold">Random QA sample</h2>
          <p className="mt-1 text-xs text-muted">
            Drawn at random by the service. Not filterable on purpose: picking which sampled items to
            review would bias the estimate the sample feeds.
          </p>
          {counts ? (
            <p className="mt-1 font-mono text-xs text-muted">
              {counts.total} sampled · {counts.open} not yet ruled on
            </p>
          ) : null}
        </div>
        {loading ? <Loading label="Loading sample" rows={5} /> : null}
        {error ? (
          <div className="p-3">
            <FailureState error={error} what="the random QA sample" roles={roles} onRetry={reload} />
          </div>
        ) : null}
        {data && !loading && !error ? (
          <div
            onClickCapture={(event) => {
              const link = (event.target as HTMLElement).closest<HTMLAnchorElement>("a[data-flag]");
              if (!link) return;
              // Stay on the QA view rather than navigating into the main queue:
              // the sample is its own stream and losing it mid-pass costs the lead
              // the place in a sample they cannot re-draw.
              event.preventDefault();
              setSelected(link.dataset.flag ?? null);
            }}
          >
            <FlagQueue flags={data} selectedId={selected} label="QA sample" />
          </div>
        ) : null}
      </section>

      <section aria-label="Sampled flag" className="min-w-0">
        {selected ? (
          <FlagDetailPane
            flagId={selected}
            scriptPref={scriptPref}
            roles={roles}
            onDisposition={(id, disposition) => {
              if (!data || disposition === null) return;
              patch(applyDisposition(data, id, disposition));
            }}
          />
        ) : (
          <div className="rounded border border-dashed border-border-strong bg-surface p-8 text-center">
            <p className="text-base font-semibold">Pick a sampled flag</p>
            <p className="mx-auto mt-2 max-w-prose text-sm text-muted">
              Rule on every item in the sample, including the ones that look obviously right. The
              estimate is only as good as the completeness of the pass.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}
