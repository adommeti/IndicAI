import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { MicButton } from "../components/MicButton";
import { TranscriptStream } from "../components/TranscriptStream";
import { VoiceStatus } from "../components/VoiceStatus";
import { rawRequest } from "../lib/api";
import { INITIAL_SESSION, isTerminal, reduceSession } from "../lib/session";
import type { VoiceSession } from "../lib/session";
import { joinRoom, readGrant, resolveVoice } from "../lib/voice";
import type { Grant, RoomHandle } from "../lib/voice";
import type { LanguageTag } from "../lib/types";

/** The voice half of the helpdesk: join a LiveKit room, speak, watch the captions.
 *
 *  Three things this component refuses to do, each because of something in
 *  `voice_pipeline.py` or `docs/build/BLOCKERS.md`:
 *
 *  1. It does not enable the microphone on join. Capture starts when the employee
 *     presses the button, and the button is offered only once the pipeline has said
 *     the consent notice played.
 *  2. It does not keep a mic open across a halted state. When the session reducer
 *     goes terminal — notice unconfirmed, STT dead, a payload it cannot read — the
 *     mic is closed here as well, so the browser stops sending audio nobody is
 *     transcribing.
 *  3. It does not pretend a room exists. There is no consent-notice asset in this
 *     repository, so `run_session` refuses to start; a connection attempt today
 *     gets a room with no agent in it, and this panel will sit in "waiting for the
 *     notice" with the mic disabled rather than inviting the employee to speak.
 */
