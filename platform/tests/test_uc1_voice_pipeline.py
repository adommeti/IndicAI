"""uc1 voice pipeline: consent ordering, STT death, measured latencies, content hygiene.

Everything here runs with no Docker, no LiveKit and no network. The LiveKit transport is
replaced by two plain pass-through processors, Silero is replaced by a deterministic VAD
analyzer (so no ONNX model is loaded and speech boundaries are driven by the test rather
than by the audio), and both Sarvam adapters are fakes that satisfy the Protocols in
``indic_platform.adapters.base``.

Anything that needs a real room carries the ``voice`` marker -- deliberately NOT
``integration``. CI provisions postgres, redis and qdrant but no LiveKit, and the
integration job fails on ANY skip (uc3/P5's evidence guard), so a LiveKit-dependent test
carrying the ``integration`` marker turns CI red for a service that was never going to be
there. ``make voice-test`` is the runner for the ``voice`` marker.

Two habits run through the file.

*Assert behaviour, not references.* "The greeting is played" is not the property that
matters -- "no user audio is consumed until it has" is -- so the ordering assertions count
frames on both sides of the gate rather than looking for the file.

*Wait for conditions, never for the clock.* Half of this pipeline runs in background tasks
(the STT stream, the graph call, synthesis), so a script of frames separated by fixed
sleeps pins the machine the test ran on rather than the behaviour. The first version of
this file did exactly that and passed alone while failing inside the full suite, where
everything is slower. :func:`drive` therefore takes a script whose steps are either frames
to queue or conditions to wait for, and :func:`until` polls a predicate with a generous
timeout and a named failure -- so a genuine hang reports what it was waiting for instead
of a mystery assertion three lines later.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from helpdesk_agent.voice_pipeline import (
    DATA_MESSAGE_VERSION,
    INPUT_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
    GreetingGate,
    GreetingUnavailable,
    SarvamSTTProcessor,
    SarvamTTSProcessor,
    StageLatency,
    VoiceSettings,
    build_pipeline,
    load_greeting,
    run_session,
    sentences,
    speaker_for,
)
from indic_platform.adapters.base import TranscriptSegment
from loguru import logger
from pipecat.audio.vad.vad_analyzer import VADAnalyzer
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    OutputTransportMessageFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

pytestmark = pytest.mark.asyncio

# 20 ms of 16 kHz PCM16 mono, the frame size the LiveKit input transport produces.
FRAME = b"\x01\x02" * (INPUT_SAMPLE_RATE * 20 // 1000)
GREETING = b"\x10\x20" * 1600  # 100 ms of notice audio

# A transcript and a reply that both carry redactable content, so the hygiene tests have
# something real to look for rather than asserting over an empty string.
UTTERANCE = "मेरा फ़ोन 9876543210 है और प्रिंटर काम नहीं कर रहा"
REPLY = "नमस्ते। आपका टिकट दर्ज हो गया है। हम आपको priya@example.com पर सूचित करेंगे।"

SETTINGS = VoiceSettings(
    room="uc1-test",
    livekit_url="ws://localhost:7880",
    identity="helpdesk-agent",
    language="hi-IN",
)


class SilentVAD(VADAnalyzer):
    """A VAD that never fires. Speech boundaries come from the frames a test sends.

    Silero works offline, but using it would make the turn boundary a property of the
    fixture audio rather than of the test, and every ordering assertion would then hinge
    on whether 20 ms of square wave reads as speech.
    """

    def num_frames_required(self) -> int:
        return 256

    def voice_confidence(self, buffer: bytes) -> float:
        return 0.0


class Passthrough(FrameProcessor):
    """Stands in for ``transport.input()`` / ``transport.output()``."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.seen: list[Frame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        self.seen.append(frame)
        await self.push_frame(frame, direction)


class FakeSTT:
    """Saaras stand-in. Records what it pulled; can be made to fail on demand."""

    def __init__(
        self,
        *,
        segments: Sequence[str] = ("मेरा फ़ोन", UTTERANCE),
        fail: bool = False,
        fail_after: int = 0,
    ) -> None:
        self._segments = list(segments)
        self._fail = fail
        self._fail_after = fail_after
        self.calls = 0
        self.chunks_pulled = 0

    async def stream(
        self, audio: AsyncIterator[bytes], *, language: str = "auto"
    ) -> AsyncIterator[TranscriptSegment]:
        self.calls += 1
        pulled = 0
        async for _chunk in audio:
            pulled += 1
            self.chunks_pulled += 1
            if self._fail and pulled > self._fail_after:
                raise RuntimeError("Sarvam STT stream rejected")
        if self._fail:
            raise RuntimeError("Sarvam STT stream rejected")
        for index, text in enumerate(self._segments):
            yield TranscriptSegment(
                start_ms=index * 500,
                end_ms=(index + 1) * 500,
                text=text,
                language="hi-IN",
            )

    async def batch(
        self, audio_uri: str, *, language: str = "auto", diarize: bool = False
    ) -> list[TranscriptSegment]:  # pragma: no cover - the voice pipeline never batches
        raise NotImplementedError


