import { useCallback, useState } from "react";
import { FlagDetailPane } from "../components/FlagDetail";
import { FlagQueue, QueueFilters } from "../components/FlagQueue";
import { EmptyState, FailureState, Loading } from "../components/States";
import { api } from "../lib/api";
import { EMPTY_FILTER, applyDisposition, filterQueue, sortQueue } from "../lib/queue";
import type { QueueFilter } from "../lib/queue";
import type { FlagSummary, Role } from "../lib/types";
import type { ScriptPref } from "../lib/script";
import { useAsync } from "../lib/useAsync";

/** Reviewer (and lead) home: the severity-sorted queue beside the flag under
 *  review. The two panes scroll independently so a long transcript never costs
 *  the reviewer their place in the queue. */
export function QueueView({
  flagId,
  scriptPref,
  roles,
}: {
  flagId: string | null;
  scriptPref: ScriptPref;
  roles: readonly Role[];
}) {
  const [filter, setFilter] = useState<QueueFilter>(EMPTY_FILTER);
  const load = useCallback(
    () =>
      api
        .flags({ severity: filter.severity, undispositioned: filter.undispositioned })
        .then(sortQueue),
    [filter.severity, filter.undispositioned],
  );
  const { data, error, loading, reload, patch } = useAsync<FlagSummary[]>(load);

  const onDisposition = useCallback(
    (id: string, disposition: FlagSummary["disposition"]) => {
      if (!data || disposition === null) return;
      // Keep the list honest without a refetch: the row shows its new ruling, and
      // leaves the "open only" view if that is what is being looked at.
      patch(filterQueue(applyDisposition(data, id, disposition), filter));
    },
    [data, filter, patch],
  );

  // A queue that cannot be loaded takes the whole view: filters and an empty
  // detail pane beside a refusal would read as a broken page rather than an answer.
  if (error)
    return (
      <FailureState
        error={error}
        what="the review queue, call transcripts and recordings"
        roles={roles}
        onRetry={reload}
      />
    );

  return (
    <div className="grid gap-4 lg:grid-cols-[22rem_1fr]">
      <section
        aria-label="Queue"
        className="h-fit overflow-hidden rounded border border-border bg-surface lg:sticky lg:top-4 lg:max-h-[calc(100vh-6rem)] lg:overflow-y-auto"
      >
        <QueueFilters filter={filter} onChange={setFilter} />
        {loading ? <Loading label="Loading queue" rows={5} /> : null}
        {data && !loading ? <FlagQueue flags={data} selectedId={flagId} /> : null}
      </section>

      <section aria-label="Flag under review" className="min-w-0">
        {flagId ? (
          <FlagDetailPane
            flagId={flagId}
            scriptPref={scriptPref}
            roles={roles}
            onDisposition={onDisposition}
          />
        ) : (
          <EmptyState
            title="No flag selected"
            body="Choose a flag from the queue. It is sorted highest severity first, then oldest first within a severity, so working top-down clears the queue in the order the policy cares about."
          />
        )}
      </section>
    </div>
  );
}
