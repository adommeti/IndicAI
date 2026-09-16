import { useCallback, useState } from "react";
import { EmptyState, FailureState, Loading } from "../components/States";
import { api } from "../lib/api";
import type { AuthMode } from "../lib/auth";
import { routeHref } from "../lib/roles";
import { tagLabel } from "../lib/script";
import type { Replay, ReplayTurn } from "../lib/types";
import { useAsync } from "../lib/useAsync";

/** `GET /sessions/{id}/replay`: the whole chain for one helpdesk session.
 *
 *  This view is reachable by URL for every signed-in employee, whether or not the
 *  tab was drawn for them. That is on purpose. The role check lives in the service
 *  (`helpdesk_agent/roles.py: enforce_replay`) and a hidden tab is not a control, so
 *  this page asks and renders the answer — a 403 for a caller without uc1's
 *  `governance` role, including for a session id that does not exist, which is why
 *  a refusal here never says whether the session was real. */
export function ReplayView({
  sessionId,
  roles,
  mode,
}: {
  sessionId: string | null;
  roles: readonly string[];
  mode: AuthMode;
}) {
  const [draft, setDraft] = useState(sessionId ?? "");
  const load = useCallback(() => api.replay(sessionId ?? ""), [sessionId]);
  const { data, error, loading, reload } = useAsync<Replay>(load, Boolean(sessionId));

  return (
    <div className="space-y-4">
      <section className="rounded border border-border bg-surface p-4">
        <h2 className="text-base font-semibold">Session replay</h2>
        <p className="mt-1 max-w-prose text-sm text-muted">
          Every turn of one helpdesk session: what the employee said, what was retrieved, what the
          model decided, what it cost in milliseconds, and the trace behind it.
        </p>
        <form
          className="mt-3 flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            window.location.hash = routeHref("replay", draft.trim());
          }}
        >
          <label htmlFor="session-id" className="sr-only">
            Session id
          </label>
          <input
            id="session-id"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="session uuid"
            data-testid="session-id"
            className="w-80 max-w-full rounded border border-border bg-surface-2 px-2 py-1.5 font-mono text-xs"
          />
          <button
            type="submit"
            disabled={!draft.trim()}
            data-testid="open-replay"
            className="rounded border border-border-strong bg-surface px-3 py-1.5 text-sm font-semibold disabled:cursor-not-allowed disabled:text-muted"
          >
            Open
          </button>
        </form>
      </section>

      {!sessionId ? (
        <EmptyState
          title="No session opened"
          body="Paste a session id above. The link in the address bar is the deep link — an auditor can paste it into a ticket."
        />
      ) : loading ? (
        <Loading label="Loading the chain" rows={4} />
      ) : error ? (
        <FailureState
          error={error}
          what="session replay"
          roles={roles}
          onRetry={reload}
          mode={mode}
        />
      ) : data ? (
        <Chain replay={data} />
      ) : null}
    </div>
  );
}