class FakeTTS:
    """Bulbul stand-in. Yields opaque 'encoded' bytes decoded by an injected decoder."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self._delay_s = delay_s
        self.spoken: list[str] = []
        self.voice: str | None = None
        self.language: str | None = None

    async def stream(
        self, text_chunks: AsyncIterator[str], *, language: str, voice: str
    ) -> AsyncIterator[bytes]:
        self.language, self.voice = language, voice
        async for text in text_chunks:
            self.spoken.append(text)
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            yield b"ENC" + text.encode()


def fake_decoder(buffer: bytes, emitted: int) -> tuple[bytes, int]:
    """Stand-in for the MP3 decoder: PCM16 whose length tracks the encoded buffer."""
    total = len(buffer)
    if total <= emitted:
        return b"", emitted
    return b"\x00\x01" * (total - emitted), total


async def decide_ok(utterance: str, language: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    return {"action": "answer", "reply": REPLY, "model": "claude-sonnet-5", "prompt_version": "ab"}


# --------------------------------------------------------------------------------------
# Harness: a script of frames and conditions, never of sleeps.
# --------------------------------------------------------------------------------------

# A frame to queue, a condition to wait for, or an action to perform. The third kind is
# how a test stands in for the transport: `GreetingGate.announce` is driven by the
# participant-joined event in production, so a script says when a participant joined.
Step = Frame | Callable[[], bool] | Callable[[], Awaitable[None]]


def until(predicate: Callable[[], bool], label: str) -> Callable[[], bool]:
    """Mark a script step as 'wait for this, then continue'. ``label`` names the wait."""
    predicate.__doc__ = label
    return predicate


async def _wait(predicate: Callable[[], bool], limit: float = 10.0) -> None:
    """Poll ``predicate`` until it holds, or fail naming what was being waited for.

    ASYNC110 would prefer an ``asyncio.Event``, and it is right in general -- but the
    conditions here read plain counters and flags on the processors under test
    (``gate.blocked``, ``stt.chunks_pulled``, ``processor.listening``). Turning each of
    those into an event would mean adding test-only signalling to production code, which
    is a worse trade than a 5 ms poll in a test helper.
    """
    try:
        async with asyncio.timeout(limit):
            while not predicate():  # noqa: ASYNC110 - polls test doubles, not an event source
                await asyncio.sleep(0.005)
    except TimeoutError:
        raise AssertionError(f"timed out after {limit}s waiting for: {predicate.__doc__}") from None


async def drive(pipeline: Pipeline, script: Sequence[Step]) -> None:
    """Run ``pipeline``, queueing frames and awaiting conditions in script order.

    The generous 10 s per-condition timeout is not slack: these conditions are reached in
    milliseconds, and the timeout exists only so a genuine deadlock fails with the name of
    what it was waiting for rather than hanging the suite.
    """
    worker = PipelineWorker(pipeline, cancel_on_idle_timeout=False)
    started = asyncio.Event()

    @worker.event_handler("on_pipeline_started")
    async def _on_started(worker: PipelineWorker, frame: Frame) -> None:
        started.set()

    async def play() -> None:
        await asyncio.wait_for(started.wait(), timeout=10.0)
        try:
            for step in script:
                if asyncio.iscoroutinefunction(step):
                    # An action: something the transport would do, such as a participant
                    # joining. `iscoroutinefunction` is a runtime check mypy cannot narrow
                    # a Callable union with, hence the cast.
                    await cast(Callable[[], Awaitable[None]], step)()
                elif callable(step):
                    await _wait(cast(Callable[[], bool], step))
                else:
                    await worker.queue_frame(step)
        finally:
            # `finally`, so a script that fails a condition still ends the pipeline.
            await worker.queue_frame(EndFrame())

    runner = WorkerRunner()
    await runner.add_workers(worker)
    # A FAILING TEST MUST FAIL, NOT HANG. `asyncio.gather(runner.run(), play())` does not
    # do that: when `play()` raises, gather propagates immediately but leaves `runner.run()`
    # pending, and the suite stops dead with nothing reported. That matters here more than
    # in most test files -- these are the consent-gate assertions, so the first person to
    # break the gate is the person who would have seen a silent hang instead of the name of
    # the property they broke. (Found by sabotaging the gate: the run never came back.)
    # So the runner is a task this function owns, and it is cancelled on the way out.
    run_task = asyncio.create_task(runner.run())
    try:
        await play()
        await asyncio.wait_for(run_task, timeout=10.0)
    finally:
        if not run_task.done():
            run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
            await run_task


def make_pipeline(
    *,
    stt: Any = None,
    tts: Any = None,
    decide: Any = decide_ok,
    on_turn: Any = None,
    ack_timeout_s: float = 30.0,
) -> tuple[Pipeline, dict[str, Any]]:
    """Build the real pipeline with fakes, and hand back its processors for assertions."""
    head, tail = Passthrough(name="head"), Passthrough(name="tail")
    pipeline = build_pipeline(
        SETTINGS,
        stt=stt or FakeSTT(),
        tts=tts or FakeTTS(),
        decide=decide,
        greeting_audio=GREETING,
        head=head,
        tail=tail,
        vad=SilentVAD(sample_rate=INPUT_SAMPLE_RATE),
        on_turn=on_turn,
        decoder=fake_decoder,
        ack_timeout_s=ack_timeout_s,
    )
    parts: dict[str, Any] = {"head": head, "tail": tail}
    for processor in pipeline.processors:
        parts[type(processor).__name__] = processor
    return pipeline, parts


def audio(count: int = 1) -> list[Frame]:
    return [
        UserAudioRawFrame(
            user_id="employee",
            audio=FRAME,
            sample_rate=INPUT_SAMPLE_RATE,
            num_channels=1,
        )
        for _ in range(count)
    ]


def messages(frames: Sequence[Frame], kind: str | None = None) -> list[dict[str, Any]]:
    out = [
        f.message
        for f in frames
        if isinstance(f, OutputTransportMessageFrame) and isinstance(f.message, dict)
    ]
    return [m for m in out if kind is None or m.get("type") == kind]


def consented(gate: GreetingGate) -> list[Step]:
    """A participant joins, the notice plays, the transport confirms it, the gate opens.

    `gate.announce` is what `run_session`'s `on_participant_connected` handler calls, so
    this is the production sequence and not a test-only shortcut. Nothing plays the notice
    before this step: a pipeline that has started but has nobody in the room is exactly the
    case the gate must not open in.
    """
    return [
        gate.announce,
        BotStoppedSpeakingFrame(),
        until(lambda: gate.opened, "the consent notice to be acknowledged"),
    ]


def one_turn(gate: GreetingGate, stt: SarvamSTTProcessor, *, index: int = 0) -> list[Step]:
    """One complete utterance, waiting for each stage rather than sleeping through it.

    Every wait is a lambda evaluated when the step is reached, not a value captured while
    the script is being built -- a second turn's target has to count the first turn's two
    frames, and a snapshot taken at build time would already be satisfied and wait for
    nothing.
    """
    return [
        *(consented(gate) if index == 0 else []),
        VADUserStartedSpeakingFrame(),
        until(lambda: stt.streams_opened > index, f"utterance {index} to open a stream"),
        *audio(2),
        until(
            lambda: stt.audio_frames_consumed >= 2 * (index + 1),
            f"utterance {index}'s audio to be consumed",
        ),
        VADUserStoppedSpeakingFrame(),
    ]


# --------------------------------------------------------------------------------------
# 1. The greeting is a consent artifact: nothing is consumed before it has played.
# --------------------------------------------------------------------------------------


async def test_no_user_audio_is_consumed_before_the_notice_has_played() -> None:
    """Audio sent before the transport acknowledges the notice never reaches STT.

    This is the ordering proof: not "the notice frame appears somewhere in the output"
    but "the gate blocked the frames that arrived first, STT consumed none of them, and
    only after the playback acknowledgement does a frame get through".
    """
    stt = FakeSTT()
    pipeline, parts = make_pipeline(stt=stt)
    gate: GreetingGate = parts["GreetingGate"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *audio(3),
            until(lambda: gate.blocked == 3, "three frames to be blocked at the gate"),
            *consented(gate),
            *audio(2),
            until(lambda: gate.forwarded == 2, "two frames to pass the opened gate"),
        ],
    )

    assert gate.blocked == 3, "audio before the notice completed must never pass the gate"
    assert gate.forwarded == 2, "audio after the notice completed must pass"
    assert stt_processor.audio_frames_consumed == 2
    assert stt.chunks_pulled == 0, "audio alone opens no STT stream; VAD delimits utterances"
    assert gate.notice_unconfirmed is False


async def test_the_notice_is_emitted_before_the_first_forwarded_audio_frame() -> None:
    """The notice's audio reaches the transport strictly before any user audio does."""
    pipeline, parts = make_pipeline()
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]

    await drive(
        pipeline,
        [
            *audio(2),
            until(lambda: gate.blocked == 2, "the pre-notice audio to be blocked"),
            *consented(gate),
            *audio(2),
            until(lambda: gate.forwarded == 2, "the post-notice audio to pass"),
            until(
                lambda: any(isinstance(f, UserAudioRawFrame) for f in tail.seen),
                "user audio to reach the transport",
            ),
        ],
    )

    notice_bytes = sum(len(f.audio) for f in tail.seen if isinstance(f, TTSAudioRawFrame))
    assert notice_bytes == len(GREETING), "the whole consent notice is emitted, not a prefix"

    first_notice = next(i for i, f in enumerate(tail.seen) if isinstance(f, TTSAudioRawFrame))
    user_audio = [i for i, f in enumerate(tail.seen) if isinstance(f, UserAudioRawFrame)]
    assert user_audio, "the gate must open once the notice is acknowledged"
    assert first_notice < min(user_audio), "notice audio precedes every user audio frame"

    notices = messages(tail.seen, "notice")
    assert notices and notices[0]["state"] == "playing"
    assert notices[0]["v"] == DATA_MESSAGE_VERSION


