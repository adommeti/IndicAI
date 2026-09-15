"""`make voice-test`: the UC1 voice latency gate, measured from LiveKit track events.

This joins the rooms the voice pipeline is serving as an ordinary participant,
plays the 30 golden WAVs through a published microphone track, and times the
agent's reply the way a caller would hear it: from the moment the caller stops
speaking to the first frame of agent audio that arrives on the subscribed track.
Nothing here trusts a timer the pipeline keeps about itself.

**One session per language, three sessions, run one after another.** A session's
``VoiceSettings.language`` is not a label: ``build_pipeline`` hands it to
``DecideProcessor`` and picks the Bulbul speaker from it, so a session pinned to
``hi-IN`` decides in Hindi and answers in a Hindi voice whatever the caller said.
Playing Telugu and Tamil items into a Hindi session would measure a configuration
the product never runs -- and charge its wrong-language TTS to the latency gate --
so each language gets its own room, its own ``run_session`` and its own ten items.
The three sets of measurements are then pooled: the gate stays the prompt's single
gate over 30 items (p50 <= 2.0s, p95 <= 3.5s) plus one action-accuracy number, not
three per-language gates.

**Sequentially, not concurrently.** Three concurrent sessions would finish in
roughly a third of the wall-clock for the same money, but they would put three
Saaras streams, three Claude calls and three Bulbul streams in flight at once,
plus three Silero VAD instances and three MP3 decoders in this one process and
event loop. Time-to-first-audio measured under that load is a number about
contention -- ours and the vendors' -- and the gate is a claim about what one
caller waits for. Sequential runs cost only wall-clock, so they are what this
harness does.

What it measures, precisely:

* ``t0`` is the instant the last *non-silent* sample of the utterance was played
  out locally. The file's trailing silence is still played -- the pipeline's VAD
  needs it to end the turn -- but it is not charged to the pipeline, so the
  number is time-from-end-of-speech rather than time-from-end-of-file.
* ``t1`` is the arrival time of the first frame of a run of at least
  ``VOICE_FRAMES`` consecutive non-silent frames on the agent's audio track,
  taken from ``rtc.AudioStream``. A lone click cannot be mistaken for a reply.
* ``time-to-first-audio = t1 - t0``, per item.

``t1`` has to belong to *this* item, and two defences make sure it does. The
harness waits for ``QUIET_GAP_S`` of agent silence before it plays an utterance,
and that gap outlasts both the pipeline's own inter-sentence idle timeout and the
synthesis of the sentence it flushes when that timeout fires, so a reply that is
merely between sentences is never mistaken for a finished one. And
every accepted onset must fall at or after the instant this item's ``decide``
returned: audio cannot be synthesized from a decision that did not exist yet, so
an earlier onset is residue from the previous turn. An onset that fails either
test is a problem, not a fast measurement.

The gate is built to be able to fail. Every item must produce a measurement:
an item that yields no agent audio, or no decision, is recorded as a problem and
fails the test -- it is never dropped so that the survivors can be averaged into
a flattering percentile. A session that dies takes the whole run down with it, by
name, rather than quietly shrinking the denominator to the items it managed. The
printed summary always states how many items were measured out of how many were
required, and that count is the denominator of both percentiles.

Preconditions (LiveKit reachable, keys present, golden audio present) SKIP, and
a skipped run measured nothing: it is UNMEASURED, not a pass. Set
``VOICE_TEST_REQUIRE=1`` to turn those skips into failures.

Requires Docker (`make stack-voice`, plus the core stack for the decision
stage), SARVAM_API_KEY and ANTHROPIC_API_KEY, and it spends real money. It also
requires ``VOICE_TEST_GREETING``: ``run_session`` refuses to start without a
recorded consent notice and this repository ships none (docs/build/BLOCKERS.md),
so the path has to be supplied rather than invented here.

The helpers that do not need a room -- item selection, the onset detector and the
rule that decides whether an onset is this item's reply -- are exercised directly
by the unmarked tests at the end of this file, which run in the ordinary gate.
"""

import asyncio
import contextlib
import json
import os
import time
import uuid
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np
import pytest
from helpdesk_agent import voice_pipeline
from indic_platform.eval.runners import run_uc1
from livekit import api, rtc

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden" / "uc1_helpdesk"
MANIFEST = GOLDEN / "manifest.jsonl"
AUDIO = GOLDEN / "audio"

