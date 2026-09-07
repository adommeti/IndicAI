import asyncio
import base64
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from indic_platform.adapters.sarvam_common import OPTIONS, SarvamAdapter


class SarvamTTS(SarvamAdapter):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__("tts", **kwargs)

    async def speak(self, text: str, *, language: str, voice: str = "priya") -> bytes:
        clean = self.redact(text)
        result = await self.request(
            self.client.text_to_speech.convert,
            price_model="bulbul:v3",
            units={"characters": len(clean)},
            text=clean,
            language_code=language,
            speaker=voice,
            model="bulbul:v3",
            output_audio_codec="wav",
        )
        return b"".join(base64.b64decode(audio, validate=True) for audio in result.audios)

    async def stream(
        self, text_chunks: AsyncIterator[str], *, language: str, voice: str
    ) -> AsyncIterator[bytes]:
        # Each upstream chunk is a complete sentence, as required by the PRD.
        while True:
            try:
                text = await asyncio.wait_for(anext(text_chunks), 30)
            except StopAsyncIteration:
                return
            clean = self.redact(text)
            units: dict[str, float] = {}

            async def generate(
                clean: str = clean, units: dict[str, float] = units
            ) -> AsyncIterator[bytes]:
                async with self.client.text_to_speech_streaming.connect(
                    model="bulbul:v3",
                    send_completion_event="true",
                    request_options=OPTIONS,
                ) as socket:
                    await socket.configure(
                        target_language_code=language,
                        speaker=voice,
                        output_audio_codec="mp3",
                        speech_sample_rate=16000,
                    )
                    await socket.convert(clean)
                    units["characters"] = len(clean)
                    await socket.flush()
                    while True:
                        event = await asyncio.wait_for(socket.recv(), 30)
                        if event.type == "audio":
                            yield base64.b64decode(event.data.audio, validate=True)
                        elif event.type == "event" and event.data.event_type == "final":
                            return
                        elif event.type == "error":
                            raise RuntimeError("Sarvam TTS stream rejected")

            async with contextlib.aclosing(
                self.runtime.stream(generate, model="bulbul:v3", units=units)
            ) as stream:
                async for chunk in stream:
                    yield chunk
