import json
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.adapters.sarvam_dub import SarvamDubbing
from indic_platform.adapters.sarvam_stt import SarvamSTT
from indic_platform.obs.langfuse import MemorySink
from sarvamai import AsyncSarvamAI


@pytest.fixture
def media(tmp_path: Path) -> Path:
    path = tmp_path / "sample.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 16000)
    return path


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("diarize", [False, True])
async def test_batch_http_job_flow(
    media: Path, monkeypatch: pytest.MonkeyPatch, failed: bool, diarize: bool
) -> None:
    duration = 1 if diarize else 31
    monkeypatch.setattr("indic_platform.adapters.sarvam_stt.media_duration", lambda _: duration)
    requests: list[httpx.Request] = []
    init_keys: list[str] = []
    uploads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal uploads
        requests.append(request)
        path = request.url.path
        payload: dict[str, Any]
        status = {
            "job_id": "job-1",
            "job_state": "Completed",
            "created_at": "now",
            "updated_at": "now",
            "storage_container_type": "Azure",
            "job_details": [
                {
                    "inputs": [{"file_name": media.name, "file_id": "1"}],
                    "outputs": [{"file_name": "result.json", "file_id": "2"}],
                    "state": "Success",
                }
            ],
        }
        if request.url.host == "storage.test":
            assert "api-subscription-key" not in request.headers
            if request.method == "PUT":
                uploads += 1
                return httpx.Response(503 if uploads == 1 else 201)
            return httpx.Response(200, json={"transcript": "नमस्ते", "language_code": "hi-IN"})
        if path.endswith("/job/v1"):
            init_keys.append(request.headers["Idempotency-Key"])
            if len(init_keys) == 1:
                return httpx.Response(429, json={"message": "busy"})
            payload = {**status, "job_parameters": {}}
        elif "upload" in path:
            payload = {
                **status,
                "upload_urls": {media.name: {"file_url": "https://storage.test/in"}},
            }
        elif "download" in path:
            payload = {
                **status,
                "download_urls": {"result.json": {"file_url": "https://storage.test/out"}},
            }
        elif "status" in path and failed:
            payload = {**status, "job_state": "Failed"}
        else:
            payload = status
        return httpx.Response(200, json=payload)

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    async with original(transport=transport) as http:
        sink = MemorySink()
        adapter = SarvamSTT(
            client=AsyncSarvamAI(api_subscription_key="mock", httpx_client=http),
            runtime=AdapterRuntime("sarvam", "stt", sink=sink, retry_base=0),
        )
        if failed:
            with pytest.raises(RuntimeError, match="batch job failed"):
                await adapter.batch(media.as_uri(), language="hi-IN", diarize=diarize)
        else:
            result = await adapter.batch(media.as_uri(), language="hi-IN", diarize=diarize)
            assert result[0].text == "नमस्ते"
            assert result[0].end_ms == duration * 1000
    assert len(init_keys) == 2 and init_keys[0] == init_keys[1]
    assert uploads == 2
    rate = 45 if diarize else 30
    assert sum(r["cost_inr"] for r in sink.records) == pytest.approx(rate * duration / 3600)


async def test_short_batch_uses_rest_with_observability(media: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/speech-to-text"
        assert b"saaras:v3" in request.content
        assert b"hi-IN" in request.content
        assert request.headers["Idempotency-Key"]
        return httpx.Response(
            200, json={"transcript": "नमस्ते", "language_code": "hi-IN", "request_id": "mock"}
        )

    sink = MemorySink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        adapter = SarvamSTT(
            client=AsyncSarvamAI(api_subscription_key="mock", httpx_client=http),
            runtime=AdapterRuntime("sarvam", "stt", sink=sink),
        )
        result = await adapter.batch(str(media), language="hi-IN")
    assert result[0].text == "नमस्ते"
    assert result[0].end_ms == 1000
    assert sum(r["cost_inr"] for r in sink.records) == pytest.approx(30 / 3600)


async def test_dubbing_http_submission_status_fetch(
    media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys: list[str] = []
    puts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal puts
        path = request.url.path
        if request.url.host == "storage.test":
            puts += 1
            assert request.headers["x-ms-blob-type"] == "BlockBlob"
            assert "api-subscription-key" not in request.headers
            return httpx.Response(429 if puts == 1 else 201)
        data: dict[str, Any]
        if path.endswith("/jobs"):
            keys.append(request.headers["Idempotency-Key"])
            payload = json.loads(request.content)
            assert payload["src_lang"] == "en-IN"
            assert payload["target_langs"] == ["hi-IN"]
            assert payload["voice_cloning"] is False
            if len(keys) == 1:
                return httpx.Response(503, json={"message": "busy"})
            data = {
                "job_id": "dub-1",
                "upload_url": "https://storage.test/video",
                "processing_started": False,
                "voice_cloning": False,
            }
        elif "export" in path:
            data = {"exports": []}
        else:
            data = {"job_id": "dub-1", "status": "completed", "progress": 100}
        return httpx.Response(200, json={"status": "success", "message": "ok", "data": data})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=transport, **kwargs)
    )
    async with original(transport=transport) as http:
        sink = MemorySink()
        adapter = SarvamDubbing(
            client=AsyncSarvamAI(api_subscription_key="mock", httpx_client=http),
            runtime=AdapterRuntime("sarvam", "dubbing", sink=sink, retry_base=0),
        )
        job = await adapter.submit(str(media), target_languages=["hi-IN"], voice_map={})
        assert job == "dub-1"
        assert (await adapter.status(job))["data"]["progress"] == 100
        assert (await adapter.fetch(job))["data"]["exports"] == []
    assert len(keys) == 2 and keys[0] == keys[1]
    assert puts == 2
    assert sum(r["cost_inr"] for r in sink.records) == pytest.approx(40 / 60)
