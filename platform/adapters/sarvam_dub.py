import asyncio
import hashlib
from typing import Any

from indic_platform.adapters.sarvam_common import (
    OPTIONS,
    SarvamAdapter,
    local_media,
    media_duration,
)


class SarvamDubbing(SarvamAdapter):
    def __init__(self, *, source_language: str = "en-IN", **kwargs: Any) -> None:
        super().__init__("dubbing", **kwargs)
        self.source_language = source_language

    async def submit(
        self, video_uri: str, *, target_languages: list[str], voice_map: dict[str, str]
    ) -> str:
        if not target_languages:
            raise ValueError("At least one target language is required")
        voices = set(voice_map.values())
        if len(voices) > 1:
            raise ValueError("Current SDK supports one voice_id per job, not per-speaker mapping")
        path = local_media(video_uri)
        duration = await asyncio.to_thread(media_duration, path)
        data = await asyncio.to_thread(path.read_bytes)
        key = hashlib.sha256(
            data
            + repr(
                (sorted(target_languages), sorted(voice_map.items()), self.source_language)
            ).encode()
        ).hexdigest()
        options = {**OPTIONS, "additional_headers": {"Idempotency-Key": key}}
        job = await self.runtime.call(
            lambda: self.client.dubbing.create(
                source_language_code=self.source_language,
                target_language_codes=target_languages,
                voice_cloning=False,
                voice_id=next(iter(voices), None),
                export_options=["video", "audio", "srt"],
                request_options=options,
            ),
            model="sarvam-dubbing",
        )
        if not job.data.upload_url:
            raise ValueError("Dubbing response omitted upload URL")

        async def upload() -> None:
            response = await self.client.dubbing.upload(job.data.upload_url, str(path), timeout=120)
            response.raise_for_status()

        await self.runtime.call(upload, model="sarvam-dubbing", timeout=120)
        await self.runtime.call(
            lambda: self.client.dubbing.start(job.data.job_id, request_options=options),
            model="sarvam-dubbing",
            units={"seconds": duration * len(target_languages)},
        )
        return str(job.data.job_id)

    async def status(self, job_id: str) -> dict[str, Any]:
        response = await self.request(
            self.client.dubbing.get_live_status, price_model="sarvam-dubbing", job_id=job_id
        )
        return response.model_dump(mode="json")

    async def fetch(self, job_id: str) -> dict[str, Any]:
        response = await self.request(
            self.client.dubbing.get_export_status, price_model="sarvam-dubbing", job_id=job_id
        )
        return response.model_dump(mode="json")
