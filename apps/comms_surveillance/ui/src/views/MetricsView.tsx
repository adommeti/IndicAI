import { useCallback } from "react";
import {
  ChainStatusPanel,
  FalseNegativePanel,
  PrecisionOverTime,
  PrecisionTable,
} from "../components/Metrics";
import { FailureState, Loading } from "../components/States";
import { api } from "../lib/api";
import type { ChainStatus, FalseNegativeEstimate, PrecisionMetrics, Role } from "../lib/types";
import { useAsync } from "../lib/useAsync";

/** Quality and chain status.
 *
 *  This is the whole of the governance surface: metrics and verification, and no
 *  request is made from this view for a transcript, a flag detail or an audio
 *  grant — a governance session simply has no code path that asks. The service
 *  refuses those endpoints for that role in any case; this view is built so the
 *  question never comes up. */
export function MetricsView({
  roles,
  governanceOnly,
}: {
  roles: readonly Role[];
  governanceOnly: boolean;
}) {
  const loadPrecision = useCallback(() => api.precision(), []);
  const loadFalseNegatives = useCallback(() => api.falseNegatives(), []);
  const loadChain = useCallback(() => api.chainStatus(), []);

  const precision = useAsync<PrecisionMetrics>(loadPrecision);
  const falseNegatives = useAsync<FalseNegativeEstimate>(loadFalseNegatives);
  const chain = useAsync<ChainStatus>(loadChain);

  return (
    <div className="space-y-4">
      {governanceOnly ? (
        <p className="rounded border border-border bg-surface px-4 py-3 text-xs text-muted" data-testid="governance-scope">
          Governance view: detection quality and audit-chain verification. Call transcripts and
          recordings are not part of this role, and are not requested here.
        </p>
      ) : null}

      {precision.loading ? <Loading label="Loading precision" rows={4} /> : null}
      {precision.error ? (
        <FailureState error={precision.error} what="detection quality metrics" roles={roles} onRetry={precision.reload} />
      ) : null}
      {precision.data ? (
        <>
          <PrecisionTable metrics={precision.data} />
          <PrecisionOverTime metrics={precision.data} />
        </>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-2">
        <div>
          {falseNegatives.loading ? <Loading label="Loading estimate" rows={2} /> : null}
          {falseNegatives.error ? (
            <FailureState
              error={falseNegatives.error}
              what="the false-negative estimate"
              roles={roles}
              onRetry={falseNegatives.reload}
            />
          ) : null}
          {falseNegatives.data ? <FalseNegativePanel estimate={falseNegatives.data} /> : null}
        </div>
        <div>
          {chain.loading ? <Loading label="Verifying chains" rows={2} /> : null}
          {chain.error ? (
            <FailureState error={chain.error} what="audit-chain verification" roles={roles} onRetry={chain.reload} />
          ) : null}
          {chain.data ? <ChainStatusPanel status={chain.data} /> : null}
        </div>
      </div>
    </div>
  );
}