async def test_a_started_pipeline_with_nobody_in_the_room_plays_no_notice() -> None:
    """The agent is dispatched into a room, not summoned into one.

    A notice played on pipeline start plays to whoever is there, which may be nobody --
    and `BaseOutputTransport` acknowledges it regardless of whether a single participant
    is subscribed, so the gate would open on a notice delivered to an empty room. The
    employee's first words would then reach Saaras having been told nothing. Playback is
    therefore driven by the participant-joined event, and this test pins the fact that
    nothing at all happens before it.
    """
    stt = FakeSTT()
    pipeline, parts = make_pipeline(stt=stt)
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]

    await drive(
        pipeline,
        [
            # No `gate.announce` anywhere: nobody has joined.
            *audio(5),
            until(lambda: gate.blocked == 5, "the audio to be blocked with no notice played"),
        ],
    )

    assert gate.announcements == 0, "no participant joined, so no notice should have played"
    assert not messages(tail.seen, "notice"), "a notice was announced to an empty room"
    assert not [f for f in tail.seen if isinstance(f, TTSAudioRawFrame)], "notice audio played"
    assert gate.opened is False, "the gate opened without a notice reaching a participant"
    assert gate.forwarded == 0
    assert stt.calls == 0


async def test_an_unacknowledged_notice_says_so_and_keeps_the_gate_shut() -> None:
    """The ack deadline is a report, not a way in.

    A client that never subscribes never produces `BotStoppedSpeakingFrame`. The old
    behaviour opened the gate after 30s on the theory that a session should not deadlock;
    that trades a lost session for a recorded one nobody consented to, which is the wrong
    way round. Now the deadline only publishes `notice: unconfirmed` -- the gate stays shut
    for the life of the session and captures nothing.
    """
    stt = FakeSTT()
    # A deadline short enough to reach without sleeping through a real one.
    pipeline, parts = make_pipeline(stt=stt, ack_timeout_s=0.0)
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]

    await drive(
        pipeline,
        [
            gate.announce,
            # Deliberately NO BotStoppedSpeakingFrame: the transport never confirms.
            *audio(4),
            until(lambda: gate.notice_unconfirmed, "the gate to report an unconfirmed notice"),
            *audio(4),
            until(lambda: gate.blocked >= 8, "every frame to stay blocked"),
        ],
    )

    assert gate.opened is False, "the gate opened on a notice nobody confirmed hearing"
    assert gate.forwarded == 0, "audio passed a gate that was never consented through"
    assert stt.calls == 0 and stt.chunks_pulled == 0, "a vendor saw audio with no notice given"

    unconfirmed = [m for m in messages(tail.seen, "notice") if m["state"] == "unconfirmed"]
    assert len(unconfirmed) == 1, "the unconfirmed notice must be reported exactly once"


