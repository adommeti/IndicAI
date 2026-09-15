"""`make voice-test`: the UC1 voice latency gate, measured from LiveKit track events.

This joins the room the voice pipeline is serving as an ordinary participant,
plays the 30 golden WAVs through a published microphone track, and times the
agent's reply the way a caller would hear it: from the moment the caller stops
speaking to the first frame of agent audio that arrives on the subscribed track.
Nothing here trusts a timer the pipeline keeps about itself.

What it measures, precisely:

* ``t0`` is the instant the last *non-silent* sample of the utterance was played
  out locally. The file's trailing silence is still played -- the pipeline's VAD
  needs it to end the turn -- but it is not charged to the pipeline, so the
  number is time-from-end-of-speech rather than time-from-end-of-file.
* ``t1`` is the arrival time of the first frame of a run of at least
  ``VOICE_FRAMES``   consecutive non-silent frames on the agent's audio track,
  taken from ``rtc.AudioStream``. A lone click cannot be mistaken for a reply.
* ``time-to-first-audio = t1 - t0``, per item.

The gate is built to be able to fail. Every item must produce a measurement:
an item that yields no agent audio, or no decision, is recorded as a problem and
fails the test -- it is never dropped so that the survivors can be averaged into
a flattering percentile. The printed summary always states how many items were
measured out of how many were required, and that count is the denominator of
both percentiles.

Preconditions (LiveKit reachable, keys present, golden audio present) SKIP, and
a skipped run measured nothing: it is UNMEASURED, not a pass. Set
``VOICE_TEST_REQUIRE=1`` to turn those skips into failures.

Requires Docker (`make stack-voice`, plus the core stack for the decision
stage), SARVAM_API_KEY and ANTHROPIC_API_KEY, and it spends real money. It also
requires ``VOICE_TEST_GREETING``: ``run_session`` refuses to start without a
recorded consent notice and this repository ships none (docs/build/BLOCKERS.md),
so the path has to be supplied rather than invented here.
"""

import asyncio
import contextlib
import json
import os
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import numpy as np
import pytest
from indic_platform.eval.runners import run_uc1
from livekit import api, rtc

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden" / "uc1_helpdesk"
MANIFEST = GOLDEN / "manifest.jsonl"
AUDIO = GOLDEN / "audio"

# The prompt's gate: 30 items, p50 <= 2.0s, p95 <= 3.5s, plus action accuracy.
REQUIRED_ITEMS = 30
PER_LANGUAGE = 10
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
QUIET_GAP_S = 1.0
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


def _select() -> list[run_uc1.Item]:
    """The 30 items, chosen deterministically: the first ten spoken ids per language."""
    spoken = [item for item in run_uc1.load_items(MANIFEST) if item.script != "Latn"]
    chosen: list[run_uc1.Item] = []
    for language in ("hi-IN", "te-IN", "ta-IN"):
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
            run = 0
            started: float | None = None
            async for event in stream:
                now = time.monotonic()
                samples = np.frombuffer(bytes(event.frame.data), dtype=np.int16)
                loud = bool(samples.size) and int(np.abs(samples.astype(np.int32)).max()) >= (
                    SILENCE_PEAK
                )
                if not loud:
                    run, started = 0, None
                    continue
                self._last_voice_at = now
                run += 1
                if run == 1:
                    started = now
                if run >= VOICE_FRAMES and started is not None:
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


