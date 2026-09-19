import { useCallback, useRef } from "react";
import { api } from "../lib/api";
import { renderingOrAbsent } from "../lib/evidence";
import { dispositionLabel, timecode } from "../lib/queue";
import type { ScriptPref } from "../lib/script";
import type { Disposition, FlagDetail as FlagDetailShape, Role } from "../lib/types";
import { useAsync } from "../lib/useAsync";
import { AudioPlayer } from "./AudioPlayer";
import type { AudioHandle } from "./AudioPlayer";
import { DispositionForm, DispositionHistory } from "./DispositionForm";
import { SeverityTag } from "./Severity";
import { EmptyState, FailureState, Loading } from "./States";
import { Transcript } from "./Transcript";

function Field({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div>
      <p className="text-xs font-semibold uppercase tracking-wider text-muted">{label}</p>
      <div className={`mt-1 text-sm ${mono ? "font-mono text-xs" : ""}`}>{children}</div>
    </div>
  );
}

export function FlagDetailPane({
  flagId,
  scriptPref,
  roles,
  onDisposition,
}: {
  flagId: string;
  scriptPref: ScriptPref;
  roles: readonly Role[];
  onDisposition: (flagId: string, disposition: Disposition) => void;
}) {
  const load = useCallback(() => api.flag(flagId), [flagId]);
  const { data, error, loading, reload, patch } = useAsync<FlagDetailShape>(load);
  const audioRef = useRef<AudioHandle>(null);

  const seek = useCallback((ms: number) => audioRef.current?.seek(ms), []);

  if (loading) return <Loading label="Loading flag" rows={6} />;
  if (error)
    return (
      <div className="p-4">
        <FailureState error={error} what="this flag, its transcript and its audio" roles={roles} onRetry={reload} />
      </div>
    );
  if (!data)
    return (
      <div className="p-4">
        <EmptyState title="Select a flag" body="Pick a flag from the queue to review it." />
      </div>
    );

  return (
    <div className="space-y-4" data-testid="flag-detail">
      <header className="rounded border border-border bg-surface p-4">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <SeverityTag severity={data.severity} size="md" />
          <h2 className="text-lg font-semibold">{data.category}</h2>
          <span
            className={`rounded px-2 py-0.5 font-mono text-[11px] uppercase tracking-wide ${
              data.disposition === null
                ? "bg-surface-2 font-semibold text-ink"
                : "bg-surface-2 text-muted"
            }`}
            data-testid="current-disposition"
          >
            {dispositionLabel(data.disposition)}
          </span>
        </div>
        <dl className="mt-3 grid gap-x-6 gap-y-2 font-mono text-xs text-muted sm:grid-cols-4">
          <div>
            <dt className="uppercase tracking-wider">Flag</dt>
            <dd className="text-ink">{data.flag_id}</dd>
          </div>
          <div>
            <dt className="uppercase tracking-wider">Call</dt>
            <dd className="text-ink">{data.call_id}</dd>
          </div>
          <div>
            <dt className="uppercase tracking-wider">Speaker</dt>
            <dd className="text-ink">{data.speaker}</dd>
          </div>
          <div>
            <dt className="uppercase tracking-wider">Flagged at</dt>
            <dd className="text-ink">{timecode(data.start_ms)}</dd>
          </div>
        </dl>
      </header>

      <section
        aria-labelledby="evidence-heading"
        className="rounded border border-border bg-surface p-4"
      >
        <h3 id="evidence-heading" className="text-sm font-semibold">
          Evidence
        </h3>
        <div className="mt-3 grid gap-4 lg:grid-cols-2">
          <Field label="Flagged span (as spoken)">
            <p className="indic m-0 border-l-4 border-[color:var(--evidence)] bg-surface-2 p-2" data-testid="evidence-span">
              {data.evidence_span}
            </p>
          </Field>
          <Field label="English rendering">
            <p
              className={`m-0 border-l-4 border-border-strong bg-surface-2 p-2 ${
                renderingOrAbsent(data.english_rendering).absent ? "italic text-muted" : ""
              }`}
              data-testid="english-rendering"
            >
              {renderingOrAbsent(data.english_rendering).text}
            </p>
          </Field>
          <Field label="Why it was flagged">
            <p className="m-0 whitespace-pre-wrap" data-testid="reasoning">
              {data.reasoning}
            </p>
          </Field>
          <Field label="Policy clause">
            <p className="m-0 whitespace-pre-wrap" data-testid="policy-clause">
              {data.policy_clause}
            </p>
          </Field>
        </div>
      </section>

      <AudioPlayer ref={audioRef} flagId={data.flag_id} startMs={data.start_ms} />

      <Transcript
        segments={data.transcript}
        evidenceSpan={data.evidence_span}
        scriptPref={scriptPref}
        focusMs={data.start_ms}
        onSeek={seek}
      />

      <div className="grid gap-4 lg:grid-cols-2">
        <DispositionForm
          flagId={data.flag_id}
          current={data.disposition}
          onRecorded={(row) => {
            // Append-only, locally too: the new row goes on the front of the
            // history and becomes the flag's current ruling. Nothing is replaced.
            patch({ ...data, disposition: row.disposition, dispositions: [row, ...data.dispositions] });
            onDisposition(data.flag_id, row.disposition);
          }}
        />
        <DispositionHistory rows={data.dispositions} />
      </div>
    </div>
  );
}