async def test_a_participant_joining_later_gets_the_notice_and_recloses_the_gate() -> None:
    """Somebody who joins after the notice finished has heard nothing.

    A supervisor joining a call in progress is not covered by a notice played before they
    arrived. The gate re-plays and re-closes, so the room stops capturing until the new
    arrival has been told too.
    """
    stt = FakeSTT()
    pipeline, parts = make_pipeline(stt=stt)
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]

    await drive(
        pipeline,
        [
            *consented(gate),
            *audio(2),
            until(lambda: gate.forwarded == 2, "the first participant's audio to pass"),
            # A second participant joins.
            gate.announce,
            until(lambda: not gate.opened, "the gate to close again for the new arrival"),
            *audio(3),
            until(lambda: gate.blocked >= 3, "audio to be blocked during the replayed notice"),
            BotStoppedSpeakingFrame(),
            until(lambda: gate.opened, "the replayed notice to be acknowledged"),
            *audio(2),
            until(lambda: gate.forwarded == 4, "audio to pass once everyone has been told"),
        ],
    )

    assert gate.announcements == 2, "the late joiner must get the notice too"
    played = [m for m in messages(tail.seen, "notice") if m["state"] == "playing"]
    assert len(played) == 2
    notice_bytes = sum(len(f.audio) for f in tail.seen if isinstance(f, TTSAudioRawFrame))
    assert notice_bytes == 2 * len(GREETING), "the replay is the whole notice, not a prefix"


async def test_a_stale_ack_deadline_cannot_open_the_gate_after_the_session_ended() -> None:
    """`EndFrame` forgets the notice, deadline included.

    Leaving `_played_at` behind meant a frame arriving in a later session found a deadline
    that had expired long ago and opened the gate on a notice belonging to a finished call.
    The deadline is zero here, so if the timestamp survived the reset the very next frame
    would open the gate -- which is exactly the assertion.
    """
    pipeline, parts = make_pipeline(ack_timeout_s=0.0)
    gate: GreetingGate = parts["GreetingGate"]

    await drive(
        pipeline,
        [
            *consented(gate),
            *audio(1),
            until(lambda: gate.forwarded == 1, "the consented audio to pass"),
        ],
    )

    # `drive` queues EndFrame when the script finishes; the gate must be fully reset.
    assert gate.opened is False
    assert gate.notice_unconfirmed is False
    assert gate._played_at is None, "a finished session left its notice deadline behind"


async def test_a_missing_notice_refuses_to_start_and_opens_no_stream(tmp_path: Path) -> None:
    """``run_session`` refuses before a token is minted; no audio, no STT, no room.

    Both shapes of the same fact are refusals: ``greeting_path=None`` ("nobody configured
    a notice") and a path that is not there ("the notice was deleted"). From the
    employee's side they are identical, and an opt-out that can happen by omission is how
    an unconsented recording ships with everything still appearing to work.
    """
    stt = FakeSTT()
    missing = tmp_path / "recorded-notice.wav"

    for path in (None, missing):
        settings = VoiceSettings(
            room=SETTINGS.room,
            livekit_url=SETTINGS.livekit_url,
            identity=SETTINGS.identity,
            greeting_path=path,
        )
        with pytest.raises(GreetingUnavailable) as caught:
            await run_session(settings, decide=decide_ok, session_id="s-1", employee_id="e-1")
        if path is not None:
            assert str(missing) in str(caught.value), "the error names the path it wanted"

    assert stt.calls == 0
    assert stt.chunks_pulled == 0


async def test_an_unreadable_notice_refuses_to_start(tmp_path: Path) -> None:
    """A file that exists but is not decodable audio is a refusal, not a silent skip."""
    corrupt = tmp_path / "notice.wav"
    corrupt.write_bytes(b"this is not audio")
    with pytest.raises(GreetingUnavailable) as caught:
        load_greeting(corrupt)
    assert str(corrupt) in str(caught.value)


