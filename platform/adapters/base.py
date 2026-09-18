from collections.abc import AsyncIterator
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field, model_validator

T = TypeVar("T", bound=BaseModel)


class TranscriptSegment(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    speaker: str | None = None
    text: str
    language: str
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self) -> "TranscriptSegment":
        if self.end_ms < self.start_ms:
            raise ValueError("Segment ends before it starts")
        return self


class STT(Protocol):
    def stream(
        self, audio: AsyncIterator[bytes], *, language: str = "auto"
    ) -> AsyncIterator[TranscriptSegment]: ...
    async def batch(
        self, audio_uri: str, *, language: str = "auto", diarize: bool = False
    ) -> list[TranscriptSegment]: ...


class TTS(Protocol):
    def stream(
        self, text_chunks: AsyncIterator[str], *, language: str, voice: str
    ) -> AsyncIterator[bytes]: ...


class Translate(Protocol):
    async def translate(
        self, text: str, *, source: str = "auto", target: str, mode: str = "formal"
    ) -> str: ...


class Dubbing(Protocol):
    async def submit(
        self, video_uri: str, *, target_languages: list[str], voice_map: dict[str, str]
    ) -> str: ...
    async def status(self, job_id: str) -> dict[str, Any]: ...
    async def fetch(self, job_id: str) -> dict[str, Any]: ...


class LLM(Protocol):
    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        model: str,
        cache_system: bool = True,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> T: ...
    def stream_text(
        self, *, system: str, messages: list[dict[str, Any]], model: str
    ) -> AsyncIterator[str]: ...