# The prompt's gate: 30 items, p50 <= 2.0s, p95 <= 3.5s, plus action accuracy.
REQUIRED_ITEMS = 30
PER_LANGUAGE = 10
LANGUAGES = ("hi-IN", "te-IN", "ta-IN")
P50_GATE_S = 2.0
P95_GATE_S = 3.5

# The golden WAVs are 16 kHz mono 16-bit; publish them unchanged.
SAMPLE_RATE = 16_000
FRAME_MS = 20
# int16 peak below this counts as silence, for both trailing-silence trimming
# and reply onset detection.
SILENCE_PEAK = 600
VOICE_FRAMES = 3
REPLY_TIMEOUT_S = 20.0
# `SarvamTTSProcessor._synthesize` treats a reply as complete only after no further
# sentence has arrived for this long (`asyncio.wait_for(queue.get(), 2.0)`), and the
# last sentence is flushed after that wait. So the agent can fall silent for two
# seconds in the MIDDLE of a reply. A quiet gap shorter than this would call that
# pause "the previous reply finished", and the flush that follows -- residue from the
# previous item -- would be timed as the next item's first audio.
# Imported, never retyped: a change to the pipeline's timeout must move this gap with
# it, and a silently stale copy here reports a latency better than the real one.
TTS_IDLE_TIMEOUT_S = voice_pipeline.TTS_IDLE_TIMEOUT_S
# And the gap has to outlast the flush itself, not just the wait for it: after the
# iterator ends, Bulbul still has to return the first chunk of that last sentence.
# Two seconds is generous against a gate that allows 3.5s for STT, the graph and TTS
# together. Being too generous costs a few seconds per item; being too mean lets the
# previous turn's tail land inside this item's window, where it is a false alarm at
# best (`_fault` rejects it) and a flattering number at worst.
FLUSH_MARGIN_S = 2.0
QUIET_GAP_S = TTS_IDLE_TIMEOUT_S + FLUSH_MARGIN_S
QUIET_TIMEOUT_S = 90.0
JOIN_TIMEOUT_S = 30.0
# Three dead items in a row means the room is broken, not that the pipeline is
# slow; stop paying for the remaining turns. The item count then fails the gate.
MAX_CONSECUTIVE_PROBLEMS = 3

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
AGENT_IDENTITY = "helpdesk-agent"
HARNESS_IDENTITY = "voice-test"


@dataclass
class Measured:
    """One golden item as this participant experienced it."""

    item_id: str
    language: str
    ttfa_s: float | None = None
    transcript: str | None = None
    decision: dict[str, Any] | None = None
    turns: int = 0
    problem: str | None = None


@dataclass
class SessionRun:
    """One language's session: the room it used and what it produced.

    ``fatal`` is set when the session itself failed -- it never started, never
    published audio, never stopped speaking, or ended mid-run. That is a failure of
    the run, reported by language; it is never allowed to pass as "fewer items".
    """

    language: str
    room: str
    rows: list[Measured] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    fatal: str | None = None
    stopped_early: bool = False

    @property
    def usable(self) -> int:
        return sum(1 for row in self.rows if row.ttfa_s is not None)


def _select() -> list[run_uc1.Item]:
    """The 30 items, chosen deterministically: the first ten spoken ids per language."""
    spoken = [item for item in run_uc1.load_items(MANIFEST) if item.script != "Latn"]
    chosen: list[run_uc1.Item] = []
    for language in LANGUAGES:
        pool = sorted((i for i in spoken if i.language == language), key=lambda i: i.id)
        chosen.extend(pool[:PER_LANGUAGE])
    return chosen


def _pcm(path: Path) -> tuple[bytes, float]:
    """Return the raw PCM of a golden WAV and the seconds of silence at its end."""
    with wave.open(str(path)) as clip:
        if (clip.getframerate(), clip.getnchannels(), clip.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise ValueError(f"{path.name}: expected 16 kHz mono 16-bit PCM")
        data = clip.readframes(clip.getnframes())
    samples = np.frombuffer(data, dtype=np.int16)
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) >= SILENCE_PEAK)
    if loud.size == 0:
        raise ValueError(f"{path.name}: nothing above the silence floor; cannot time an utterance")
    return data, float(samples.size - 1 - int(loud[-1])) / SAMPLE_RATE