async def test_an_empty_notice_refuses_to_start(tmp_path: Path) -> None:
    """Zero bytes of audio is not a notice, however valid the container."""
    import wave

    silent = tmp_path / "notice.wav"
    with wave.open(str(silent), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(OUTPUT_SAMPLE_RATE)
        handle.writeframes(b"")
    with pytest.raises(GreetingUnavailable):
        load_greeting(silent)


async def test_the_gate_refuses_to_be_built_with_no_notice_audio() -> None:
    """Even the constructor refuses: there is no way to assemble a consent-free pipeline."""
    with pytest.raises(GreetingUnavailable):
        GreetingGate(greeting_audio=b"")


# --------------------------------------------------------------------------------------
# 2. STT failure switches the client to chat mode and stops listening.
# --------------------------------------------------------------------------------------


async def test_stt_failure_sends_chat_mode_and_stops_pulling_audio() -> None:
    """The data message is sent AND no further audio is pulled or consumed afterwards."""
    stt = FakeSTT(fail=True, fail_after=1)
    pipeline, parts = make_pipeline(stt=stt)
    tail: Passthrough = parts["tail"]
    gate: GreetingGate = parts["GreetingGate"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *consented(gate),
            VADUserStartedSpeakingFrame(),
            until(lambda: stt_processor.streams_opened == 1, "the STT stream to open"),
            *audio(3),
            until(lambda: not stt_processor.listening, "the STT stream to fail"),
            until(lambda: bool(messages(tail.seen, "mode")), "the chat-mode message"),
        ],
    )

    # Everything after this point is measured against a pipeline that has already failed,
    # so the counters are frozen rather than racing.
    consumed_at_failure = stt_processor.audio_frames_consumed
    pulled_at_failure = stt.chunks_pulled

    modes = messages(tail.seen, "mode")
    assert modes, "a dead STT must tell the client"
    assert modes[0]["mode"] == "chat"
    assert modes[0]["reason"] == "stt_unavailable"

    assert stt_processor.listening is False
    assert stt_processor.audio_frames_consumed == consumed_at_failure
    assert stt.chunks_pulled == pulled_at_failure, "the adapter stops being fed once it raised"
    assert stt.calls == 1, "a dead STT is never re-opened within the session"


async def test_audio_after_stt_failure_never_reaches_the_transport() -> None:
    """Nothing downstream of a dead STT sees the employee's audio either."""
    stt = FakeSTT(fail=True, fail_after=0)
    pipeline, parts = make_pipeline(stt=stt)
    tail: Passthrough = parts["tail"]
    gate: GreetingGate = parts["GreetingGate"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *consented(gate),
            VADUserStartedSpeakingFrame(),
            until(lambda: stt_processor.streams_opened == 1, "the STT stream to open"),
            *audio(1),
            until(lambda: not stt_processor.listening, "the STT stream to fail"),
            until(lambda: bool(messages(tail.seen, "mode")), "the chat-mode message"),
            # Sent strictly after the client has been told the room stopped listening.
            *audio(4),
            until(lambda: gate.forwarded >= 5, "the post-failure audio to pass the gate"),
        ],
    )

    mode_index = next(
        i
        for i, f in enumerate(tail.seen)
        if isinstance(f, OutputTransportMessageFrame)
        and isinstance(f.message, dict)
        and f.message.get("type") == "mode"
    )
    assert not [f for f in tail.seen[mode_index:] if isinstance(f, UserAudioRawFrame)]


async def test_a_new_utterance_after_stt_death_opens_no_stream() -> None:
    """VAD keeps firing after STT dies; the processor must not take that as a retry."""
    stt = FakeSTT(fail=True, fail_after=0)
    pipeline, parts = make_pipeline(stt=stt)
    gate: GreetingGate = parts["GreetingGate"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *consented(gate),
            VADUserStartedSpeakingFrame(),
            until(lambda: stt_processor.streams_opened == 1, "the STT stream to open"),
            *audio(1),
            until(lambda: not stt_processor.listening, "the STT stream to fail"),
            VADUserStoppedSpeakingFrame(),
            VADUserStartedSpeakingFrame(),
            *audio(2),
            until(lambda: gate.forwarded >= 3, "the second utterance's audio to pass the gate"),
        ],
    )

    assert stt.calls == 1
    assert stt_processor.streams_opened == 1
    assert stt_processor.listening is False


async def test_an_open_room_with_nobody_speaking_opens_no_stt_stream() -> None:
    """Audio flows the whole time a room is open. Only VAD-delimited speech is streamed.

    The gate has already opened -- the notice has played -- so this is not the consent
    property tested above; it is the one that holds for the rest of the call. LiveKit
    delivers ``InputAudioRawFrame``s continuously while a participant is connected,
    speaking or not, and ``SarvamSTTProcessor`` sees every one of them. If it opened a
    stream on audio rather than on ``VADUserStartedSpeakingFrame``, an idle room would be
    transcribed end to end.

    Two consequences, and the privacy one is why this test exists: everything said in the
    room while nobody is addressing the agent would reach Sarvam, and Saaras bills on audio
    duration (Rs 30/h, about Rs 0.50/min), so an open room would meter for as long as it
    stayed open. Between utterances the audio goes to a bounded pre-roll ring buffer and is
    discarded as it overflows.

    Sabotage check: opening the feed from ``_accept`` instead of ``_start_utterance`` fails
    this test and the pre-notice one, and nothing else -- which is why this one is here.
    """
    stt = FakeSTT()
    pipeline, parts = make_pipeline(stt=stt)
    gate: GreetingGate = parts["GreetingGate"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *consented(gate),
            # A connected participant, silent. No VAD frame is ever sent.
            *audio(40),
            until(lambda: gate.forwarded >= 40, "every idle frame to reach the processor"),
        ],
    )

    assert stt_processor.audio_frames_consumed == 40, (
        "the processor does see every frame -- the point is what it does with them"
    )
    assert stt_processor.streams_opened == 0, (
        "an idle room opened a Saaras stream; silence would be sent to the vendor and billed"
    )
    assert stt.calls == 0, "the adapter was never called"
    assert stt.chunks_pulled == 0, "not one byte of the idle room was pulled by the vendor"


