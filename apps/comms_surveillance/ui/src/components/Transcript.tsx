import { evidenceLocated, markEvidence } from "../lib/evidence";
import { timecode } from "../lib/queue";
import { pickText } from "../lib/script";
import type { ScriptPref } from "../lib/script";
import type { TranscriptSegment } from "../lib/types";

/** The transcript with the flagged span marked in place.
 *
 *  Two things this must not do: highlight anything that is not the evidence, and
 *  pretend the evidence was found when it was not. When the span cannot be
 *  located in the rendered lines, the notice says so and the raw span is shown
 *  above, so the reviewer rules on the real text either way. */
export function Transcript({
  segments,
  evidenceSpan,
  scriptPref,
  focusMs,
  onSeek,
}: {
  segments: readonly TranscriptSegment[];
  evidenceSpan: string;
  scriptPref: ScriptPref;
  focusMs: number;
  onSeek?: (ms: number) => void;
}) {
  const rendered = segments.map((segment) => pickText(segment, scriptPref));
  const located = evidenceLocated(
    rendered.map((item) => item.text),
    evidenceSpan,
  );
  const fellBack = rendered.some((item) => item.fellBack);

  return (
    <section aria-labelledby="transcript-heading" className="rounded border border-border bg-surface">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border px-4 py-2">
        <h3 id="transcript-heading" className="text-sm font-semibold">
          Transcript
        </h3>
        <p className="text-xs text-muted">
          {segments.length} segments · flagged span marked{" "}
          <span className="evidence-mark">like this</span>
        </p>
      </div>

      {!located && evidenceSpan ? (
        <p
          className="border-b border-border bg-surface-2 px-4 py-2 text-xs text-muted"
          data-testid="evidence-not-located"
        >
          The flagged span could not be matched against the transcript as rendered
          {scriptPref === "latn" ? " in Latin script" : ""} — it is shown in full in the evidence
          panel above. Nothing in the transcript is marked rather than marking the wrong words.
        </p>
      ) : null}

      {fellBack ? (
        <p className="border-b border-border px-4 py-2 text-xs text-muted" data-testid="roman-fallback">
          Some lines have no romanisation and are shown in their native script.
        </p>
      ) : null}

      <ol className="divide-y divide-border">
        {segments.map((segment, index) => {
          const line = rendered[index];
          if (!line) return null;
          const parts = markEvidence(line.text, evidenceSpan);
          const isFocus = segment.start_ms <= focusMs && focusMs < segment.end_ms;
          return (
            <li
              key={segment.seg_id}
              data-testid={`segment-${segment.seg_id}`}
              data-evidence={parts.some((part) => part.evidence) ? "true" : undefined}
              className={`grid grid-cols-[7rem_1fr] gap-3 px-4 py-2 ${
                isFocus ? "bg-surface-2" : ""
              }`}
            >
              <span className="pt-0.5 font-mono text-[11px] text-muted">
                {onSeek ? (
                  <button
                    type="button"
                    onClick={() => onSeek(segment.start_ms)}
                    className="underline decoration-dotted underline-offset-2"
                    title="Play from here"
                  >
                    {timecode(segment.start_ms)}
                  </button>
                ) : (
                  timecode(segment.start_ms)
                )}
                <span className="ml-1 block">{segment.speaker}</span>
              </span>
              <p className="indic m-0">
                {parts.map((part, partIndex) =>
                  part.evidence ? (
                    <mark key={partIndex} className="evidence-mark">
                      {part.text}
                    </mark>
                  ) : (
                    <span key={partIndex}>{part.text}</span>
                  ),
                )}
                {line.fellBack ? (
                  <span className="ml-2 align-middle font-mono text-[10px] uppercase tracking-wide text-muted">
                    no romanisation
                  </span>
                ) : null}
              </p>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
