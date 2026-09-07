import asyncio
import base64
import contextlib
import hashlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from indic_platform.adapters.base import TranscriptSegment
from indic_platform.adapters.sarvam_common import (
    OPTIONS,
    SarvamAdapter,
    local_media,
    media_duration,
    upload_blob,
)


def segments(
    payload: dict[str, Any], language: str, duration: float = 0
) -> list[TranscriptSegment]:
    language = payload.get("language_code") or language
    diarized = payload.get("diarized_transcript") or {}
    entries = diarized.get("entries", []) if isinstance(diarized, dict) else diarized
    if entries:
        return [
            TranscriptSegment(
                start_ms=round(e["start_time_seconds"] * 1000),
                end_ms=round(e["end_time_seconds"] * 1000),
                speaker=e.get("speaker_id"),
                text=e["transcript"],
                language=language,
            )
            for e in entries
        ]
    return [
        TranscriptSegment(
            start_ms=0, end_ms=round(duration * 1000), text=payload["transcript"], language=language
        )
    ]


class SarvamSTT(SarvamAdapter):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__("stt", **kwargs)

    async def stream(
        self, audio: AsyncIterator[bytes], *, language: str = "auto"
    ) -> AsyncIterator[TranscriptSegment]:
        """Input: 16kHz mono PCM16. Streaming timestamps are receive-window estimates."""
        units: dict[str, float] = {"seconds": 0}

        async def generate() -> AsyncIterator[TranscriptSegment]:
            async with self.client.speech_to_text_streaming.connect(
                model="saaras:v3",
                language_code="unknown" if language == "auto" else language,
                input_audio_codec="pcm_s16le",
                sample_rate="16000",
                flush_signal="true",
                request_options=OPTIONS,
            ) as socket:
                finished = asyncio.Event()

                async def send() -> None:
                    while True:
                        try:
                            chunk = await asyncio.wait_for(anext(audio), 30)
                        except StopAsyncIteration:
                            break
                        await asyncio.wait_for(
                            socket.transcribe(
                                audio=base64.b64encode(chunk).decode(),
                                encoding="audio/pcm",
                                sample_rate=16000,
                            ),
                            30,
                        )
                        units["seconds"] += len(chunk) / 32000
                    await socket.flush()
                    finished.set()

                sender = asyncio.create_task(send())
                offset = 0
                try:
                    while True:
                        receiver = asyncio.create_task(socket.recv())
                        done, _ = await asyncio.wait(
                            {receiver, sender}, timeout=30, return_when=asyncio.FIRST_COMPLETED
                        )
                        if sender in done and sender.exception():
                            receiver.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await receiver
                            raise sender.exception()  # type: ignore[misc]
                        try:
                            event = await asyncio.wait_for(receiver, 30)
                        finally:
                            if not receiver.done():
                                receiver.cancel()
                        if event.type == "error":
                            raise RuntimeError("Sarvam STT stream rejected")
                        if hasattr(event.data, "transcript"):
                            end = round(units["seconds"] * 1000)
                            yield TranscriptSegment(
                                start_ms=offset,
                                end_ms=end,
                                text=event.data.transcript,
                                language=event.data.language_code or language,
                            )
                            offset = end
                            if finished.is_set():
                                return
                        elif (
                            finished.is_set() and getattr(event.data, "event_type", None) == "flush"
                        ):
                            return
                finally:
                    sender.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await sender

        async with contextlib.aclosing(
            self.runtime.stream(generate, model="saaras:v3", units=units)
        ) as stream:
            async for segment in stream:
                yield segment

    async def batch(
        self, audio_uri: str, *, language: str = "auto", diarize: bool = False
    ) -> list[TranscriptSegment]:
        path = local_media(audio_uri)
        duration = await asyncio.to_thread(media_duration, path)
        data = await asyncio.to_thread(path.read_bytes)
        model = "saaras:v3:diarized" if diarize else "saaras:v3"
        key = hashlib.sha256(data + f"{language}:{diarize}:saaras:v3".encode()).hexdigest()
        api = self.client.speech_to_text_job
        options = {**OPTIONS, "additional_headers": {"Idempotency-Key": key}}
        job = await self.runtime.call(
            lambda: api.initialise(
                job_parameters={
                    "model": "saaras:v3",
                    "language_code": "unknown" if language == "auto" else language,
                    "with_diarization": diarize,
                    "with_timestamps": True,
                },
                request_options=options,
            ),
            model=model,
        )
        links = await self.request(
            api.get_upload_links, price_model=model, job_id=job.job_id, files=[path.name]
        )
        url = links.upload_urls[path.name].file_url
        await self.runtime.call(lambda: upload_blob(url, data), model=model, timeout=120)
        await self.runtime.call(
            lambda: api.start(job.job_id, request_options=options),
            model=model,
            units={"seconds": duration},
        )
        deadline, interval = time.monotonic() + 600, 1.0
        while True:
            status = await self.request(api.get_status, price_model=model, job_id=job.job_id)
            state = status.job_state.lower()
            if state == "completed":
                break
            if state in {"failed", "cancelled"}:
                raise RuntimeError("Sarvam batch job failed")
            if time.monotonic() + interval >= deadline:
                raise TimeoutError("Sarvam batch polling deadline exceeded")
            await asyncio.sleep(interval)
            interval = min(interval * 2, 30)
        details = status.job_details or []
        if any(d.state != "Success" for d in details):
            raise RuntimeError("Sarvam batch contains failed files")
        names = [o.file_name for d in details for o in (d.outputs or [])]
        if not names:
            raise ValueError("Completed batch has no output")
        downloads = await self.request(
            api.get_download_links, price_model=model, job_id=job.job_id, files=names
        )
        output: list[TranscriptSegment] = []
        for name in names:

            async def fetch(name: str = name) -> dict[str, Any]:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(downloads.download_urls[name].file_url)
                    response.raise_for_status()
                    return json.loads(response.content)

            payload = await self.runtime.call(fetch, model=model)
            parsed = segments(payload, language, duration)
            output.extend(parsed)
        return output