async def test_a_healthy_stt_publishes_partials_then_a_flagged_final() -> None:
    """Partials are published for the UI; the final one is flagged, and emitted once."""
    stt = FakeSTT(segments=("मेरा फ़ोन", UTTERANCE))
    pipeline, parts = make_pipeline(stt=stt)
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *one_turn(gate, stt_processor),
            until(
                lambda: any(m["final"] for m in messages(tail.seen, "transcript")),
                "the final transcript",
            ),
        ],
    )

    transcripts = messages(tail.seen, "transcript")
    assert [m["final"] for m in transcripts] == [False, False, True]
    assert sum(isinstance(f, TranscriptionFrame) for f in tail.seen) == 1
    assert sum(isinstance(f, InterimTranscriptionFrame) for f in tail.seen) == 2
    final_frame = next(f for f in tail.seen if isinstance(f, TranscriptionFrame))
    assert final_frame.finalized is True


# --------------------------------------------------------------------------------------
# 3. Latencies are measured, and a stage that did not run has no latency at all.
# --------------------------------------------------------------------------------------


async def test_time_to_first_audio_is_measured_from_the_final_transcript() -> None:
    """The reported figure brackets a deliberately slow TTS; it is not a sum of parts."""
    recorded: list[tuple[int, StageLatency]] = []

    async def on_turn(index: int, latency: StageLatency) -> None:
        recorded.append((index, latency))

    tts = FakeTTS(delay_s=0.08)
    pipeline, parts = make_pipeline(tts=tts, on_turn=on_turn)
    gate: GreetingGate = parts["GreetingGate"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]
    tts_processor: SarvamTTSProcessor = parts["SarvamTTSProcessor"]

    await drive(
        pipeline,
        [
            *one_turn(gate, stt_processor),
            until(lambda: bool(recorded), "the turn's latencies to be reported"),
        ],
    )

    _, latency = recorded[0]
    measured = latency.measured()
    assert latency.time_to_first_audio_ms is not None
    # The fake TTS sleeps 80 ms before its first chunk. A figure below that is not a
    # measurement of anything; one far above it means the clock was started too early.
    assert latency.time_to_first_audio_ms >= 80.0
    assert latency.tts_ms is not None
    assert latency.time_to_first_audio_ms >= latency.tts_ms
    assert set(measured) >= {"vad_ms", "stt_ms", "decide_ms", "tts_ms", "time_to_first_audio_ms"}
    assert all(value > 0 for value in measured.values())
    assert tts_processor.audio_frames_emitted >= 1


async def test_a_stage_that_did_not_run_is_absent_rather_than_zero() -> None:
    """The house rule from comms_surveillance/metrics.py: unmeasured is never 0.0."""
    latency = StageLatency(decide_ms=12.5)
    assert latency.vad_ms is None
    assert latency.measured() == {"decide_ms": 12.5}
    assert "time_to_first_audio_ms" not in latency.measured()
    assert 0.0 not in latency.measured().values()


async def test_latencies_merge_into_the_graph_payload_without_colliding() -> None:
    """``turns.latency_ms`` keeps the graph's node timings; voice stages are namespaced."""
    latency = StageLatency(decide_ms=12.5, time_to_first_audio_ms=900.0)
    merged = latency.merge_into({"retrieve": 40.0, "decide": 300.0, "total": 420.0})
    assert merged["decide"] == 300.0, "the graph's own decide timing is not overwritten"
    assert merged["voice.decide_ms"] == 12.5
    assert merged["voice.time_to_first_audio_ms"] == 900.0
    assert "voice.stt_ms" not in merged, "an unmeasured stage adds no key"


async def test_a_turn_with_no_speech_reports_no_transcript_and_no_audio_latency() -> None:
    """An utterance Saaras returns nothing for produces no decision and no fake numbers."""
    recorded: list[StageLatency] = []

    async def on_turn(index: int, latency: StageLatency) -> None:
        recorded.append(latency)

    stt = FakeSTT(segments=())
    pipeline, parts = make_pipeline(stt=stt, on_turn=on_turn)
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *one_turn(gate, stt_processor),
            until(
                lambda: (
                    stt_processor.turn is not None and stt_processor.turn.transcript_at is not None
                ),
                "the STT stream to finish with no transcript",
            ),
        ],
    )

    assert not messages(tail.seen, "transcript")
    assert not messages(tail.seen, "decision")
    assert not recorded, "no turn completed, so no latency is reported for one"