function Chain({ replay }: { replay: Replay }) {
  return (
    <section data-testid="replay" className="space-y-3">
      <header className="rounded border border-border bg-surface p-4">
        <p className="font-mono text-xs" data-testid="replay-session">
          {replay.session_id}
        </p>
        <p className="mt-1 text-sm text-muted">
          {replay.employee_id} · opened {new Date(replay.created_at).toLocaleString()} ·{" "}
          {replay.turns.length} turn{replay.turns.length === 1 ? "" : "s"}
        </p>
        {/* Stated once, at the top, rather than as a broken player on every turn. */}
        <p className="mt-2 max-w-prose text-xs text-muted" data-testid="no-audio">
          No audio is kept. This application streams a voice turn to the vendor and to the room and
          stores none of it, so every <span className="font-mono">audio_url</span> in this chain is
          null. The transcript below is the record of what was said.
        </p>
      </header>

      {replay.turns.length === 0 ? (
        <EmptyState
          title="This session has no turns"
          body="The session row exists but nothing was ever said in it."
        />
      ) : (
        <ol className="space-y-3">
          {replay.turns.map((turn) => (
            <li key={turn.turn_index}>
              <TurnCard turn={turn} />
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function TurnCard({ turn }: { turn: ReplayTurn }) {
  const decision = turn.decision_json ?? {};
  const action = typeof decision.action === "string" ? decision.action : null;
  const reply = typeof decision.reply_text === "string" ? decision.reply_text : null;
  const cited = Array.isArray(decision.cited_article_ids)
    ? decision.cited_article_ids.filter((id): id is string => typeof id === "string")
    : [];

  return (
    <article
      className="rounded border border-border bg-surface p-4"
      data-testid={`replay-turn-${turn.turn_index}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">Turn {turn.turn_index + 1}</h3>
        <p className="text-xs text-muted">{tagLabel(turn.language)}</p>
      </div>

      <p className="text-xs uppercase tracking-wider text-muted">Employee said</p>
      <p className="indic mt-1" data-testid="replay-utterance">
        {turn.utterance}
      </p>

      {reply ? (
        <>
          <p className="mt-3 text-xs uppercase tracking-wider text-muted">
            Agent replied{action ? ` · ${action}` : ""}
          </p>
          <p className="indic mt-1" data-testid="replay-reply">
            {reply}
          </p>
        </>
      ) : null}

      {cited.length > 0 ? (
        <p className="mt-2 text-xs text-muted" data-testid="replay-citations">
          Cited {cited.join(", ")}
        </p>
      ) : null}

      <Latencies latency={turn.latency_ms} />

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
        <Meta label="model" value={turn.model} />
        <Meta label="prompt" value={turn.prompt_version} />
        <Meta label="policy" value={turn.policy_version} />
        <Meta label="ticket" value={ticketLabel(turn)} />
      </dl>

      <div className="mt-3 flex flex-wrap items-center gap-3 text-xs">
        {turn.langfuse_url ? (
          <a
            className="underline"
            href={turn.langfuse_url}
            target="_blank"
            rel="noreferrer noopener"
            data-testid="trace-link"
          >
            Open the Langfuse trace
          </a>
        ) : turn.trace_id ? (
          // A link built from a half-configured deployment lands on a 404 and an
          // auditor reads that as "the trace was lost". The id is the honest handle.
          <span className="text-muted" data-testid="trace-id">
            Trace <span className="font-mono">{turn.trace_id}</span> — no Langfuse link is
            configured for this deployment.
          </span>
        ) : (
          <span className="text-muted" data-testid="no-trace">
            No trace recorded for this turn.
          </span>
        )}
      </div>

      <details className="mt-3">
        <summary className="cursor-pointer text-xs font-semibold">Decision JSON</summary>
        <pre className="mt-2 max-h-80 overflow-auto rounded border border-border bg-surface-2 p-2 font-mono text-xs">
          {JSON.stringify(turn.decision_json, null, 2)}
        </pre>
      </details>
      <details className="mt-2">
        <summary className="cursor-pointer text-xs font-semibold">Retrieval JSON</summary>
        <pre className="mt-2 max-h-80 overflow-auto rounded border border-border bg-surface-2 p-2 font-mono text-xs">
          {JSON.stringify(turn.retrieval_json, null, 2)}
        </pre>
      </details>
    </article>
  );
}

function ticketLabel(turn: ReplayTurn): string | null {
  if (!turn.ticket) return null;
  return turn.ticket.ticket_number
    ? `${turn.ticket.status} ${turn.ticket.ticket_number}`
    : turn.ticket.status;
}

function Meta({ label, value }: { label: string; value: string | null }) {
  return (
    <div>
      <dt className="uppercase tracking-wider text-muted">{label}</dt>
      <dd className="font-mono">{value ?? <span className="text-muted">not recorded</span>}</dd>
    </div>
  );
}

/** Stage timings. A stage that did not run is ABSENT from the row, and absent is
 *  rendered as "not measured" — never as 0 ms, which would report a measurement
 *  nobody took (`voice_pipeline.StageLatency`, and CLAUDE.md on unmeasured data). */
function Latencies({ latency }: { latency: Record<string, number> | null }) {
  const entries = Object.entries(latency ?? {});
  if (entries.length === 0)
    return (
      <p className="mt-3 text-xs text-muted" data-testid="no-latency">
        No stage timings recorded for this turn — unmeasured, not zero.
      </p>
    );
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="min-w-full text-xs" data-testid="latency">
        <caption className="sr-only">Stage timings in milliseconds</caption>
        <thead>
          <tr className="text-left text-muted">
            {entries.map(([stage]) => (
              <th key={stage} scope="col" className="pr-4 font-normal uppercase tracking-wider">
                {stage}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            {entries.map(([stage, ms]) => (
              <td key={stage} className="pr-4 font-mono">
                {Math.round(ms)} ms
              </td>
            ))}
          </tr>
        </tbody>
      </table>
    </div>
  );
}