def _greeting() -> Path | None:
    """The recorded consent notice to start the session with, if one is configured."""
    configured = os.getenv("VOICE_TEST_GREETING", "").strip()
    path = Path(configured) if configured else None
    return path if path is not None and path.is_file() else None


async def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 80)
    try:
        async with asyncio.timeout(3):
            _, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


async def _blockers(items: list[run_uc1.Item]) -> list[str]:
    reasons: list[str] = []
    if len(items) != REQUIRED_ITEMS:
        reasons.append(f"golden set yielded {len(items)} spoken items, need {REQUIRED_ITEMS}")
    short = [
        language
        for language in LANGUAGES
        if sum(1 for i in items if i.language == language) != PER_LANGUAGE
    ]
    if short:
        reasons.append(
            f"fewer than {PER_LANGUAGE} spoken golden items for {', '.join(short)}: each "
            "language is run as its own session and must supply its own ten items"
        )
    missing = [i.id for i in items if not (AUDIO / f"{i.id}.wav").is_file()]
    if missing:
        reasons.append(
            f"golden audio missing for {', '.join(missing)} (see program/P1-golden-audio)"
        )
    if os.getenv("LIVE_API_TESTS") != "1":
        reasons.append("LIVE_API_TESTS=1 required: this gate makes paid Sarvam and Claude calls")
    if not _greeting():
        reasons.append(
            "VOICE_TEST_GREETING is unset or not a file: run_session refuses to start without a "
            "recorded consent notice, and no such asset exists in this repository (see "
            "docs/build/BLOCKERS.md). Point it at the approved notice, or -- for a "
            "latency-only run, where the only participant is this harness and no employee is "
            "being recorded -- at a labelled stand-in"
        )
    for key in ("SARVAM_API_KEY", "ANTHROPIC_API_KEY", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        if not os.getenv(key):
            reasons.append(f"{key} is not set")
    if not await _reachable(LIVEKIT_URL):
        reasons.append(
            f"no LiveKit listening at {LIVEKIT_URL}: run `make stack-voice` (needs Docker)"
        )
    return reasons


def _token(room: str, identity: str) -> str:
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grants)
        .to_jwt()
    )


def _switches_to_chat(payload: bytes) -> bool:
    """Best-effort read of the pipeline's STT-failure message (contract: switch to chat).

    Degrading to chat mid-run means STT died, so whatever audio did arrive is not
    a reply latency. Recognising it is deliberately narrow: it only ever turns a
    run into a failure, never into a pass.
    """
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(message, dict):
        return False
    return message.get("mode") == "chat" or message.get("type") in {
        "chat_mode",
        "mode_switch",
        "degraded",
        "stt_failed",
    }


def _is_voice(samples: np.ndarray) -> bool:
    """Whether one audio frame is above the silence floor. Pure; unit-tested below."""
    if samples.size == 0:
        return False
    return int(np.abs(samples.astype(np.int32)).max()) >= SILENCE_PEAK


class OnsetRun:
    """Counts consecutive voiced frames and reports where the current run began.

    Split out of :class:`AgentAudio` so the rule that turns frames into an onset can
    be proven without a room: a run of at least ``VOICE_FRAMES`` voiced frames is a
    reply, anything shorter is a click, and one silent frame resets the count. The
    instant reported is always the *first* frame of the run, not the frame that
    confirmed it.
    """

    def __init__(self, frames: int = VOICE_FRAMES) -> None:
        self._frames = frames
        self._run = 0
        self._started: float | None = None

    def feed(self, at: float, voiced: bool) -> float | None:
        """Return the start of the confirmed run, or ``None`` while it is unconfirmed."""
        if not voiced:
            self._run, self._started = 0, None
            return None
        self._run += 1
        if self._run == 1:
            self._started = at
        if self._run >= self._frames:
            return self._started
        return None