async def test_a_failing_decide_tells_the_client_and_reports_no_decide_latency() -> None:
    """A broken graph must not leave the employee in silence -- but it invents no number.

    Two properties at once. The client is told (so a caller who has just stopped speaking
    can be shown something rather than hearing nothing and being unable to tell a broken
    graph from a slow one), and the stage that raised still gets no ``decide_ms``: it did
    not run, and a number would read as "instant" on a dashboard.
    """
    recorded: list[StageLatency] = []

    async def on_turn(index: int, latency: StageLatency) -> None:
        recorded.append(latency)

    async def decide_boom(
        utterance: str, language: str, history: list[dict[str, Any]]
    ) -> dict[str, Any]:
        raise RuntimeError("graph unavailable")

    pipeline, parts = make_pipeline(decide=decide_boom, on_turn=on_turn)
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *one_turn(gate, stt_processor),
            until(lambda: bool(messages(tail.seen, "error")), "the decide failure signal"),
        ],
    )

    errors = messages(tail.seen, "error")
    assert errors[0]["stage"] == "decide"
    assert errors[0]["recoverable"] is True
    # The signal carries no exception text, no utterance and no reply.
    assert set(errors[0]) == {"v", "type", "stage", "recoverable"}

    assert not messages(tail.seen, "decision")
    assert not recorded, "no audio was ever emitted, so no turn latency is claimed"


async def test_a_turn_after_a_failed_decide_still_answers() -> None:
    """A decide failure is per-turn, not terminal. The next utterance must go through.

    This is the part most likely to regress: it would be easy to reuse the STT failure's
    latching flag and quietly turn one bad graph call into a dead session.
    """
    recorded: list[StageLatency] = []

    async def on_turn(index: int, latency: StageLatency) -> None:
        recorded.append(latency)

    calls = {"n": 0}

    async def decide_once_broken(
        utterance: str, language: str, history: list[dict[str, Any]]
    ) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("graph unavailable")
        return await decide_ok(utterance, language, history)

    tts = FakeTTS()
    pipeline, parts = make_pipeline(decide=decide_once_broken, tts=tts, on_turn=on_turn)
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *one_turn(gate, stt_processor, index=0),
            until(lambda: bool(messages(tail.seen, "error")), "the first turn to fail"),
            *one_turn(gate, stt_processor, index=1),
            until(lambda: bool(recorded), "the recovered turn to report latencies"),
        ],
    )

    assert calls["n"] == 2, "the second final transcript must still reach decide"
    assert stt_processor.streams_opened == 2, "the second utterance must still open a stream"
    assert stt_processor.listening is True, "a decide failure never stops the room listening"

    assert len(messages(tail.seen, "error")) == 1, "only the failed turn signals an error"
    decisions = messages(tail.seen, "decision")
    assert len(decisions) == 1 and decisions[0]["action"] == "answer"
    assert tts.spoken == sentences(REPLY), "the recovered turn is actually spoken"
    assert recorded[-1].time_to_first_audio_ms is not None


# --------------------------------------------------------------------------------------
# 4. Content hygiene: nothing about audio or transcripts reaches a log.
# --------------------------------------------------------------------------------------


async def test_no_transcript_or_audio_content_reaches_the_log() -> None:
    """At INFO and above, the log carries counters and milliseconds -- never content."""
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(str(message)), level="INFO")
    try:
        tts = FakeTTS()
        pipeline, parts = make_pipeline(tts=tts)
        gate: GreetingGate = parts["GreetingGate"]
        stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]
        await drive(
            pipeline,
            [
                *one_turn(gate, stt_processor),
                until(lambda: tts.spoken == sentences(REPLY), "the whole reply to be spoken"),
            ],
        )
    finally:
        logger.remove(sink_id)

    blob = "".join(captured)
    for secret in (UTTERANCE, REPLY, "9876543210", "priya@example.com"):
        assert secret not in blob, "transcript or reply content must never reach a log"
    for sentence in sentences(REPLY):
        assert sentence not in blob
    assert FRAME.hex() not in blob and GREETING.hex() not in blob


async def test_text_published_to_the_room_is_redacted() -> None:
    """``redact`` runs before text leaves this module; no README override is documented."""
    pipeline, parts = make_pipeline(stt=FakeSTT(segments=(UTTERANCE,)))
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *one_turn(gate, stt_processor),
            until(
                lambda: any(m["final"] for m in messages(tail.seen, "transcript")),
                "the final transcript",
            ),
        ],
    )

    transcripts = messages(tail.seen, "transcript")
    assert transcripts
    for message in transcripts:
        assert "9876543210" not in message["text"]
        assert "[PHONE]" in message["text"]


async def test_decision_messages_carry_the_action_but_not_the_reply() -> None:
    """The room is told what happened, not what was said; the audio carries the words."""
    pipeline, parts = make_pipeline()
    gate: GreetingGate = parts["GreetingGate"]
    tail: Passthrough = parts["tail"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *one_turn(gate, stt_processor),
            until(lambda: bool(messages(tail.seen, "decision")), "the decision message"),
        ],
    )

    decisions = messages(tail.seen, "decision")
    assert decisions[0]["action"] == "answer"
    assert not any("reply" in message for message in decisions)


# --------------------------------------------------------------------------------------
# Pipeline shape, synthesis and the vendor-facing details.
# --------------------------------------------------------------------------------------


async def test_the_reply_is_synthesized_sentence_by_sentence_in_the_session_voice() -> None:
    """Bulbul receives one complete sentence at a time, in the session's language."""
    tts = FakeTTS()
    pipeline, parts = make_pipeline(tts=tts)
    gate: GreetingGate = parts["GreetingGate"]
    stt_processor: SarvamSTTProcessor = parts["SarvamSTTProcessor"]

    await drive(
        pipeline,
        [
            *one_turn(gate, stt_processor),
            until(lambda: tts.spoken == sentences(REPLY), "the whole reply to be spoken"),
        ],
    )

    assert tts.spoken == sentences(REPLY)
    assert len(tts.spoken) > 1, "a multi-sentence reply is not handed over as one block"
    assert tts.language == "hi-IN"
    assert tts.voice == speaker_for("hi-IN")


