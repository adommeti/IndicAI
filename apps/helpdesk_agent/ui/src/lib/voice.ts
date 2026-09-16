import { parseRoomMessage } from "./messages";
import type { SessionEvent } from "./session";

/** Joining the LiveKit room: where the connection details come from, and the thin
 *  wrapper over `livekit-client` that turns room events into session events.
 *
 *  ## There is no token endpoint in this build
 *
 *  A LiveKit participant joins with a JWT signed by the server's API secret
 *  (`apps/helpdesk_agent/voice_demo.md`, step 2). Minting one is a server's job —
 *  the secret must never reach a browser — and the pinned uc1/P6 API contract has
 *  no such endpoint: it defines `GET /me`, `POST /chat/turn` and
 *  `GET /sessions/{id}/replay` and nothing else. So this page does not invent one.
 *
 *  Instead `VITE_LIVEKIT_TOKEN_PATH` names the path to POST to when a deployment
 *  grows one (expected to answer `{url, token, room}`), and when it is unset:
 *
 *  - on a loopback origin in a dev-bypass build, the operator can paste a token
 *    minted with the snippet in `voice_demo.md` — the documented way to drive a
 *    local room today;
 *  - anywhere else, voice is reported unavailable, in those words, and chat
 *    carries on. A mic button that cannot connect is worse than no mic button.
 *
 *  ## And the room will refuse to start anyway
 *
 *  `docs/build/BLOCKERS.md` (uc1/P5): there is no recorded consent-notice audio in
 *  this repository, and `run_session` refuses to start a session without one. So
 *  the honest expectation for a real attempt today is a connection that never gets
 *  an agent, or an agent that exits before playing anything. The UI must read that
 *  as "not listening", which is exactly what the session reducer's initial states
 *  do: nothing here is live until the pipeline says the notice is playing.
 */

export interface VoiceEnv {
  VITE_LIVEKIT_TOKEN_PATH?: string | undefined;
  VITE_LIVEKIT_URL?: string | undefined;
}

export type VoiceConfig =
  /** POST here for `{url, token, room}`. */
  | { kind: "endpoint"; path: string; url: string | null }
  /** Local development: the operator supplies a token by hand. */
  | { kind: "manual"; url: string; reason: string }
  | { kind: "unavailable"; reason: string };

const DEFAULT_LOCAL_URL = "ws://localhost:7880";

export function resolveVoice(env: VoiceEnv, devBypass: boolean): VoiceConfig {
  const path = (env.VITE_LIVEKIT_TOKEN_PATH ?? "").trim();
  const url = (env.VITE_LIVEKIT_URL ?? "").trim();
  if (path) {
    if (!path.startsWith("/"))
      return {
        kind: "unavailable",
        reason: `VITE_LIVEKIT_TOKEN_PATH must be a same-origin path beginning with "/"; got ${JSON.stringify(path)}. A token endpoint on another origin is a credential-minting service, and this page will not post an identity to one by accident.`,
      };
    return { kind: "endpoint", path, url: url || null };
  }
  if (devBypass)
    return {
      kind: "manual",
      url: url || DEFAULT_LOCAL_URL,
      reason:
        "No token endpoint is configured, so this local build accepts a LiveKit token pasted by hand — mint one with the snippet in apps/helpdesk_agent/voice_demo.md, step 2. Treat it as a credential: whoever holds it can join the room and hear the call.",
    };
  return {
    kind: "unavailable",
    reason:
      "Voice is not available: this deployment has no LiveKit token endpoint configured (VITE_LIVEKIT_TOKEN_PATH), and the helpdesk API does not mint room tokens in this build. Chat below is unaffected.",
  };
}

export interface Grant {
  url: string;
  token: string;
  room: string;
}

/** Ask the deployment's token endpoint for a join grant. Shapes are validated
 *  before anything is handed to the SDK: a "token" that is not a string is a
 *  misconfigured endpoint, not something to try connecting with. */
export function readGrant(body: unknown, fallbackUrl: string | null): Grant {
  if (typeof body !== "object" || body === null)
    throw new Error("The token endpoint did not return a JSON object.");
  const raw = body as Record<string, unknown>;
  const token = raw.token;
  const url = typeof raw.url === "string" && raw.url ? raw.url : fallbackUrl;
  if (typeof token !== "string" || !token)
    throw new Error("The token endpoint returned no token.");
  if (!url)
    throw new Error(
      "The token endpoint returned no LiveKit URL and VITE_LIVEKIT_URL is not set, so there is nowhere to connect to.",
    );
  return { url, token, room: typeof raw.room === "string" ? raw.room : "" };
}

export interface RoomHandle {
  setMicrophoneEnabled: (enabled: boolean) => Promise<void>;
  disconnect: () => Promise<void>;
}

export interface JoinOptions {
  grant: Grant;
  /** Where the agent's audio element is attached. */
  audioContainer: HTMLElement;
  onEvent: (event: SessionEvent) => void;
}

/** Connect, wire the room's events to session events, and hand back the two
 *  controls the UI needs.
 *
 *  `livekit-client` is imported dynamically: a session that never touches voice
 *  never downloads the SDK, and the unit tests never stand up a WebRTC stack in
 *  jsdom. The microphone is NOT enabled here — nothing captures until the employee
 *  presses the button, and the button is only offered once the pipeline has said
 *  the notice played (`session.micAllowed`). */
export async function joinRoom(options: JoinOptions): Promise<RoomHandle> {
  const { Room, RoomEvent, Track } = await import("livekit-client");
  const room = new Room({ adaptiveStream: false, dynacast: false });

  room.on(RoomEvent.DataReceived, (payload: Uint8Array) => {
    options.onEvent({ kind: "data", parsed: parseRoomMessage(payload) });
  });

  room.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind !== Track.Kind.Audio) return;
    const element = track.attach();
    // The consent notice is the first thing this element plays. It must be
    // audible and it must not be something the page can silence by accident.
    element.autoplay = true;
    element.setAttribute("data-testid", "agent-audio");
    options.audioContainer.appendChild(element);
  });

  // The browser-side twin of the `BotStoppedSpeakingFrame` that opens the server's
  // gate: the agent was speaking and now is not. Display only -- see session.ts.
  let agentWasSpeaking = false;
  room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
    const agentSpeaking = speakers.some(
      (speaker) => speaker.identity !== room.localParticipant.identity,
    );
    if (agentWasSpeaking && !agentSpeaking) options.onEvent({ kind: "agent-stopped-speaking" });
    agentWasSpeaking = agentSpeaking;
  });

  room.on(RoomEvent.Disconnected, () => options.onEvent({ kind: "disconnected" }));

  options.onEvent({ kind: "connecting" });
  await room.connect(options.grant.url, options.grant.token);
  options.onEvent({ kind: "connected" });

  return {
    setMicrophoneEnabled: async (enabled: boolean) => {
      await room.localParticipant.setMicrophoneEnabled(enabled);
    },
    disconnect: async () => {
      await room.disconnect();
    },
  };
}
