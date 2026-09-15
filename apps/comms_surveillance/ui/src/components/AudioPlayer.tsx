import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { ApiError, api } from "../lib/api";
import { timecode } from "../lib/queue";
import type { AudioGrant } from "../lib/types";
import { FailureState } from "./States";

export interface AudioHandle {
  seek: (ms: number) => void;
}

/** Call audio, fetched on demand.
 *
 *  The grant is short-lived and minted only when the reviewer asks for it, so a
 *  queue page never mints one it does not use. The URL lives in component state
 *  for the life of the grant and nowhere else: not in storage, not in the address
 *  bar, not in a download link, and never written to the console — a presigned
 *  object URL is the recording (.claude/rules/adapters.md). When it expires the
 *  player clears it and asks for a new one rather than failing silently. */
export const AudioPlayer = forwardRef<AudioHandle, { flagId: string; startMs: number }>(
  function AudioPlayer({ flagId, startMs }, ref) {
    const audioRef = useRef<HTMLAudioElement>(null);
    const [grant, setGrant] = useState<AudioGrant | null>(null);
    const [error, setError] = useState<unknown>(null);
    const [loading, setLoading] = useState(false);
    const [remaining, setRemaining] = useState(0);

    // A new flag invalidates whatever grant is held.
    useEffect(() => {
      setGrant(null);
      setError(null);
      setRemaining(0);
    }, [flagId]);

    const load = useCallback(async () => {
      setLoading(true);
      setError(null);
      try {
        const next = await api.audio(flagId);
        setGrant(next);
        setRemaining(next.expires_in_s);
      } catch (cause) {
        setError(cause);
      } finally {
        setLoading(false);
      }
    }, [flagId]);

    useEffect(() => {
      if (!grant) return;
      const timer = window.setInterval(() => {
        setRemaining((seconds) => {
          if (seconds <= 1) {
            window.clearInterval(timer);
            setGrant(null);
            return 0;
          }
          return seconds - 1;
        });
      }, 1000);
      return () => window.clearInterval(timer);
    }, [grant]);

    const seek = useCallback((ms: number) => {
      const element = audioRef.current;
      if (!element) return;
      const apply = () => {
        element.currentTime = Math.max(0, ms / 1000);
        void element.play().catch(() => {
          /* autoplay policy: the reviewer presses play, the seek still landed */
        });
      };
      if (element.readyState >= 1) apply();
      else element.addEventListener("loadedmetadata", apply, { once: true });
    }, []);

    useImperativeHandle(ref, () => ({ seek }), [seek]);

    // The point of the player: land on the flagged moment, not at 0:00.
    const onLoadedMetadata = useCallback(() => {
      const element = audioRef.current;
      if (element) element.currentTime = Math.max(0, (grant?.start_ms ?? startMs) / 1000);
    }, [grant, startMs]);

    return (
      <section aria-labelledby="audio-heading" className="rounded border border-border bg-surface p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 id="audio-heading" className="text-sm font-semibold">
            Call audio
          </h3>
          <p className="font-mono text-xs text-muted">
            flagged at {timecode(startMs)}
            {grant ? ` · access expires in ${remaining}s` : ""}
          </p>
        </div>

        {error ? (
          <div className="mt-3">
            <FailureState error={error} what="call audio" onRetry={error instanceof ApiError && error.forbidden ? undefined : load} />
          </div>
        ) : null}

        {grant ? (
          <>
            <audio
              ref={audioRef}
              src={grant.url}
              controls
              preload="metadata"
              onLoadedMetadata={onLoadedMetadata}
              controlsList="nodownload"
              className="mt-3 w-full"
              data-testid="audio"
            >
              Your browser cannot play this recording.
            </audio>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => seek(grant.start_ms)}
                className="rounded border border-border-strong bg-surface px-3 py-1.5 text-xs font-semibold"
                data-testid="seek-evidence"
              >
                Seek to flagged moment ({timecode(grant.start_ms)})
              </button>
            </div>
            <p className="mt-2 text-xs text-muted">
              This access is time-limited and is not a shareable link. Reload it here if it lapses
              mid-review.
            </p>
          </>
        ) : !error ? (
          <div className="mt-3">
            <button
              type="button"
              onClick={() => void load()}
              disabled={loading}
              className="rounded bg-accent px-3 py-1.5 text-xs font-semibold text-[color:var(--accent-ink)] disabled:opacity-50"
              data-testid="load-audio"
            >
              {loading ? "Requesting access…" : "Load audio"}
            </button>
            <p className="mt-2 text-xs text-muted">
              Audio is fetched only when you ask for it, under a short-lived grant that starts at the
              flagged moment.
            </p>
          </div>
        ) : null}
      </section>
    );
  },
);