async def _play(source: rtc.AudioSource, pcm: bytes) -> None:
    """Publish the clip in real time and return when the last sample is out."""
    step = SAMPLE_RATE * FRAME_MS // 1000 * 2
    for offset in range(0, len(pcm), step):
        chunk = pcm[offset : offset + step]
        await source.capture_frame(rtc.AudioFrame(chunk, SAMPLE_RATE, 1, len(chunk) // 2))
    await source.wait_for_playout()


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

    from helpdesk_agent.graph import decide as graph_decide
    from helpdesk_agent.voice_pipeline import VoiceSettings, run_session

    turns: list[dict[str, Any]] = []

    async def decide(utterance: str, language: str, history: list[Any]) -> dict[str, Any]:
        """The pipeline's decision stage, recorded at the point it is taken."""
        decision = await graph_decide(utterance, language, history)
        turns.append({"transcript": utterance, "decision": decision})
        return decision

    room_name = f"voice-test-{uuid.uuid4().hex[:8]}"
    agent: asyncio.Task[None] | None = None
    audio = AgentAudio()
    joined = asyncio.Event()
    degraded: list[str] = []
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
            degraded.append(packet.data.decode("utf-8", "replace")[:200])

    measured: list[Measured] = []
    source = rtc.AudioSource(SAMPLE_RATE, 1)
    try:
        # The port was open, so a failure from here on is a real failure, not a
        # missing stack: it fails the gate rather than skipping it.
        await room.connect(LIVEKIT_URL, _token(room_name, HARNESS_IDENTITY))
        track = rtc.LocalAudioTrack.create_audio_track(HARNESS_IDENTITY, source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        # The caller is in the room first, so the consent notice is played to a
        # participant rather than into an empty room, as it would be on a real call.
        agent = asyncio.create_task(
            run_session(
                VoiceSettings(
                    room=room_name,
                    livekit_url=LIVEKIT_URL,
                    identity=AGENT_IDENTITY,
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
            pytest.fail(
                f"the agent never joined room {room_name} within {JOIN_TIMEOUT_S:.0f}s"
                + (f": {died}" if died else "")
            )
        try:
            async with asyncio.timeout(JOIN_TIMEOUT_S):
                await audio.published.wait()
        except TimeoutError:
            died = _died(agent)
            pytest.fail(
                f"the agent published no audio track within {JOIN_TIMEOUT_S:.0f}s"
                + (f": {died}" if died else "")
            )

        # The recorded-notice greeting plays before any user audio is consumed;
        # let it finish so item 1 is timed against silence like every other item.
        try:
            async with asyncio.timeout(QUIET_TIMEOUT_S):
                await audio.quiet(QUIET_GAP_S)
        except TimeoutError:
            pytest.fail(f"the agent never stopped speaking after joining ({QUIET_TIMEOUT_S:.0f}s)")

        consecutive = 0
        for index, item in enumerate(items, start=1):
            row = Measured(item.id, item.language)
            measured.append(row)
            died = _died(agent)
            if died:
                row.problem = f"the agent session ended: {died}"
                break
            try:
                async with asyncio.timeout(QUIET_TIMEOUT_S):
                    await audio.quiet(QUIET_GAP_S)
            except TimeoutError:
                row.problem = "the previous reply never ended; no silence to time against"
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_PROBLEMS:
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
                    break
                continue

            ttfa = first_audio - end_of_speech
            row.turns = len(turns) - before
            if row.turns == 0:
                row.problem = "audio arrived but the pipeline never reached a decision"
            elif ttfa <= 0:
                row.problem = f"agent audio began {-ttfa:.2f}s before the utterance ended"
            else:
                row.ttfa_s = ttfa
                row.transcript = str(turns[before]["transcript"])
                row.decision = dict(turns[before]["decision"])
            consecutive = 0 if row.problem is None else consecutive + 1
            print(
                f"voice-test: {index:>2}/{REQUIRED_ITEMS} {item.id} "
                + (f"ttfa={ttfa:.3f}s turns={row.turns}" if row.problem is None else row.problem),
                flush=True,
            )
            if consecutive >= MAX_CONSECUTIVE_PROBLEMS:
                break
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

    # --- report before asserting, so a paid run yields every number it earned ---
    latencies = [row.ttfa_s for row in measured if row.ttfa_s is not None]
    problems = [f"{row.item_id}: {row.problem}" for row in measured if row.problem]
    print(
        f"\nvoice-test: {len(latencies)} items measured of {REQUIRED_ITEMS} required "
        f"({len(measured)} attempted); both percentiles below are over those "
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
            f"(run_uc1 B6 gate: {'pass' if accuracy_passed else 'FAIL'})"
        )
    else:
        print("voice-test: action accuracy UNMEASURED: not every item reached a decision")

    assert not degraded, f"the pipeline switched the client to chat mid-run: {degraded[0]}"
    assert not problems, (
        f"{len(problems)} of {len(measured)} items produced no usable measurement, so the "
        f"percentiles would be an average over the items that happened to work: {problems}"
    )
    assert len(latencies) == REQUIRED_ITEMS, (
        f"measured {len(latencies)} items; the gate requires all {REQUIRED_ITEMS} "
        f"({len(measured)} attempted)"
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


def _died(agent: asyncio.Task[None] | None) -> str | None:
    """The agent session's failure, if it has already ended."""
    if agent is None or not agent.done():
        return None
    if agent.cancelled():
        return "run_session was cancelled"
    error = agent.exception()
    return f"{type(error).__name__}: {error}" if error else "run_session returned early"


async def _score(items: list[run_uc1.Item], measured: list[Measured]) -> run_uc1.Report:
    """Action accuracy on exactly what the room produced, scored by run_uc1.evaluate.

    The transcripts and decisions are the live ones from the voice run; they are
    replayed into the existing scorer rather than rescored here, so the harness
    cannot drift from `make eval-uc1`.
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
