import { useState } from "react";
import { timecode } from "../lib/review";
import type { Language, Segment } from "../lib/types";
import { GlossaryChips, LockedBadge, ScoreBadge } from "./Badges";
import { ChangeLog } from "./ChangeLog";

export function SegmentRow({
  segment,
  language,
  busy,
  onApprove,
}: {
  segment: Segment;
  language: Language;
  busy: boolean;
  onApprove: (text: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(segment.translation);

  const rowTone = segment.locked_mismatch
    ? "border-l-4 border-l-danger"
    : segment.flagged
      ? "border-l-4 border-l-warn"
      : segment.approved
        ? "border-l-4 border-l-ok"
        : "border-l-4 border-l-transparent";

  return (
    <tr className={`align-top ${rowTone} border-b border-border`} data-testid={`seg-${segment.seg_id}`}>
      <td className="p-3 font-mono text-xs text-muted">
        <div>#{segment.seg_id}</div>
        <div>{timecode(segment.start_ms)}</div>
        <div className="mt-1">
          <LockedBadge segment={segment} />
        </div>
      </td>

      <td className="max-w-xs p-3 text-sm">{segment.source_text}</td>

      <td className="max-w-md p-3" lang={language}>
        {editing ? (
          <div>
            <textarea
              aria-label={`Translation for segment ${segment.seg_id}`}
              className="indic w-full rounded border border-border bg-surface p-2"
              rows={4}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
            <div className="mt-2 flex gap-2">
              <button
                type="button"
                disabled={busy}
                onClick={() => onApprove(draft)}
                className="rounded bg-accent px-3 py-1 text-xs font-semibold text-white disabled:opacity-40"
              >
                Save and approve
              </button>
              <button
                type="button"
                onClick={() => {
                  setDraft(segment.translation);
                  setEditing(false);
                }}
                className="rounded border border-border px-3 py-1 text-xs"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <div>
            <p className="indic">{segment.translation || <span className="text-muted">—</span>}</p>
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="mt-1 rounded text-[11px] text-accent underline underline-offset-2"
            >
              Edit
            </button>
          </div>
        )}
      </td>

      <td className="max-w-xs p-3 text-sm text-muted">
        {segment.backtranslation ?? <span className="text-[11px]">unmeasured</span>}
        {segment.qa_reason && (
          <span className="mt-1 block text-[11px] italic">{segment.qa_reason}</span>
        )}
      </td>

      <td className="p-3 text-center">
        <ScoreBadge score={segment.qa_score} />
      </td>

      <td className="max-w-[10rem] p-3">
        <GlossaryChips segment={segment} />
      </td>

      <td className="max-w-[12rem] p-3">
        <ChangeLog changes={segment.change_log} />
      </td>

      <td className="p-3 text-right">
        {segment.approved ? (
          <div className="text-[11px] text-ok">
            approved
            <span className="block text-muted">{segment.approved_by}</span>
          </div>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => onApprove(segment.translation)}
            className="rounded border border-accent px-3 py-1 text-xs font-semibold text-accent disabled:opacity-40"
            data-testid={`approve-${segment.seg_id}`}
          >
            Approve
          </button>
        )}
      </td>
    </tr>
  );
}
