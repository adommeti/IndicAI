import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.security.redact import redact
from sarvamai import AsyncSarvamAI
from sarvamai.core.request_options import RequestOptions

OPTIONS: RequestOptions = {"max_retries": 0, "timeout_in_seconds": 30}


class SarvamAdapter:
    def __init__(
        self,
        capability: str,
        *,
        client: AsyncSarvamAI | None = None,
        runtime: AdapterRuntime | None = None,
        redactor: Callable[[str], str] = redact,
    ) -> None:
        self.client = client or AsyncSarvamAI(
            api_subscription_key=os.environ["SARVAM_API_KEY"], timeout=30
        )
        self.runtime = runtime or AdapterRuntime("sarvam", capability)
        self.redact = redactor

    @property
    def degraded_mode(self) -> str | None:
        return self.runtime.degraded_mode

    async def request(
        self, method: Any, *, price_model: str, units: dict[str, float] | None = None, **kwargs: Any
    ) -> Any:
        return await self.runtime.call(
            lambda: method(**kwargs, request_options=OPTIONS), model=price_model, units=units
        )


def local_media(uri: str) -> Path:
    """P0 media boundary: local paths/file URIs; apps materialize object-store assets."""
    parsed = urlparse(uri)
    if parsed.scheme not in ("", "file") or parsed.netloc not in ("", "localhost"):
        raise ValueError("Materialize media to a local path or file:// URI before submission")
    path = Path(unquote(parsed.path)).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Media must be a regular file")
    return path


async def upload_blob(
    url: str, data: bytes, content_type: str = "application/octet-stream"
) -> None:
    # Only signed vendor upload URLs are accepted here; no API credentials sent to storage.
    if urlparse(url).scheme != "https":
        raise ValueError("Vendor storage URL must use HTTPS")
    async with httpx.AsyncClient(timeout=120) as client:
        result = await client.put(
            url, content=data, headers={"x-ms-blob-type": "BlockBlob", "Content-Type": content_type}
        )
        result.raise_for_status()


def media_duration(path: Path) -> float:
    """Duration for billing; WAV uses stdlib, other media requires ffprobe."""
    import json
    import math
    import subprocess
    import wave

    try:
        with wave.open(str(path), "rb") as wav:
            duration = wav.getnframes() / wav.getframerate()
    except (wave.Error, EOFError):
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        duration = float(json.loads(result.stdout)["format"]["duration"])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Media must have a positive finite duration")
    return duration