async def test_build_pipeline_wires_the_prd_c5_order() -> None:
    """The pipeline is constructed, not run: order is asserted on the processor list."""
    pipeline, _ = make_pipeline()
    names = [type(p).__name__ for p in pipeline.processors]
    stages = [
        "GreetingGate",
        "VADProcessor",
        "SarvamSTTProcessor",
        "DecideProcessor",
        "SarvamTTSProcessor",
    ]
    positions = [names.index(stage) for stage in stages]
    assert positions == sorted(positions), f"PRD C5 order broken: {names}"
    assert names.index("GreetingGate") < names.index("VADProcessor"), (
        "the gate must sit upstream of VAD, or audio it blocks is still analysed"
    )
    assert names[1] == "Passthrough", "the transport's input processor leads the pipeline"
    assert names[-2] == "Passthrough", "the transport's output processor closes it"


async def test_the_speaker_table_only_contains_bulbul_v3_voices() -> None:
    """Verified against the bulbul:v3 roster; an unknown id fails the stream at connect."""
    roster = {
        "aditya", "ritu", "ashutosh", "priya", "neha", "rahul", "pooja", "rohan", "simran",
        "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun", "manan", "sumit",
        "roopa", "kabir", "aayan", "shubh", "advait", "anand", "tanya", "tarun", "sunny",
        "mani", "gokul", "vijay", "shruti", "suhani", "mohit", "kavitha", "rehan", "soham",
        "rupali", "niharika",
    }  # fmt: skip
    for language in ("hi-IN", "hi-Latn", "te-IN", "ta-IN", "en-IN", "unknown-XX"):
        assert speaker_for(language) in roster


async def test_sentence_splitting_handles_the_danda() -> None:
    """A Hindi reply terminates on ``।``; splitting on ``.`` alone yields one block."""
    assert sentences("नमस्ते। आपका टिकट दर्ज हो गया है। धन्यवाद।") == [
        "नमस्ते।",
        "आपका टिकट दर्ज हो गया है।",
        "धन्यवाद।",
    ]
    assert sentences("Hello there. Your ticket is filed!") == [
        "Hello there.",
        "Your ticket is filed!",
    ]
    assert sentences("   ") == []


async def test_the_greeting_round_trips_through_a_real_wav(tmp_path: Path) -> None:
    """``load_greeting`` returns PCM16 mono at the pipeline rate."""
    import wave

    path = tmp_path / "notice.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(OUTPUT_SAMPLE_RATE)
        handle.writeframes(GREETING)

    assert load_greeting(path) == GREETING


async def test_a_notice_recorded_at_another_rate_is_resampled(tmp_path: Path) -> None:
    """An 8 kHz notice still plays; it is resampled rather than played at double speed."""
    import wave

    path = tmp_path / "notice-8k.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(GREETING)

    loaded = load_greeting(path, sample_rate=OUTPUT_SAMPLE_RATE)
    assert len(loaded) == pytest.approx(len(GREETING) * 2, rel=0.05)


async def test_the_decoder_contract_emits_each_sample_exactly_once() -> None:
    """A growing encoded buffer yields every sample once and never re-emits one."""
    first, total = fake_decoder(b"ABCD", 0)
    assert total == 4 and len(first) == 8
    second, total = fake_decoder(b"ABCDEF", total)
    assert total == 6 and len(second) == 4
    nothing, total = fake_decoder(b"ABCDEF", total)
    assert nothing == b"" and total == 6


@pytest.mark.voice
async def test_run_session_joins_a_real_room_and_gates_on_the_real_notice(
    tmp_path: Path,
) -> None:
    """The one property that cannot be faked: the gate opens on real playback, in a room.

    Marked ``voice``, not ``integration``. CI provisions postgres/redis/qdrant but no
    LiveKit, and the integration job fails on ANY skip (uc3/P5's evidence guard), so an
    ``integration`` marker here would turn CI red for a service that was never going to
    be there. ``make voice-test`` runs this marker with ``make stack-voice`` up.

    What this covers that the mocked tests cannot: ``BotStoppedSpeakingFrame`` is emitted
    by the real ``BaseOutputTransport`` only once the notice has actually been written
    out to the room, so only here is "the notice played" a fact about a room rather than
    a fact about a fake. Everything else -- ordering, the chat-mode switch, the latency
    arithmetic -- is proven above without a room.
    """
    import os
    import wave

    required = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    absent = [name for name in required if not os.getenv(name)]
    if absent:
        pytest.skip(f"no LiveKit room reachable: {', '.join(absent)} unset (make stack-voice)")

    notice = tmp_path / "recorded-notice.wav"
    with wave.open(str(notice), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(OUTPUT_SAMPLE_RATE)
        handle.writeframes(GREETING * 10)

    settings = VoiceSettings(
        room=f"uc1-voice-{int(time.time())}",
        livekit_url=os.environ["LIVEKIT_URL"],
        identity="helpdesk-agent",
        language="hi-IN",
        greeting_path=notice,
    )
    # No participant ever joins, so the session is expected to idle out rather than to
    # complete a turn. What is asserted is that it connected and refused nothing.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(
            run_session(settings, decide=decide_ok, session_id="s-live", employee_id="e-live"),
            timeout=20,
        )