class AgentAudio:
    """Reply onsets on the agent's track, timed at the moment frames arrive here."""

    def __init__(self) -> None:
        self.published = asyncio.Event()
        self.tracks = 0
        self._armed_at: float | None = None
        self._onset: asyncio.Future[float] | None = None
        self._last_voice_at = time.monotonic()
        self._readers: set[asyncio.Task[None]] = set()

    def consume(self, track: rtc.Track) -> None:
        self.tracks += 1
        reader = asyncio.create_task(self._read(track))
        self._readers.add(reader)
        reader.add_done_callback(self._readers.discard)
        self.published.set()

    async def _read(self, track: rtc.Track) -> None:
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        try:
            run = OnsetRun()
            async for event in stream:
                now = time.monotonic()
                voiced = _is_voice(np.frombuffer(bytes(event.frame.data), dtype=np.int16))
                if voiced:
                    self._last_voice_at = now
                started = run.feed(now, voiced)
                if started is not None:
                    self._mark(started)
        finally:
            await stream.aclose()

    def arm(self) -> None:
        """Ignore everything heard so far; the next reply starts from here."""
        self._armed_at = time.monotonic()
        self._onset = asyncio.get_running_loop().create_future()

    def _mark(self, onset: float) -> None:
        pending = self._onset
        armed = self._armed_at
        if pending is not None and not pending.done() and armed is not None and onset >= armed:
            pending.set_result(onset)

    async def onset(self) -> float:
        if self._onset is None:
            raise RuntimeError("arm() before waiting for a reply onset")
        return await self._onset

    async def quiet(self, gap: float) -> None:
        """Return once no agent audio has arrived for `gap` seconds."""
        while True:
            idle = time.monotonic() - self._last_voice_at
            if idle >= gap:
                return
            await asyncio.sleep(gap - idle)

    async def aclose(self) -> None:
        for reader in list(self._readers):
            reader.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)


def _fault(
    *,
    turns_taken: int,
    ttfa_s: float,
    first_audio: float,
    decided_at: float | None,
) -> str | None:
    """Why this onset is not a measurement of this item's reply, or ``None`` if it is.

    Pure, so every branch is provable offline. The decision-time check is the one
    that catches residue: TTS cannot produce audio for a decision that has not
    returned yet, so an onset earlier than that instant belongs to the previous
    turn no matter how quiet the room looked beforehand.
    """
    if turns_taken == 0:
        return "audio arrived but the pipeline never reached a decision"
    if decided_at is None:
        return "the decision for this item carries no time, so no onset can be attributed to it"
    if first_audio < decided_at:
        return (
            f"agent audio began {decided_at - first_audio:.2f}s before this item's decision "
            "returned, so it was the tail of the previous reply, not this item's first audio"
        )
    if ttfa_s <= 0:
        return f"agent audio began {-ttfa_s:.2f}s before the utterance ended"
    return None