export function VoicePanel({
  language,
  devBypass,
}: {
  language: LanguageTag;
  devBypass: boolean;
}) {
  const config = useMemo(() => resolveVoice(import.meta.env, devBypass), [devBypass]);
  const [session, dispatch] = useReducer(reduceSession, INITIAL_SESSION);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [micOn, setMicOn] = useState(false);
  const [manualToken, setManualToken] = useState("");
  const [manualUrl, setManualUrl] = useState(config.kind === "manual" ? config.url : "");
  const room = useRef<RoomHandle | null>(null);
  const audio = useRef<HTMLDivElement | null>(null);

  const connected = session.capture !== "idle" && session.capture !== "connecting";

  // A halted session is not listening. Close the microphone rather than leaving it
  // open into a pipeline that has stopped reading it.
  useEffect(() => {
    if (!isTerminal(session.capture) || !micOn) return;
    setMicOn(false);
    room.current?.setMicrophoneEnabled(false).catch(() => undefined);
  }, [session.capture, micOn]);

  // Leaving the page leaves the room. A tab closed on a live microphone is a
  // microphone that keeps billing Saaras for whatever it hears.
  useEffect(
    () => () => {
      room.current?.disconnect().catch(() => undefined);
    },
    [],
  );

  const connect = useCallback(
    async (grant: Grant) => {
      setConnectError(null);
      setBusy(true);
      try {
        room.current = await joinRoom({
          grant,
          audioContainer: audio.current ?? document.body,
          onEvent: dispatch,
        });
      } catch (cause) {
        dispatch({ kind: "disconnected" });
        setConnectError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const startFromEndpoint = useCallback(async () => {
    if (config.kind !== "endpoint") return;
    setBusy(true);
    setConnectError(null);
    try {
      const body = await rawRequest<unknown>(config.path, {
        method: "POST",
        body: JSON.stringify({ language }),
      });
      await connect(readGrant(body, config.url));
    } catch (cause) {
      setConnectError(cause instanceof Error ? cause.message : String(cause));
      setBusy(false);
    }
  }, [config, connect, language]);

  const startFromToken = useCallback(async () => {
    try {
      await connect(readGrant({ token: manualToken.trim(), url: manualUrl.trim() }, null));
    } catch (cause) {
      setConnectError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [connect, manualToken, manualUrl]);

  const leave = useCallback(async () => {
    setMicOn(false);
    await room.current?.setMicrophoneEnabled(false).catch(() => undefined);
    await room.current?.disconnect().catch(() => undefined);
    room.current = null;
    dispatch({ kind: "reset" });
  }, []);

  const toggleMic = useCallback(async () => {
    const next = !micOn;
    setBusy(true);
    try {
      await room.current?.setMicrophoneEnabled(next);
      setMicOn(next);
    } catch (cause) {
      setConnectError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }, [micOn]);

  return (
    <section
      aria-labelledby="voice-heading"
      className="rounded border border-border bg-surface p-4"
      data-testid="voice-panel"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="voice-heading" className="text-base font-semibold">
          Speak to the helpdesk
        </h2>
        {connected ? (
          <button
            type="button"
            onClick={leave}
            data-testid="leave"
            className="rounded border border-border-strong bg-surface px-3 py-1.5 text-xs font-semibold"
          >
            Leave the room
          </button>
        ) : null}
      </div>

      <div ref={audio} className="sr-only" data-testid="agent-audio-host" />

      {config.kind === "unavailable" ? (
        <p className="mt-3 max-w-prose text-sm text-muted" data-testid="voice-unavailable">
          {config.reason}
        </p>
      ) : null}

      {!connected && config.kind === "endpoint" ? (
        <div className="mt-3 space-y-2">
          <button
            type="button"
            onClick={startFromEndpoint}
            disabled={busy}
            data-testid="join"
            className="rounded border border-border-strong bg-surface px-4 py-2 text-sm font-semibold hover:border-accent disabled:cursor-not-allowed disabled:text-muted"
          >
            {busy ? "Joining…" : "Start a voice session"}
          </button>
          <p className="max-w-prose text-xs text-muted">
            The call is recorded and transcribed. A recorded notice says so before anything you say
            is captured, and the agent will not listen until it has played.
          </p>
        </div>
      ) : null}

      {!connected && config.kind === "manual" ? (
        <form
          className="mt-3 space-y-2"
          data-testid="manual-join"
          onSubmit={(event) => {
            event.preventDefault();
            void startFromToken();
          }}
        >
          <p className="max-w-prose text-xs text-muted">{config.reason}</p>
          <div className="flex flex-wrap gap-2">
            <label className="flex-1 text-xs">
              <span className="text-muted">LiveKit URL</span>
              <input
                value={manualUrl}
                onChange={(event) => setManualUrl(event.target.value)}
                data-testid="manual-url"
                className="mt-1 w-full rounded border border-border bg-surface-2 px-2 py-1.5 font-mono text-xs"
              />
            </label>
            <label className="flex-[2] text-xs">
              <span className="text-muted">Join token</span>
              <input
                value={manualToken}
                onChange={(event) => setManualToken(event.target.value)}
                data-testid="manual-token"
                autoComplete="off"
                spellCheck={false}
                className="mt-1 w-full rounded border border-border bg-surface-2 px-2 py-1.5 font-mono text-xs"
              />
            </label>
          </div>
          <button
            type="submit"
            disabled={busy || !manualToken.trim()}
            data-testid="join"
            className="rounded border border-border-strong bg-surface px-4 py-2 text-sm font-semibold hover:border-accent disabled:cursor-not-allowed disabled:text-muted"
          >
            {busy ? "Joining…" : "Join the room"}
          </button>
        </form>
      ) : null}

      {connectError ? (
        <p
          role="alert"
          className="mt-3 rounded border border-danger-edge bg-danger-wash p-3 text-sm text-danger"
          data-testid="voice-error"
        >
          {connectError}
          {/* The one failure worth naming, because it is the state of this
              repository today rather than a mistake by the operator. */}
          {/(greeting|notice|consent)/i.test(connectError) ? (
            <span className="mt-1 block text-xs text-muted">
              The agent refuses to start a session without its recorded consent notice. That asset
              is not in this repository yet (docs/build/BLOCKERS.md).
            </span>
          ) : null}
        </p>
      ) : null}

      {connected ? (
        <div className="mt-4 space-y-3">
          <VoiceStatus session={session} />
          <MicButton session={session} micOn={micOn} busy={busy} onToggle={() => void toggleMic()} />
          <TranscriptStream session={session} />
          <EmptyTranscripts session={session} />
        </div>
      ) : null}
    </section>
  );
}

function EmptyTranscripts({ session }: { session: VoiceSession }) {
  if (session.turns.length > 0) return null;
  if (session.capture === "listening")
    return (
      <p className="rounded border border-dashed border-border-strong p-4 text-center text-sm text-muted">
        Nothing transcribed yet. Press <strong>Speak</strong> and say what you need — captions
        appear here as the agent hears them.
      </p>
    );
  return null;
}
