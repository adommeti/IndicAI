import { captureCopy, refusalCopy } from "../lib/session";
import type { VoiceSession } from "../lib/session";

/** What the room is doing, in one place and in words.
 *
 *  The halted states — the notice never confirmed, STT gone, a payload this page
 *  cannot read — are drawn as a boxed panel with no dismiss control, because they
 *  are not notifications: they are the state of the session for the rest of its
 *  life, and the employee's next sentence depends on knowing them. A dismissible
 *  toast over a live-looking mic button is precisely the failure this panel exists
 *  to prevent.
 *
 *  `aria-live="assertive"` on the halted case is deliberate: "the agent stopped
 *  listening" interrupts, because someone who is mid-sentence needs to stop talking
 *  now rather than at the next convenient pause. The running states are polite. */
export function VoiceStatus({ session }: { session: VoiceSession }) {
  const copy = captureCopy(session);
  const halted = copy.halted;

  return (
    <div className="space-y-2">
      <section
        role="status"
        aria-live={halted ? "assertive" : "polite"}
        data-testid="voice-status"
        data-capture={session.capture}
        className={
          halted
            ? "rounded border-2 border-halt bg-halt-wash p-4"
            : "rounded border border-border bg-surface-2 p-3"
        }
      >
        <p
          className={`flex items-center gap-2 text-sm font-semibold ${halted ? "text-halt" : ""}`}
        >
          <span aria-hidden="true">{halted ? "⏹" : session.capture === "listening" ? "●" : "…"}</span>
          {copy.label}
        </p>
        <p className="mt-1 max-w-prose text-sm text-muted">{copy.detail}</p>

        {session.refusal ? (
          <p className="mt-2 max-w-prose text-sm" data-testid="refusal">
            {refusalCopy(session.refusal)}
          </p>
        ) : null}

        {session.capture === "chat-only" && session.modeReason ? (
          <p className="mt-2 font-mono text-xs text-muted" data-testid="mode-reason">
            reason: {session.modeReason}
          </p>
        ) : null}
      </section>

      {/* Sticky for the rest of the session even if a later announcement moved the
          room on: "this session already failed to confirm the notice once" is not
          something to quietly stop mentioning. */}
      {session.noticeEverUnconfirmed && session.capture !== "notice-unconfirmed" ? (
        <p
          className="rounded border border-halt-edge bg-halt-wash p-2 text-xs"
          data-testid="notice-unconfirmed-history"
        >
          Earlier in this session the agent could not confirm the recorded notice had played and
          captured nothing until it was played again.
        </p>
      ) : null}

      {session.errors.length > 0 ? (
        <ul className="space-y-1" data-testid="turn-errors">
          {session.errors.map((error, index) => (
            <li
              key={`${error.stage}-${index}`}
              className="rounded border border-warn-edge bg-warn-wash p-2 text-xs text-warn"
            >
              The agent could not complete the <span className="font-mono">{error.stage}</span> step
              {error.turn === null ? "" : ` for turn ${error.turn + 1}`}
              {error.recoverable
                ? " — it recovered and answered; say it again if the reply missed the point."
                : " and stopped."}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