async def _play(source: rtc.AudioSource, pcm: bytes) -> None:
    """Publish the clip in real time and return when the last sample is out."""
    step = SAMPLE_RATE * FRAME_MS // 1000 * 2
    for offset in range(0, len(pcm), step):
        chunk = pcm[offset : offset + step]
        await source.capture_frame(rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // 2))
    await source.wait_for_playout()


def _died(agent: asyncio.Task[None] | None) -> str | None:
    """The agent session's failure, if it has already ended."""
    if agent is None or not agent.done():
        return None
    if agent.cancelled():
        return "run_session was cancelled"
    error = agent.exception()
    return f"{type(error).__name__}: {error}" if error else "run_session returned early"


async def _run_language(
    language: str,
    items: list[run_uc1.Item],
    clips: dict[str, tuple[bytes, float]],
    room_name: str,
) -> SessionRun:
    """Run one language's ten items through its own room and its own session.

    Every way this can go wrong is returned, never raised: the caller has already
    paid for whatever earlier languages produced and must be able to print it. A
    session-level failure comes back as ``fatal`` (named by language) and fails the
    run; a per-item failure comes back as a row with a ``problem``.
    """
    from helpdesk_agent.graph import decide as graph_decide
    from helpdesk_agent.voice_pipeline import VoiceSettings, run_session

    result = SessionRun(language=language, room=room_name)
    turns: list[dict[str, Any]] = []

    async def decide(
        utterance: str, language_of_session: str, history: list[Any]
    ) -> dict[str, Any]:
        """The pipeline's decision stage, recorded at the point it is taken.

        ``returned_at`` is the earliest instant at which any audio for this turn could
        exist; :func:`_fault` refuses any onset older than it.
        """
        decision = await graph_decide(utterance, language_of_session, history)
        turns.append(
            {
                "transcript": utterance,
                "decision": decision,
                "returned_at": time.monotonic(),
            }
        )
        return decision

    agent: asyncio.Task[None] | None = None
    audio = AgentAudio()
    joined = asyncio.Event()
    room = rtc.Room()

    @room.on("participant_connected")
    def _on_participant(participant: rtc.RemoteParticipant) -> None:
        if participant.identity == AGENT_IDENTITY:
            joined.set()

    @room.on("track_subscribed")
    def _on_track(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if participant.identity == AGENT_IDENTITY and track.kind == rtc.TrackKind.KIND_AUDIO:
            audio.consume(track)

    @room.on("data_received")
    def _on_data(packet: rtc.DataPacket) -> None:
        if _switches_to_chat(packet.data):
            result.degraded.append(packet.data.decode("utf-8", "replace")[:200])

    source = rtc.AudioSource(SAMPLE_RATE, 1)
    try:
        await room.connect(LIVEKIT_URL, _token(room_name, HARNESS_IDENTITY))
        track = rtc.LocalAudioTrack.create_audio_track(HARNESS_IDENTITY, source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        # The caller joins and publishes before the agent is started, as on a real
        # call: the employee is in the room to hear the consent notice. The harness
        # does not depend on when the pipeline emits it -- it simply waits for the
        # room to go quiet below before timing the first item.
        agent = asyncio.create_task(
            run_session(
                VoiceSettings(
                    room=room_name,
                    livekit_url=LIVEKIT_URL,
                    identity=AGENT_IDENTITY,
                    # Not decoration: this picks the decision language and the Bulbul
                    # speaker, so it must be the language of the items played below.
                    language=language,
                    greeting_path=_greeting(),
                ),
                decide=decide,
                session_id=str(uuid.uuid4()),
                employee_id=HARNESS_IDENTITY,
            )
        )
        if any(p.identity == AGENT_IDENTITY for p in room.remote_participants.values()):
            joined.set()
        try:
            async with asyncio.timeout(JOIN_TIMEOUT_S):
                await joined.wait()
        except TimeoutError:
            died = _died(agent)
            result.fatal = f"the agent never joined room {room_name} within {JOIN_TIMEOUT_S:.0f}s"
            result.fatal += f": {died}" if died else ""
            return result
        try:
            async with asyncio.timeout(JOIN_TIMEOUT_S):
                await audio.published.wait()
        except TimeoutError:
            died = _died(agent)
            result.fatal = f"the agent published no audio track within {JOIN_TIMEOUT_S:.0f}s"
            result.fatal += f": {died}" if died else ""
            return result

        # The recorded-notice greeting plays before any user audio is consumed;
        # let it finish so item 1 is timed against silence like every other item.
        try:
            async with asyncio.timeout(QUIET_TIMEOUT_S):
                await audio.quiet(QUIET_GAP_S)
        except TimeoutError:
            result.fatal = (
                f"the agent never stopped speaking after joining ({QUIET_TIMEOUT_S:.0f}s)"
            )
            return result

        consecutive = 0
        for index, item in enumerate(items, start=1):
            row = Measured(item.id, item.language)
            result.rows.append(row)
            died = _died(agent)
            if died:
                row.problem = f"the agent session ended: {died}"
                result.fatal = f"run_session ended during item {index}/{PER_LANGUAGE}: {died}"
                break
            try:
                async with asyncio.timeout(QUIET_TIMEOUT_S):
                    await audio.quiet(QUIET_GAP_S)
            except TimeoutError:
                row.problem = "the previous reply never ended; no silence to time against"
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_PROBLEMS:
                    result.stopped_early = True
                    break
                continue

            before = len(turns)
            pcm, trailing = clips[item.id]
            audio.arm()
            await _play(source, pcm)
            end_of_speech = time.monotonic() - trailing
            try:
                async with asyncio.timeout(REPLY_TIMEOUT_S):
                    first_audio = await audio.onset()
            except TimeoutError:
                row.problem = f"no agent audio within {REPLY_TIMEOUT_S:.0f}s of the utterance"
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_PROBLEMS:
                    result.stopped_early = True
                    break
                continue

            ttfa = first_audio - end_of_speech
            row.turns = len(turns) - before
            turn = turns[before] if row.turns else None
            row.problem = _fault(
                turns_taken=row.turns,
                ttfa_s=ttfa,
                first_audio=first_audio,
                decided_at=float(turn["returned_at"]) if turn is not None else None,
            )
            if row.problem is None and turn is not None:
                row.ttfa_s = ttfa
                row.transcript = str(turn["transcript"])
                row.decision = dict(turn["decision"])
            consecutive = 0 if row.problem is None else consecutive + 1
            print(
                f"voice-test: {language} {index:>2}/{PER_LANGUAGE} {item.id} "
                + (f"ttfa={ttfa:.3f}s turns={row.turns}" if row.problem is None else row.problem),
                flush=True,
            )
            if consecutive >= MAX_CONSECUTIVE_PROBLEMS:
                result.stopped_early = True
                break
    except asyncio.CancelledError:
        raise
    except Exception as error:  # reported through `fatal`, never swallowed
        result.fatal = f"the {language} session raised {type(error).__name__}: {error}"
    finally:
        if agent is not None:
            agent.cancel()
            with contextlib.suppress(BaseException):
                await agent
        await audio.aclose()
        with contextlib.suppress(Exception):
            await room.disconnect()
        with contextlib.suppress(Exception):
            await source.aclose()
    return result


@pytest.mark.voice
async def test_voice_time_to_first_audio_and_action_accuracy() -> None:
    items = _select()
    blockers = await _blockers(items)
    if blockers:
        reason = "voice-test measured nothing (UNMEASURED): " + "; ".join(blockers)
        if os.getenv("VOICE_TEST_REQUIRE") == "1":
            pytest.fail(reason)
        pytest.skip(reason)

    # Percentiles are only comparable across runs if the audio behind them is the
    # same audio; this raises on any drift from the checked-in checksums.
    run_uc1.verify_audio(run_uc1.load_items(MANIFEST), AUDIO, GOLDEN / "audio_manifest.jsonl")
    clips = {item.id: await asyncio.to_thread(_pcm, AUDIO / f"{item.id}.wav") for item in items}

    base = f"voice-test-{uuid.uuid4().hex[:8]}"
    runs: list[SessionRun] = []
    skipped: list[str] = []
    for language in LANGUAGES:
        spoken = [item for item in items if item.language == language]
        if runs and (runs[-1].fatal or runs[-1].stopped_early):
            # The run can no longer measure all 30 items, so it has already failed.
            # Every further item would spend real Saaras, Bulbul and Claude money on
            # a verdict that cannot change; the unattempted languages are named below.
            skipped.append(language)
            continue
        print(f"\nvoice-test: starting the {language} session ({len(spoken)} items)", flush=True)
        runs.append(await _run_language(language, spoken, clips, f"{base}-{language.lower()}"))

    measured = [row for run in runs for row in run.rows]
    degraded = [f"{run.language}: {message}" for run in runs for message in run.degraded]
    fatals = [f"{run.language}: {run.fatal}" for run in runs if run.fatal]

    # --- report before asserting, so a paid run yields every number it earned ---
    latencies = [row.ttfa_s for row in measured if row.ttfa_s is not None]
    problems = [f"{row.item_id}: {row.problem}" for row in measured if row.problem]
    for run in runs:
        line = f"voice-test: {run.language} session in {run.room}: {run.usable}/{PER_LANGUAGE} "
        line += f"items measured ({len(run.rows)} attempted)"
        if run.fatal:
            line += " -- SESSION DIED, see below"
        elif run.stopped_early:
            line += f" -- stopped after {MAX_CONSECUTIVE_PROBLEMS} consecutive problems"
        print(line, flush=True)
    for language in skipped:
        print(
            f"voice-test: {language} session 0/{PER_LANGUAGE} items measured: never started, "
            "because an earlier language failed and the run was stopped rather than billed",
            flush=True,
        )
    print(
        f"\nvoice-test: {len(latencies)} items measured of {REQUIRED_ITEMS} required "
        f"({len(measured)} attempted across {len(runs)} of {len(LANGUAGES)} per-language "
        f"sessions, {PER_LANGUAGE} items each); both percentiles below are over those "
        f"{len(latencies)} measured items and no others",
        flush=True,
    )
    if latencies:
        print(
            f"voice-test: time-to-first-audio n={len(latencies)} "
            f"p50={run_uc1.percentile(latencies, 0.5):.3f}s "
            f"p95={run_uc1.percentile(latencies, 0.95):.3f}s "
            f"min={min(latencies):.3f}s max={max(latencies):.3f}s "
            f"(gates p50<={P50_GATE_S}s p95<={P95_GATE_S}s)"
        )
    else:
        print("voice-test: time-to-first-audio UNMEASURED: no item produced agent audio")
    for fatal in fatals:
        print(f"voice-test: SESSION DIED {fatal}")
    for problem in problems:
        print(f"voice-test: PROBLEM {problem}")

    accuracy: float | None = None
    accuracy_passed = False
    if len(measured) == REQUIRED_ITEMS and all(row.decision is not None for row in measured):
        report = await _score(items, measured)
        accuracy = report.metrics["action_accuracy"]
        accuracy_passed = bool(report.quality_gates["action_accuracy"])
        print(
            f"voice-test: action accuracy {accuracy:.1%} over {len(items)} voice items "
            f"pooled from {len(runs)} per-language sessions "
            f"(run_uc1 B6 gate: {'pass' if accuracy_passed else 'FAIL'})"
        )
    else:
        print("voice-test: action accuracy UNMEASURED: not every item reached a decision")

    assert not fatals, (
        f"{len(fatals)} of the {len(LANGUAGES)} per-language sessions died, so those items were "
        f"never measured and the run is not a measurement of the gate: {fatals}"
    )
    assert not degraded, f"the pipeline switched the client to chat mid-run: {degraded[0]}"
    assert not problems, (
        f"{len(problems)} of {len(measured)} items produced no usable measurement, so the "
        f"percentiles would be an average over the items that happened to work: {problems}"
    )
    assert len(latencies) == REQUIRED_ITEMS, (
        f"measured {len(latencies)} items; the gate requires all {REQUIRED_ITEMS} "
        f"({len(measured)} attempted"
        + (f", languages never attempted: {', '.join(skipped)}" if skipped else "")
        + ")"
    )
    p50 = run_uc1.percentile(latencies, 0.5)
    p95 = run_uc1.percentile(latencies, 0.95)
    assert p50 <= P50_GATE_S, f"time-to-first-audio p50 {p50:.3f}s > {P50_GATE_S}s over n=30 items"
    assert p95 <= P95_GATE_S, f"time-to-first-audio p95 {p95:.3f}s > {P95_GATE_S}s over n=30 items"
    assert accuracy is not None, "action accuracy was not measured on the 30 voice items"
    assert accuracy_passed, (
        f"action accuracy {accuracy:.1%} over the {len(items)} voice items fails run_uc1's "
        "B6 gate (the same threshold `make eval-uc1` applies)"
    )


async def _score(items: list[run_uc1.Item], measured: list[Measured]) -> run_uc1.Report:
    """Action accuracy on exactly what the rooms produced, scored by run_uc1.evaluate.

    The transcripts and decisions are the live ones from the three voice sessions,
    pooled into one set of 30; they are replayed into the existing scorer rather than
    rescored here, so the harness cannot drift from `make eval-uc1`.
    """
    rows = {row.item_id: row for row in measured}
    current: list[str] = []

    async def replay_stt(path: Path, language: str) -> str:
        current.append(path.stem)
        transcript = rows[path.stem].transcript
        assert transcript is not None
        return transcript

    async def replay_decide(utterance: str, language: str, history: list[Any]) -> dict[str, Any]:
        row = rows[current[-1]]
        assert row.decision is not None and row.transcript == utterance
        return row.decision

    return await run_uc1.evaluate(items, AUDIO, stt=replay_stt, decide=replay_decide)


# --- offline proofs of the parts that do not need a room -------------------------


def test_selection_is_ten_spoken_items_per_language_deterministically() -> None:
    chosen = _select()
    assert len(chosen) == REQUIRED_ITEMS == PER_LANGUAGE * len(LANGUAGES)
    assert [i.id for i in chosen] == [i.id for i in _select()]
    assert not any(i.script == "Latn" for i in chosen), "Latn items have no golden audio"
    for language in LANGUAGES:
        block = [i for i in chosen if i.language == language]
        assert len(block) == PER_LANGUAGE
        assert [i.id for i in block] == sorted(i.id for i in block)
    # The three blocks are contiguous and in LANGUAGES order, which is what lets each
    # session be handed exactly its own ten items.
    assert [i.language for i in chosen] == [lang for lang in LANGUAGES for _ in range(PER_LANGUAGE)]


def test_is_voice_separates_speech_from_silence_and_empty_frames() -> None:
    assert not _is_voice(np.zeros(0, dtype=np.int16))
    assert not _is_voice(np.zeros(320, dtype=np.int16))
    assert not _is_voice(np.full(320, SILENCE_PEAK - 1, dtype=np.int16))
    assert _is_voice(np.full(320, SILENCE_PEAK, dtype=np.int16))
    # A negative excursion is speech too; the floor is on the absolute peak.
    assert _is_voice(np.full(320, -SILENCE_PEAK, dtype=np.int16))


def test_onset_run_needs_consecutive_voiced_frames_and_reports_the_first() -> None:
    run = OnsetRun()
    # A lone click, then silence: no onset, and the run restarts from scratch.
    assert run.feed(1.0, True) is None
    assert run.feed(1.02, False) is None
    assert run.feed(1.04, True) is None
    assert run.feed(1.06, True) is None
    # The third consecutive voiced frame confirms it, dated to the first of the run.
    assert run.feed(1.08, True) == 1.04
    # Still speaking: the same onset, never a later one.
    assert run.feed(1.10, True) == 1.04
    # Silence ends the run; the next reply gets its own onset.
    assert run.feed(1.12, False) is None
    assert run.feed(2.00, True) is None
    assert run.feed(2.02, True) is None
    assert run.feed(2.04, True) == 2.00


def test_quiet_gap_outlasts_the_pipelines_inter_sentence_idle_timeout() -> None:
    # `SarvamTTSProcessor._synthesize` waits TTS_IDLE_TIMEOUT_S for the next sentence
    # before flushing the last one, so a shorter gap would call a mid-reply pause
    # "finished" and time the flush as the next item's first audio.
    assert QUIET_GAP_S >= TTS_IDLE_TIMEOUT_S + FLUSH_MARGIN_S > TTS_IDLE_TIMEOUT_S
    assert REPLY_TIMEOUT_S > QUIET_GAP_S
    assert QUIET_TIMEOUT_S > QUIET_GAP_S


def test_fault_accepts_only_audio_that_follows_this_items_decision() -> None:
    decided_at = 100.0
    # The good case: the decision returned, then audio, after the utterance ended.
    assert _fault(turns_taken=1, ttfa_s=1.2, first_audio=100.5, decided_at=decided_at) is None
    # Audio, but the pipeline never reached a decision at all.
    assert _fault(turns_taken=0, ttfa_s=1.2, first_audio=100.5, decided_at=None) == (
        "audio arrived but the pipeline never reached a decision"
    )
    # Residue: the onset predates the instant this item's decision returned, so it
    # cannot be this item's reply however quiet the room looked beforehand.
    residue = _fault(turns_taken=1, ttfa_s=0.4, first_audio=99.6, decided_at=decided_at)
    assert residue is not None and "before this item's decision returned" in residue
    # An onset exactly at the decision instant is allowed: it is the earliest audio
    # for this turn that could physically exist.
    assert _fault(turns_taken=1, ttfa_s=0.4, first_audio=decided_at, decided_at=decided_at) is None
    # A decision with no recorded time can never be attributed to an onset.
    unattributable = _fault(turns_taken=1, ttfa_s=1.2, first_audio=100.5, decided_at=None)
    assert unattributable is not None and "carries no time" in unattributable
    # Audio that started before the caller stopped speaking is not a reply either.
    early = _fault(turns_taken=1, ttfa_s=-0.3, first_audio=100.5, decided_at=decided_at)
    assert early is not None and "before the utterance ended" in early


def test_pcm_charges_only_speech_and_rejects_a_silent_clip(tmp_path: Path) -> None:
    speech = np.full(SAMPLE_RATE, 4000, dtype=np.int16)
    clip = np.concatenate([speech, np.zeros(SAMPLE_RATE // 2, dtype=np.int16)])
    path = tmp_path / "item.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(clip.tobytes())
    data, trailing = _pcm(path)
    # The whole file is published, but only up to the last loud sample is timed.
    assert len(data) == clip.size * 2
    assert trailing == pytest.approx(0.5, abs=1 / SAMPLE_RATE)

    silent = tmp_path / "silent.wav"
    with wave.open(str(silent), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(np.zeros(SAMPLE_RATE, dtype=np.int16).tobytes())
    with pytest.raises(ValueError, match="cannot time an utterance"):
        _pcm(silent)


def test_switches_to_chat_recognises_the_pipelines_degradation_message() -> None:
    assert _switches_to_chat(json.dumps({"type": "mode", "mode": "chat"}).encode())
    assert _switches_to_chat(json.dumps({"type": "stt_failed"}).encode())
    assert not _switches_to_chat(json.dumps({"type": "transcript", "text": "hi"}).encode())
    assert not _switches_to_chat(b"\xff\xfe not json")
    assert not _switches_to_chat(json.dumps([1, 2, 3]).encode())
