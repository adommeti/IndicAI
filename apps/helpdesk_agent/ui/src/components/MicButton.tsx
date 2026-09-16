import { captureCopy, micAllowed } from "../lib/session";
import type { VoiceSession } from "../lib/session";

/** The microphone control.
 *
 *  It is enabled in exactly one capture state — `listening`, which this page only
 *  enters after the pipeline has said the consent notice played. In every other
 *  state it is disabled and *says why*, rather than looking available and failing
 *  on click.
 *
 *  Disabling is not the control. `GreetingGate` drops input audio, and
 *  `SarvamSTTProcessor` stops reading it, whatever this button renders; a browser
 *  cannot enforce anything about a room it merely joined. What this button owes the
 *  employee is an honest claim: if it says "Listening", audio is leaving this
 *  browser, and if it does not, the page is not pretending otherwise. */
export function MicButton({
  session,
  micOn,
  busy,
  onToggle,
}: {
  session: VoiceSession;
  micOn: boolean;
  busy: boolean;
  onToggle: () => void;
}) {
  const allowed = micAllowed(session);
  const copy = captureCopy(session);
  const label = !allowed ? copy.label : micOn ? "Stop speaking" : "Speak";

  return (
    <div className="flex items-center gap-3">
      <button
        type="button"
        onClick={onToggle}
        disabled={!allowed || busy}
        aria-pressed={micOn}
        aria-describedby="voice-status-detail"
        data-testid="mic"
        className={`flex items-center gap-2 rounded border px-4 py-2 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:border-border disabled:bg-surface-2 disabled:text-muted ${
          micOn
            ? "border-live bg-live-wash text-live"
            : "border-border-strong bg-surface text-ink hover:border-accent"
        }`}
      >
        <span aria-hidden="true" className={micOn ? "mic-live-dot" : ""}>
          {micOn ? "●" : "🎙"}
        </span>
        {busy ? "Working…" : label}
      </button>

      <p id="voice-status-detail" className="max-w-prose text-xs text-muted">
        {micOn
          ? "Your microphone is open and audio is being sent to the agent."
          : allowed
            ? "Your microphone is closed. Nothing is being sent."
            : copy.detail}
      </p>
    </div>
  );
}
