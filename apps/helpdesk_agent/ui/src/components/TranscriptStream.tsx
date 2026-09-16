import { tagLabel } from "../lib/script";
import type { VoiceSession, VoiceTurn } from "../lib/session";

const ACTION_LABELS: Record<string, string> = {
  answer: "answered",
  clarify: "asked for more detail",
  file_ticket: "offered to raise a ticket",
};

/** Live transcripts from the room.
 *
 *  A partial is a guess the pipeline has not committed to, so it is drawn as
 *  unfinished — dimmed, with a caret, and labelled "hearing…" for a screen reader —
 *  and the final text replaces it in place. The two are never shown as two
 *  utterances: the employee said one thing.
 *
 *  The language on each line is what Saaras *identified*, not what the page asked
 *  for. STT runs on `auto`, so an employee who switches script mid-call is still
 *  transcribed, and the label has to report the result rather than the setting. */
export function TranscriptStream({ session }: { session: VoiceSession }) {
  if (session.turns.length === 0) return null;
  return (
    <ol className="space-y-3" data-testid="transcripts">
      {session.turns.map((turn) => (
        <TurnLine key={turn.turn} turn={turn} />
      ))}
    </ol>
  );
}

function TurnLine({ turn }: { turn: VoiceTurn }) {
  const settled = turn.final !== null;
  return (
    <li
      className="rounded border border-border bg-surface p-3"
      data-testid={`voice-turn-${turn.turn}`}
      data-final={settled ? "true" : "false"}
    >
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-xs uppercase tracking-wider text-muted">
          You · turn {turn.turn + 1}
        </span>
        <span className="text-xs text-muted">{tagLabel(turn.language)}</span>
      </div>
      <p className={`indic mt-1 ${settled ? "" : "partial"}`} data-testid={`voice-text-${turn.turn}`}>
        {settled ? turn.final : turn.partial}
      </p>
      <p className="sr-only">{settled ? "final transcript" : "hearing, not final yet"}</p>
      {turn.action ? (
        <p className="mt-2 text-xs text-muted" data-testid={`voice-action-${turn.turn}`}>
          The agent {ACTION_LABELS[turn.action] ?? turn.action}.
        </p>
      ) : null}
    </li>
  );
}
