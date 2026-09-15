"""UC1 voice pipeline: LiveKit room in, Saaras STT, the helpdesk graph, Bulbul TTS, room out.

Pipeline order (PRD C5)::

    transport.input()        LiveKit audio in (16 kHz PCM16 mono)
      -> GreetingGate        recorded consent notice; blocks user audio until it has played
      -> VADProcessor        Silero VAD, turn boundaries
      -> SarvamSTTProcessor  indic_platform.adapters.sarvam_stt.SarvamSTT.stream (Saaras, auto)
      -> DecideProcessor     graph.decide on the FINAL transcript, reply split into sentences
      -> SarvamTTSProcessor  indic_platform.adapters.sarvam_tts.SarvamTTS.stream (Bulbul)
      -> transport.output()  LiveKit audio out

Four properties this module exists to guarantee, in the order they bite:

**1. The greeting is a consent artifact, not decoration.** This is a recorded support
call; an employee has to be told it is recorded *before* a single sample of their
audio is consumed. So the notice is not "played at session start" as a courtesy --
:class:`GreetingGate` sits at the head of the pipeline and drops every
:class:`InputAudioRawFrame` until the output transport has reported the notice
finished playing out (``BotStoppedSpeakingFrame``, which is why the notice is emitted
as TTS frames: that is the only frame pair the transport turns into a playback-complete
signal -- see ``pipecat.transports.base_output``). Nothing downstream, VAD included,
ever sees the dropped audio.

Playback is triggered by a participant *joining*, not by the pipeline starting: the
agent is dispatched into the room rather than summoned into it, so a notice played on
``StartFrame`` can play to an empty room, be acknowledged by the transport anyway, and
leave the gate open before the employee ever arrives. Every join re-plays it and
re-closes the gate, so a late joiner is notified too. If the transport never confirms
playback, the gate stays SHUT and says so (``notice: unconfirmed``); it does not open
on a timeout.

A *missing* notice is therefore a refusal, not a degradation. ``run_session`` calls
:func:`load_greeting` before it mints a token or joins a room, and
:class:`GreetingUnavailable` names the path it wanted. ``greeting_path=None`` refuses
for the same reason: "no notice configured" and "notice deleted" are the same fact from
the employee's side, and an opt-out that can happen by *omission* is how an unconsented
recording ships without anyone noticing -- everything still appears to work. An explicit
opt-out, if the product ever wants one, belongs in a separately named setting so it has
to be typed on purpose.

**2. A dead STT never leaves a live-looking room.** When the Saaras stream raises,
:class:`SarvamSTTProcessor` sends the client a ``mode: chat`` data message *and* stops
listening: the current audio feed is closed, no new stream is opened, and subsequent
``InputAudioRawFrame``s are dropped rather than forwarded. A room that keeps its mic
open after transcription has died is worse than a room that says so.

**3. Latencies are measured, never estimated.** :class:`StageLatency` fields are
``float | None`` and a stage that did not run stays ``None``; :meth:`StageLatency.measured`
omits it entirely. A zero that means "did not happen" is the defect
``comms_surveillance/metrics.py`` documents at length, and the same rule applies here.
``time_to_first_audio_ms`` is measured from the FINAL transcript to the first TTS audio
frame this pipeline actually pushed downstream -- not from a guess about when the user
stopped speaking, and not from when TTS was asked to start.

**4. No content in logs or spans.** Nothing here logs a transcript, a reply or audio
bytes; the log lines carry turn indices, byte counts, booleans and milliseconds.
``redact`` runs over every piece of text that leaves this module -- including the
transcript captions published to the room -- because ``CLAUDE.md`` allows unredacted
text only where an app's README documents the override, and this app's README does not.
Vendor-side redaction is the adapters' own (``SarvamTTS`` redacts before synthesis).
Pipecat logs frame contents at DEBUG/TRACE, so a deployment keeps its loguru level at
INFO or above; ``platform/tests/test_uc1_voice_pipeline.py`` asserts the INFO-and-above
surface is content-free.

Codec note, verified live against Sarvam on 2026-09-15: ``SarvamTTS.stream`` yields
**MP3** (MPEG Layer III, 16 kHz mono -- first chunk began ``ff f3 c8 c4``), while LiveKit
needs PCM16. :func:`decode_mp3_stream` decodes the growing MP3 buffer with ``soundfile``
and emits only the samples that are new, which was confirmed to decode a truncated
prefix correctly (chunk 1 alone -> 7488 samples; chunks 1-2 -> 13248; all 8 -> 43200).
``soundfile`` is a hard dependency of ``pipecat-ai`` 1.10.0, so it is present wherever
this module is, but it is imported lazily and the decoder is injectable.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from indic_platform.adapters.base import STT, TTS, TranscriptSegment
from indic_platform.security.redact import redact
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADAnalyzer
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    OutputTransportMessageFrame,
    TextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import BaseTransport

# Saaras streaming wants 16 kHz mono PCM16 (see `sarvam_stt.SarvamSTT.stream`), and the
# Bulbul stream the adapter configures comes back at 16 kHz too, so both ends of the
# room run at one rate and nothing in this pipeline resamples.
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 16000

# Bulbul v3 speaker per language. Every id here is on the bulbul:v3 roster (verified via
# the `sarvam` MCP server, 2026-09-15); an id that is not on it fails the stream with
# "Sarvam TTS stream rejected" at connect time, which is why this is a closed table with
# a known-good default rather than a formatted string.
SPEAKERS: Mapping[str, str] = {
    "hi-IN": "priya",
    "hi-Latn": "priya",
    "te-IN": "kavya",
    "ta-IN": "shreya",
    "en-IN": "neha",
}
DEFAULT_SPEAKER = "priya"

# Schema version for everything this pipeline publishes to the room, so a UI can refuse
# a payload shape it does not know instead of silently rendering half of it.
DATA_MESSAGE_VERSION = 1

# How much audio to keep before VAD confirms speech. Silero needs `start_secs` of voice
# before it says "started", and that audio is part of the utterance: without a pre-roll
# the first syllable is cut off and the transcript quietly loses the imperative verb.
PREROLL_MS = 400

# How long the gate waits for the transport to confirm the notice finished playing before
# it says so. This is a REPORTING deadline, not a way in: on expiry the gate publishes
# `notice: unconfirmed` and stays shut, because "we waited 30 seconds" is not evidence
# that anyone was told the call is recorded. A control that opens on the absence of a
# signal is not a control, and the cost of failing closed is one lost session.
GREETING_ACK_TIMEOUT_S = 30.0

_SENTENCE_END = re.compile("(?<=[\u0964\u0965.!?\u2047\u2048\u2049])[\\s\u200b]+")


class GreetingUnavailable(RuntimeError):
    """The recorded consent notice could not be loaded, so no session may start."""


DecideCallable = Callable[[str, str, list[dict[str, Any]]], Awaitable[dict[str, Any]]]
TurnCallback = Callable[[int, "StageLatency"], Awaitable[None]]
Decoder = Callable[[bytes, int], tuple[bytes, int]]


@dataclass(frozen=True)
class VoiceSettings:
    """Everything a voice session needs that is not a collaborator.

    Attributes:
        room: LiveKit room name.
        livekit_url: ``LIVEKIT_URL`` (ws:// or wss://).
        identity: the agent's participant identity, as minted into its access token.
        language: session language; selects the Bulbul speaker. STT stays on ``auto``
            so a employee who switches script mid-call is still transcribed.
        greeting_path: the recorded consent notice. ``None`` is a refusal, not an
            opt-out -- see the module docstring.
    """

    room: str
    livekit_url: str
    identity: str
    language: str = "hi-IN"
    greeting_path: Path | None = None


@dataclass
class StageLatency:
    """Milliseconds per stage of one voice turn, merged into ``turns.latency_ms``.

    Every field is ``float | None`` and stays ``None`` when its stage did not run. A
    stage that did not happen has no latency -- not ``0.0``, which reads as "instant"
    on a dashboard and is indistinguishable from a real measurement. ``comms_surveillance``
    shipped that defect once (``evidence_failure_rate: 0.0`` over zero checks) and
    ``CLAUDE.md`` is explicit: missing data is "unmeasured", never a passing placeholder.

    Attributes:
        vad_ms: from the last user audio frame consumed to VAD confirming end of speech,
            i.e. the endpointing delay the employee actually waits through.
        stt_ms: from end of speech to the FINAL transcript being ready.
        decide_ms: from the final transcript to ``decide`` returning.
        tts_ms: from the first reply sentence being handed to Bulbul to the first audio
            chunk coming back.
        time_to_first_audio_ms: from the FINAL transcript to the first TTS audio frame
            this pipeline actually pushed downstream. This is the number the P5 gate
            measures; it is never derived from the others.
    """

    vad_ms: float | None = None
    stt_ms: float | None = None
    decide_ms: float | None = None
    tts_ms: float | None = None
    time_to_first_audio_ms: float | None = None

    def measured(self) -> dict[str, float]:
        """Only the stages that actually ran. Absent means unmeasured, never zero."""
        return {
            name: value
            for name, value in (
                ("vad_ms", self.vad_ms),
                ("stt_ms", self.stt_ms),
                ("decide_ms", self.decide_ms),
                ("tts_ms", self.tts_ms),
                ("time_to_first_audio_ms", self.time_to_first_audio_ms),
            )
            if value is not None
        }

    def merge_into(self, latency_ms: Mapping[str, float] | None) -> dict[str, float]:
        """Merge the measured stages onto the graph's own ``turns.latency_ms`` payload.

        The voice stages are namespaced under ``voice.`` so they cannot collide with the
        graph's node timings (``retrieve``, ``decide``, ``total``, ...), which measure a
        different thing: the graph's ``decide`` is one component of this turn's
        ``decide_ms``, not the same number.
        """
        merged: dict[str, float] = dict(latency_ms or {})
        merged.update({f"voice.{name}": value for name, value in self.measured().items()})
        return merged


def speaker_for(language: str) -> str:
    """Bulbul speaker for a session language, defaulting to the warm IVR voice."""
    return SPEAKERS.get(language, DEFAULT_SPEAKER)


def sentences(text: str) -> list[str]:
    """Split a reply into sentences for sentence-by-sentence synthesis (PRD C5).

    Splits on the Devanagari danda and double danda as well as Latin terminators, since
    a Hindi or Marathi reply ends its sentences with ``।`` and would otherwise be handed
    to Bulbul as one block -- which costs the whole reply's synthesis time before the
    first audio frame, and is exactly what ``time_to_first_audio_ms`` measures.
    """
    parts = [part.strip() for part in _SENTENCE_END.split(text.strip())]
    return [part for part in parts if part]


def decode_mp3_stream(buffer: bytes, emitted_samples: int) -> tuple[bytes, int]:
    """Decode a growing MP3 buffer to PCM16, returning only the samples not yet emitted.

    MP3 is framed and carries a bit reservoir, so a chunk off the wire is not
    independently decodable -- the second chunk Sarvam sends begins with a LAME tag, not
    a frame sync. Decoding the whole buffer each time and slicing off what is new is
    therefore the correct shape, not a lazy one; it is O(n^2) in chunks but n is one
    sentence (8 chunks / 43 KB in the live probe), so the cost is noise next to a
    network round trip.

    Returns ``(pcm16_bytes, total_samples_decoded)``. A buffer whose trailing frame is
    incomplete may fail to decode; that is not an error, it means "wait for more bytes",
    and the caller keeps the buffer and tries again.
    """
    import soundfile as sf

    try:
        with sf.SoundFile(io.BytesIO(buffer)) as handle:
            samples = handle.read(dtype="int16")
    except Exception:
        return b"", emitted_samples
    if samples.ndim > 1:
        samples = samples[:, 0]
    total = int(samples.shape[0])
    if total <= emitted_samples:
        return b"", emitted_samples
    return samples[emitted_samples:].tobytes(), total


def load_greeting(path: Path | None, *, sample_rate: int = OUTPUT_SAMPLE_RATE) -> bytes:
    """Load the recorded consent notice as PCM16 mono at ``sample_rate``.

    Raises:
        GreetingUnavailable: when no notice is configured, or the file is missing,
            unreadable, or not decodable audio. Every message names the path that was
            expected, because the operator's next action is to put a file there.
    """
    if path is None:
        raise GreetingUnavailable(
            "No recorded consent notice is configured (VoiceSettings.greeting_path is "
            "None). This is a recorded support call: the notice must play before any "
            "employee audio is consumed, so the session refuses to start rather than "
            "listen without it."
        )
    if not path.is_file():
        raise GreetingUnavailable(
            f"Recorded consent notice not found at {path}. The session refuses to start: "
            "skipping the notice would turn a consented recording into an unconsented one."
        )
    try:
        import soundfile as sf

        with sf.SoundFile(str(path)) as handle:
            samples = handle.read(dtype="int16")
            source_rate = handle.samplerate
    except GreetingUnavailable:
        raise
    except Exception as error:
        raise GreetingUnavailable(
            f"Recorded consent notice at {path} could not be decoded ({type(error).__name__})."
        ) from None
    if samples.ndim > 1:
        samples = samples[:, 0]
    if source_rate != sample_rate:
        import soxr

        samples = soxr.resample(samples, source_rate, sample_rate).astype(samples.dtype)
    audio = bytes(samples.tobytes())
    if not audio:
        raise GreetingUnavailable(f"Recorded consent notice at {path} decodes to no audio.")
    return audio


def _message(payload: dict[str, Any]) -> OutputTransportMessageFrame:
    """A room data message. ``LiveKitOutputTransport.send_message`` json-encodes dicts."""
    return OutputTransportMessageFrame(message={"v": DATA_MESSAGE_VERSION, **payload})


class _AudioFeed:
    """Adapts pushed audio chunks into the ``AsyncIterator[bytes]`` the STT adapter pulls.

    ``pulled`` is the point of the class as much as the queue is: it counts chunks the
    adapter actually took, which is how the tests prove that a dead STT stops consuming
    audio rather than merely stops reporting it.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.pulled = 0
        self.closed = False

    def push(self, chunk: bytes) -> None:
        if not self.closed:
            self._queue.put_nowait(chunk)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(None)

    async def chunks(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            self.pulled += 1
            yield chunk


@dataclass
class _Turn:
    """Monotonic timestamps for one turn. ``None`` means the stage has not happened."""

    index: int
    last_audio_at: float | None = None
    speech_stopped_at: float | None = None
    transcript_at: float | None = None
    tts_started_at: float | None = None
    latency: StageLatency = field(default_factory=StageLatency)


class GreetingGate(FrameProcessor):
    """Plays the recorded consent notice and holds back user audio until it has played.

    The gate is the head of the pipeline, upstream of VAD, so audio it drops is not
    merely un-transcribed -- it is never analysed, never buffered and never leaves the
    process. ``blocked`` and ``forwarded`` count those two outcomes so ordering can be
    asserted rather than assumed.

    The notice is emitted as ``TTSStartedFrame`` / ``TTSAudioRawFrame`` / ``TTSStoppedFrame``
    because that is the only sequence ``BaseOutputTransport`` turns into a
    ``BotStoppedSpeakingFrame`` once the audio has been written out; a plain
    ``OutputAudioRawFrame`` is played but never acknowledged, leaving the gate with
    nothing to open on.

    **The notice is not played on ``StartFrame``.** A pipeline starts when the agent is
    dispatched, which can be before anyone is in the room; the output transport writes the
    audio out and acknowledges it whether or not a single participant is subscribed, so
    starting on ``StartFrame`` would let the gate open on a notice that played to an empty
    room -- and the first thing the employee said on joining would go to Saaras with no
    notice ever delivered. Playback is therefore driven by the caller, from the transport's
    participant-joined event: see :meth:`announce` and ``run_session``. Offline tests call
    :meth:`announce` at the same point in the sequence, so the tested path and the
    production path are the same path.
    """

    def __init__(
        self,
        *,
        greeting_audio: bytes,
        sample_rate: int = OUTPUT_SAMPLE_RATE,
        chunk_ms: int = 20,
        ack_timeout_s: float = GREETING_ACK_TIMEOUT_S,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not greeting_audio:
            raise GreetingUnavailable("Refusing to build a voice pipeline with no consent notice.")
        self._audio = greeting_audio
        self._sample_rate = sample_rate
        self._chunk_bytes = max(2, (sample_rate * chunk_ms // 1000) * 2)
        self._ack_timeout_s = ack_timeout_s
        self._played_at: float | None = None
        self.opened = False
        self.announcements = 0
        self.notice_unconfirmed = False
        self.blocked = 0
        self.forwarded = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if not self.opened:
                await self._check_ack_deadline()
                self.blocked += 1
                return
            self.forwarded += 1
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

        if isinstance(frame, BotStoppedSpeakingFrame) and not self.opened:
            # The notice finished playing out of the transport. Only now does this
            # session start consuming the employee's audio.
            self._open("playback_complete")
        elif isinstance(frame, EndFrame | CancelFrame):
            self._reset()

    async def announce(self) -> None:
        """Play the consent notice, closing the gate until it is acknowledged.

        Called when a participant joins -- including one who joins after an earlier notice
        finished. A late joiner has heard nothing, so the notice is played again and the
        gate closes for the replay: the alternative is a room that keeps capturing while
        somebody who was never notified is in it.
        """
        self.opened = False
        self.notice_unconfirmed = False
        self._played_at = None
        self.announcements += 1
        await self._play_greeting()

    def _reset(self) -> None:
        """Forget the notice entirely when the pipeline ends.

        ``_played_at`` has to go with ``opened``. Leaving a stale timestamp behind means a
        later audio frame finds a deadline that expired long ago and opens the gate on a
        notice belonging to a finished session.
        """
        self.opened = False
        self.notice_unconfirmed = False
        self._played_at = None

    async def _check_ack_deadline(self) -> None:
        """Report a notice that was never acknowledged. Never open on one.

        The deadline exists because a client that does not subscribe would otherwise leave
        the gate waiting forever with nothing said about it. It is a diagnostic, NOT a way
        in: the gate stays shut, because "we waited 30 seconds" is not evidence that anyone
        was told the call is recorded, and a consent control that opens on the absence of a
        signal is not a control. Failing closed costs a session; failing open records
        someone who was never notified.
        """
        if self._played_at is None or self.notice_unconfirmed:
            return
        if time.monotonic() - self._played_at < self._ack_timeout_s:
            return
        self.notice_unconfirmed = True
        logger.warning(
            f"{self}: consent notice was never acknowledged after {self._ack_timeout_s:.0f}s; "
            "input stays closed and this session will capture nothing"
        )
        await self.push_frame(_message({"type": "notice", "state": "unconfirmed"}))

    def _open(self, reason: str) -> None:
        self.opened = True
        logger.info(f"{self}: consent notice complete, input opened (reason={reason})")

    async def _play_greeting(self) -> None:
        await self.push_frame(_message({"type": "notice", "state": "playing"}))
        await self.push_frame(TTSStartedFrame())
        for offset in range(0, len(self._audio), self._chunk_bytes):
            await self.push_frame(
                TTSAudioRawFrame(
                    audio=self._audio[offset : offset + self._chunk_bytes],
                    sample_rate=self._sample_rate,
                    num_channels=1,
                )
            )
        await self.push_frame(TTSStoppedFrame())
        self._played_at = time.monotonic()
        logger.info(f"{self}: consent notice emitted ({len(self._audio)} bytes)")


class SarvamSTTProcessor(FrameProcessor):
    """Saaras streaming STT, one stream per VAD-delimited utterance.

    The adapter's contract is a *pull* one -- ``stream(audio: AsyncIterator[bytes])``
    consumes until the iterator ends, then flushes -- while a Pipecat pipeline *pushes*
    audio frames. :class:`_AudioFeed` is the join between them, and VAD supplies the
    utterance boundary: ``VADUserStartedSpeakingFrame`` opens a feed (with the pre-roll
    Silero's ``start_secs`` would otherwise have eaten), ``VADUserStoppedSpeakingFrame``
    closes it, and the transcript the adapter yields as the stream drains is the FINAL
    one that ``DecideProcessor`` acts on. Everything yielded before that is partial and
    is published to the room as a caption.

    When the stream raises, the processor goes to chat mode permanently: it tells the
    client, closes the feed, and drops audio instead of forwarding it. There is no path
    back to listening within a session, because there is no evidence the vendor
    recovered and a room that silently resumes recording after saying it stopped is
    worse than one that stays in chat.
    """

    def __init__(
        self,
        *,
        stt: STT,
        language: str = "auto",
        preroll_ms: int = PREROLL_MS,
        sample_rate: int = INPUT_SAMPLE_RATE,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._stt = stt
        self._language = language
        self._sample_rate = sample_rate
        self._preroll_bytes = (sample_rate * preroll_ms // 1000) * 2
        self._preroll = bytearray()
        self._feed: _AudioFeed | None = None
        self._task: asyncio.Task[None] | None = None
        self._segments: list[str] = []
        # What Saaras actually identified, as opposed to what it was asked for. With
        # `language="auto"` -- the configured default -- `self._language` is the literal
        # string "auto", which is not a language tag and is useless to a client trying to
        # pick a font or a direction for the caption it is about to render.
        self._detected: str | None = None
        self.listening = True
        self.audio_frames_consumed = 0
        self.streams_opened = 0
        self.turn: _Turn | None = None
        self.on_final: Callable[[_Turn], None] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            if not self.listening:
                # Chat mode: the audio stops here. Nothing downstream sees it either.
                return
            self.audio_frames_consumed += 1
            self._accept(frame.audio)
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self._start_utterance()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._end_utterance()
        elif isinstance(frame, EndFrame | CancelFrame):
            await self._close()

    def _accept(self, audio: bytes) -> None:
        if self.turn is not None:
            self.turn.last_audio_at = time.perf_counter()
        if self._feed is not None:
            self._feed.push(audio)
            return
        self._preroll.extend(audio)
        if len(self._preroll) > self._preroll_bytes:
            del self._preroll[: len(self._preroll) - self._preroll_bytes]

    async def _start_utterance(self) -> None:
        if not self.listening or self._feed is not None:
            return
        index = self.streams_opened
        self.streams_opened = index + 1
        self.turn = _Turn(index=index)
        self._segments = []
        # Per utterance: an employee who switches language mid-call must not have this
        # turn labelled with the previous turn's identification.
        self._detected = None
        feed = _AudioFeed()
        if self._preroll:
            feed.push(bytes(self._preroll))
            self._preroll.clear()
        self._feed = feed
        self._task = self.create_task(self._consume(feed, self.turn))

    async def _end_utterance(self) -> None:
        turn = self.turn
        if turn is not None and turn.last_audio_at is not None:
            # The endpointing delay the employee waits through: their last sample in,
            # VAD's verdict out. Absent when no audio arrived, never zero.
            turn.latency.vad_ms = (time.perf_counter() - turn.last_audio_at) * 1000
        if turn is not None:
            turn.speech_stopped_at = time.perf_counter()
        if self._feed is not None:
            self._feed.close()

    async def _consume(self, feed: _AudioFeed, turn: _Turn) -> None:
        try:
            async for segment in self._stt.stream(feed.chunks(), language=self._language):
                self._segments.append(segment.text)
                await self._publish(segment, final=False)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._fail(type(error).__name__)
            return
        finally:
            if self._feed is feed:
                self._feed = None
        await self._finalize(turn)

    async def _finalize(self, turn: _Turn) -> None:
        text = " ".join(part.strip() for part in self._segments if part.strip()).strip()
        turn.transcript_at = time.perf_counter()
        if turn.speech_stopped_at is not None:
            turn.latency.stt_ms = (turn.transcript_at - turn.speech_stopped_at) * 1000
        if not text:
            logger.info(f"{self}: turn {turn.index} produced no transcript")
            return
        clean = redact(text)
        await self.push_frame(
            _message(
                {
                    "type": "transcript",
                    "final": True,
                    "text": clean,
                    # The language Saaras identified across this utterance's segments, not
                    # the one it was asked for -- which is "auto" in every configuration
                    # this pipeline ships with, and not a language tag a client can use.
                    "language": self._detected or self._language,
                    "turn": turn.index,
                }
            )
        )
        if self.on_final is not None:
            self.on_final(turn)
        await self.push_frame(
            TranscriptionFrame(
                text=clean,
                user_id="",
                timestamp=str(turn.index),
                finalized=True,
            )
        )

    async def _publish(self, segment: TranscriptSegment, *, final: bool) -> None:
        text = segment.text.strip()
        if not text:
            return
        if segment.language:
            self._detected = segment.language
        clean = redact(text)
        await self.push_frame(
            _message(
                {
                    "type": "transcript",
                    "final": final,
                    "text": clean,
                    "language": segment.language,
                    "turn": self.turn.index if self.turn else 0,
                }
            )
        )
        await self.push_frame(
            InterimTranscriptionFrame(text=clean, user_id="", timestamp=str(segment.start_ms))
        )

    async def _fail(self, error_name: str) -> None:
        """Stop listening and put the client in chat mode. No content, no recovery."""
        self.listening = False
        if self._feed is not None:
            self._feed.close()
            self._feed = None
        self._preroll.clear()
        logger.warning(f"{self}: STT unavailable ({error_name}); switching the client to chat")
        await self.push_frame(
            _message({"type": "mode", "mode": "chat", "reason": "stt_unavailable"})
        )

    async def _close(self) -> None:
        if self._feed is not None:
            self._feed.close()
            self._feed = None
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(Exception):
                await self.cancel_task(task)


class DecideProcessor(FrameProcessor):
    """Runs the helpdesk graph on the FINAL transcript and streams the reply by sentence.

    ``decide`` is injected rather than imported so a unit test never touches Postgres,
    Qdrant or Claude; in production it is ``helpdesk_agent.graph.decide``. The reply is
    split by :func:`sentences` and pushed as separate ``TextFrame``s so Bulbul can start
    speaking the first sentence while the rest are still queued -- that gap is most of
    ``time_to_first_audio_ms``.
    """

    def __init__(
        self,
        *,
        decide: DecideCallable,
        language: str,
        history_limit: int = 8,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._decide = decide
        self._language = language
        self._history_limit = history_limit
        self._history: list[dict[str, Any]] = []
        self.turn: _Turn | None = None
        self.decisions = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame) and frame.finalized:
            await self._respond(frame.text)

    async def _respond(self, utterance: str) -> None:
        turn = self.turn
        started = time.perf_counter()
        try:
            decision = await self._decide(utterance, self._language, list(self._history))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Silence is the worst failure mode a voice call has: the employee has just
            # finished speaking and cannot tell a broken graph from a slow one or a dead
            # line. So the client is told, in the same shape `SarvamSTTProcessor._fail`
            # uses -- but `recoverable: true` and NO latching flag, because unlike a dead
            # STT this is one turn's failure: the next utterance opens a stream and
            # reaches `decide` normally. `decide_ms` stays unset; the stage did not run.
            #
            # No spoken fallback is synthesized here. Saying "sorry, something went
            # wrong" out loud would need approved per-language wording, which this repo
            # does not have, and inventing copy for an employee-facing voice line is not
            # this module's call to make. See docs/build/BLOCKERS.md.
            logger.warning(f"{self}: decide failed ({type(error).__name__})")
            await self.push_frame(
                _message({"type": "error", "stage": "decide", "recoverable": True})
            )
            return
        if turn is not None:
            # decide_ms is wall time around the graph call. The graph's own node timings
            # live in `turns.latency_ms` unprefixed; this is the whole call, including
            # persistence, which is why `merge_into` namespaces it.
            turn.latency.decide_ms = (time.perf_counter() - started) * 1000
        self.decisions += 1
        reply = str(decision.get("reply", ""))
        self._history.append(
            {
                "utterance": utterance,
                "action": decision.get("action"),
                "reply": reply,
            }
        )
        del self._history[: max(0, len(self._history) - self._history_limit)]
        await self.push_frame(
            _message(
                {
                    "type": "decision",
                    "action": decision.get("action"),
                    "turn": turn.index if turn else 0,
                }
            )
        )
        for sentence in sentences(reply):
            await self.push_frame(TextFrame(text=sentence))


class SarvamTTSProcessor(FrameProcessor):
    """Bulbul streaming TTS, sentence in, PCM16 out, with the turn's audio clock.

    The adapter yields MP3 (verified live, see the module docstring); this processor
    decodes it to PCM16 because that is what LiveKit publishes. The first frame it pushes
    downstream stops the turn's clock: ``time_to_first_audio_ms`` is measured there, from
    the FINAL transcript, and nowhere else.
    """

    def __init__(
        self,
        *,
        tts: TTS,
        language: str,
        voice: str | None = None,
        sample_rate: int = OUTPUT_SAMPLE_RATE,
        decoder: Decoder = decode_mp3_stream,
        on_turn: TurnCallback | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._tts = tts
        self._language = language
        self._voice = voice or speaker_for(language)
        self._sample_rate = sample_rate
        self._decoder = decoder
        self._on_turn = on_turn
        self._queue: asyncio.Queue[str | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self.turn: _Turn | None = None
        self.audio_frames_emitted = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if (
            isinstance(frame, TextFrame)
            and not isinstance(frame, TranscriptionFrame | InterimTranscriptionFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            await self._speak(frame.text)
            return

        await self.push_frame(frame, direction)

        if isinstance(frame, EndFrame | CancelFrame):
            await self._close()

    async def _speak(self, text: str) -> None:
        if not text.strip():
            return
        if self._queue is None:
            self._queue = asyncio.Queue()
            if self.turn is not None and self.turn.tts_started_at is None:
                self.turn.tts_started_at = time.perf_counter()
            self._task = self.create_task(self._synthesize(self._queue, self.turn))
        self._queue.put_nowait(text)

    async def _synthesize(self, queue: asyncio.Queue[str | None], turn: _Turn | None) -> None:
        async def chunks() -> AsyncIterator[str]:
            while True:
                try:
                    text = await asyncio.wait_for(queue.get(), 2.0)
                except TimeoutError:
                    # A turn's reply is complete when no further sentence arrives; the
                    # adapter needs the iterator to end so it can flush the last one.
                    return
                if text is None:
                    return
                yield text

        buffer = bytearray()
        emitted_samples = 0
        started = True
        try:
            await self.push_frame(TTSStartedFrame())
            async for encoded in self._tts.stream(
                chunks(), language=self._language, voice=self._voice
            ):
                buffer.extend(encoded)
                pcm, emitted_samples = self._decoder(bytes(buffer), emitted_samples)
                if not pcm:
                    continue
                if started and turn is not None:
                    self._stop_clock(turn)
                    started = False
                self.audio_frames_emitted += 1
                await self.push_frame(
                    TTSAudioRawFrame(audio=pcm, sample_rate=self._sample_rate, num_channels=1)
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"{self}: TTS unavailable ({type(error).__name__})")
        finally:
            await self.push_frame(TTSStoppedFrame())
            self._queue = None
            if turn is not None and self._on_turn is not None:
                await self._on_turn(turn.index, turn.latency)

    def _stop_clock(self, turn: _Turn) -> None:
        now = time.perf_counter()
        if turn.tts_started_at is not None:
            turn.latency.tts_ms = (now - turn.tts_started_at) * 1000
        if turn.transcript_at is not None:
            turn.latency.time_to_first_audio_ms = (now - turn.transcript_at) * 1000
        logger.info(f"{self}: turn {turn.index} first audio, latency_ms={turn.latency.measured()}")

    async def _close(self) -> None:
        if self._queue is not None:
            self._queue.put_nowait(None)
        task, self._task = self._task, None
        if task is not None:
            with contextlib.suppress(Exception):
                await self.cancel_task(task)


def build_pipeline(
    settings: VoiceSettings,
    *,
    stt: STT,
    tts: TTS,
    decide: DecideCallable,
    greeting_audio: bytes,
    head: FrameProcessor | None = None,
    tail: FrameProcessor | None = None,
    vad: VADAnalyzer | None = None,
    on_turn: TurnCallback | None = None,
    decoder: Decoder = decode_mp3_stream,
    ack_timeout_s: float = GREETING_ACK_TIMEOUT_S,
) -> Pipeline:
    """Construct the voice pipeline without running it.

    ``head`` and ``tail`` are the transport's input and output processors; both are
    optional so the pipeline can be driven frame-by-frame in a test with no LiveKit
    room, no Docker and no network. ``greeting_audio`` is already-decoded PCM16 -- the
    consent decision (:func:`load_greeting`) is made before this point, so a pipeline
    can never be constructed without a notice.

    The one shared mutable object between the processors is the current ``_Turn``: STT
    creates it and hands it to decide and TTS via ``on_final``, so all five latencies
    belong to the same turn even when the next utterance starts before the last reply
    has finished speaking.
    """
    stt_processor = SarvamSTTProcessor(stt=stt, language="auto")
    decide_processor = DecideProcessor(decide=decide, language=settings.language)
    tts_processor = SarvamTTSProcessor(
        tts=tts,
        language=settings.language,
        sample_rate=OUTPUT_SAMPLE_RATE,
        decoder=decoder,
        on_turn=on_turn,
    )

    def adopt(turn: _Turn) -> None:
        decide_processor.turn = turn
        tts_processor.turn = turn

    stt_processor.on_final = adopt

    processors: list[FrameProcessor] = [
        GreetingGate(
            greeting_audio=greeting_audio,
            sample_rate=OUTPUT_SAMPLE_RATE,
            ack_timeout_s=ack_timeout_s,
        ),
        VADProcessor(vad_analyzer=vad or SileroVADAnalyzer(sample_rate=INPUT_SAMPLE_RATE)),
        stt_processor,
        decide_processor,
        tts_processor,
    ]
    if head is not None:
        processors.insert(0, head)
    if tail is not None:
        processors.append(tail)
    return Pipeline(processors)


def _access_token(settings: VoiceSettings) -> str:
    """Mint the agent's room token. Keys come from the environment, never the repo."""
    from livekit import api

    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(settings.identity)
        .with_name(settings.identity)
        .with_grants(api.VideoGrants(room_join=True, room=settings.room))
        .to_jwt()
    )


async def run_session(
    settings: VoiceSettings,
    *,
    decide: DecideCallable,
    session_id: str,
    employee_id: str,
    on_turn: TurnCallback | None = None,
) -> None:
    """Join the room and run one voice session until the pipeline ends.

    The consent notice is loaded *first*, before a token is minted or a room is joined,
    so a missing notice fails with :class:`GreetingUnavailable` and no room ever opens.

    Args:
        settings: room, URL, identity, language and the consent notice path.
        decide: the helpdesk graph entry point; ``helpdesk_agent.graph.decide`` in
            production, a stub in tests.
        session_id: helpdesk session this call belongs to.
        employee_id: the authenticated employee; carried for attribution only, and
            never sent to a vendor or written to a log by this module.
        on_turn: receives ``(turn_index, StageLatency)`` when a turn's first audio has
            been emitted. ``StageLatency.merge_into`` produces the ``turns.latency_ms``
            payload; the write itself belongs to the caller's transaction, which is why
            this is a hook rather than a database call here.

    Raises:
        GreetingUnavailable: no readable consent notice; nothing is started.
    """
    greeting_audio = load_greeting(settings.greeting_path)

    from indic_platform.adapters.sarvam_stt import SarvamSTT
    from indic_platform.adapters.sarvam_tts import SarvamTTS
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.transports.livekit.transport import LiveKitParams, LiveKitTransport
    from pipecat.workers.runner import WorkerRunner

    transport = LiveKitTransport(
        url=settings.livekit_url,
        token=_access_token(settings),
        room_name=settings.room,
        params=LiveKitParams(
            audio_in_enabled=True,
            audio_in_sample_rate=INPUT_SAMPLE_RATE,
            audio_out_enabled=True,
            audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
        ),
    )
    pipeline = build_pipeline(
        settings,
        stt=SarvamSTT(),
        tts=SarvamTTS(),
        decide=decide,
        greeting_audio=greeting_audio,
        head=transport.input(),
        tail=transport.output(),
        on_turn=on_turn,
    )
    gate = next(p for p in pipeline.processors if isinstance(p, GreetingGate))
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=INPUT_SAMPLE_RATE,
            audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
            enable_metrics=True,
        ),
    )

    @transport.event_handler("on_participant_connected")
    async def _on_participant_connected(_transport: BaseTransport, participant_id: str) -> None:
        # Every join, not just the first. The pipeline may have started well before anyone
        # was in the room -- the agent is dispatched, not summoned -- so playing the notice
        # on pipeline start would play it to nobody. And a participant who joins later has
        # heard nothing either: they get the notice too, and the gate closes again while it
        # plays, because a room that keeps capturing around somebody who was never told is
        # the exact situation the notice exists to prevent.
        await gate.announce()

    @transport.event_handler("on_participant_disconnected")
    async def _on_participant_disconnected(_transport: BaseTransport, participant_id: str) -> None:
        await worker.stop_when_done()

    logger.info(
        "voice session starting "
        f"(room={settings.room!r}, language={settings.language}, session={session_id})"
    )
    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()


__all__: Sequence[str] = (
    "DATA_MESSAGE_VERSION",
    "DecideProcessor",
    "GreetingGate",
    "GreetingUnavailable",
    "INPUT_SAMPLE_RATE",
    "OUTPUT_SAMPLE_RATE",
    "SPEAKERS",
    "SarvamSTTProcessor",
    "SarvamTTSProcessor",
    "StageLatency",
    "VoiceSettings",
    "build_pipeline",
    "decode_mp3_stream",
    "load_greeting",
    "run_session",
    "sentences",
    "speaker_for",
)
