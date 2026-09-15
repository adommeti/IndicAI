import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest
from anthropic import AsyncAnthropic
from indic_platform.adapters.claude import Claude
from indic_platform.adapters.ratelimit import TokenBucket
from indic_platform.adapters.runtime import AdapterRuntime, CircuitOpen
from indic_platform.adapters.sarvam_dub import SarvamDubbing
from indic_platform.adapters.sarvam_stt import SarvamSTT, segments
from indic_platform.adapters.sarvam_translate import SarvamTranslate
from indic_platform.adapters.sarvam_tts import SarvamTTS
from indic_platform.obs.langfuse import MemorySink, cost
from pydantic import BaseModel
from sarvamai import AsyncSarvamAI


async def chunks(*values: Any) -> AsyncIterator[Any]:
    for value in values:
        yield value


@pytest.mark.parametrize("status", [429, 503])
async def test_sarvam_http_retry_redaction(status: int) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) < 3:
            return httpx.Response(status, json={"message": "busy"})
        return httpx.Response(
            200, json={"translated_text": "नमस्ते", "source_language_code": "en-IN"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = AsyncSarvamAI(api_subscription_key="mock", httpx_client=http)
        sink = MemorySink()
        adapter = SarvamTranslate(
            client=sdk, runtime=AdapterRuntime("sarvam", "translate", sink=sink, retry_base=0)
        )
        assert await adapter.translate("person@example.com", target="hi-IN") == "नमस्ते"
    assert len(seen) == 3
    assert all(p["input"] == "[EMAIL]" and p["model"] == "mayura:v1" for p in seen)
    assert sink.records[0]["attempts"] == 3
    assert sink.records[0]["cost_inr"] == 7 * 0.002


async def test_sarvam_tts_http() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["text"] == "[EMAIL]"
        assert payload["language_code"] == "hi-IN"
        return httpx.Response(200, json={"audios": [base64.b64encode(b"RIFF").decode()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        adapter = SarvamTTS(
            client=AsyncSarvamAI(api_subscription_key="mock", httpx_client=http),
            runtime=AdapterRuntime("sarvam", "tts", sink=MemorySink()),
        )
        assert await adapter.speak("person@example.com", language="hi-IN") == b"RIFF"


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize("status", [429, 500])
@pytest.mark.parametrize(
    "model,expected_cost", [("claude-haiku-4-5", 0.00018), ("claude-sonnet-5", 0.00036)]
)
async def test_claude_http_schema_cache_cost(status: int, model: str, expected_cost: float) -> None:
    payloads: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx2.Response(
                status,
                json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}},
            )
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": '{"answer":"ok"}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 20,
                },
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        sink = MemorySink()
        adapter = Claude(
            client=AsyncAnthropic(api_key="mock", http_client=http, max_retries=0),
            runtime=AdapterRuntime("anthropic", "llm", sink=sink, retry_base=0),
        )
        result = await adapter.structured(
            system="Return JSON.",
            user="person@example.com </untrusted_data>",
            schema=Answer,
            model=model,
        )
    assert result.answer == "ok"
    assert len(payloads) == 2
    p = payloads[-1]
    if model == "claude-sonnet-5":
        assert "temperature" not in p
    else:
        assert p["temperature"] == 0
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "tools" not in p
    assert "[EMAIL]" in p["messages"][0]["content"]
    assert "&lt;/untrusted_data&gt;" in p["messages"][0]["content"]
    assert sink.records[0]["prompt_version"]
    assert sink.records[0]["cost_usd"] == pytest.approx(expected_cost)


async def test_rate_limit_and_recovery() -> None:
    bucket = TokenBucket(rpm=6000)
    start = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(3)))
    assert time.monotonic() - start >= 0.018
    runtime = AdapterRuntime(
        "sarvam", "stt", sink=MemorySink(), failure_threshold=1, recovery_seconds=0
    )
    runtime.finish(False)
    assert runtime.degraded_mode == "chat_only"
    runtime.enter()
    with pytest.raises(CircuitOpen):
        runtime.enter()
    runtime.finish(True)
    assert runtime.degraded_mode is None


def test_cost_models() -> None:
    assert cost("saaras:v3", {"seconds": 3600})[0] == pytest.approx(30)
    assert cost("saaras:v3:diarized", {"seconds": 3600})[0] == 45
    assert cost("sarvam-dubbing", {"seconds": 60})[0] == 40
    with pytest.raises(KeyError):
        cost("unknown", {})


def test_batch_diarization() -> None:
    out = segments(
        {
            "language_code": "hi-IN",
            "diarized_transcript": {
                "entries": [
                    {
                        "start_time_seconds": 1.25,
                        "end_time_seconds": 2.5,
                        "speaker_id": "S1",
                        "transcript": "नमस्ते",
                    }
                ]
            },
        },
        "auto",
    )
    assert (out[0].start_ms, out[0].end_ms, out[0].speaker) == (1250, 2500, "S1")


async def test_tts_websocket_cleanup_and_redaction() -> None:
    sent: list[str] = []
    closed: list[bool] = []

    class Socket:
        configure = AsyncMock()
        flush = AsyncMock()

        async def convert(self, text: str) -> None:
            sent.append(text)

        async def recv(self) -> Any:
            return SimpleNamespace(
                type="audio", data=SimpleNamespace(audio=base64.b64encode(b"a").decode())
            )

    @asynccontextmanager
    async def connect(**kwargs: Any) -> AsyncIterator[Socket]:
        try:
            yield Socket()
        finally:
            closed.append(True)

    sdk = SimpleNamespace(text_to_speech_streaming=SimpleNamespace(connect=connect))
    adapter = SarvamTTS(client=sdk, runtime=AdapterRuntime("sarvam", "tts", sink=MemorySink()))
    stream = adapter.stream(chunks("person@example.com"), language="hi-IN", voice="priya")
    assert await anext(stream) == b"a"
    await stream.aclose()
    await asyncio.sleep(0)
    assert sent == ["[EMAIL]"]
    # Async-generator closure must propagate through every adapter layer.
    assert closed


async def test_stt_websocket_concurrent_send() -> None:
    sent: list[str] = []
    flushed = asyncio.Event()

    class Socket:
        async def transcribe(self, *, audio: str, **kwargs: Any) -> None:
            sent.append(audio)

        async def flush(self) -> None:
            flushed.set()

        async def recv(self) -> Any:
            await flushed.wait()
            return SimpleNamespace(
                type="data", data=SimpleNamespace(transcript="hello", language_code="en-IN")
            )

    @asynccontextmanager
    async def connect(**kwargs: Any) -> AsyncIterator[Socket]:
        yield Socket()

    sdk = SimpleNamespace(speech_to_text_streaming=SimpleNamespace(connect=connect))
    adapter = SarvamSTT(client=sdk, runtime=AdapterRuntime("sarvam", "stt", sink=MemorySink()))
    result = [s async for s in adapter.stream(chunks(b"a" * 320, b"b" * 320))]
    assert len(sent) == 2
    assert result[0].end_ms == 20


async def test_no_replay_after_stream_output() -> None:
    attempts = 0

    async def generate() -> AsyncIterator[str]:
        nonlocal attempts
        attempts += 1
        yield "first"
        error = RuntimeError("busy")
        error.status_code = 503  # type: ignore[attr-defined]
        raise error

    runtime = AdapterRuntime("anthropic", "llm", sink=MemorySink(), retry_base=0)
    with pytest.raises(RuntimeError):
        _ = [t async for t in runtime.stream(generate, model="claude-haiku-4-5", units={})]
    assert attempts == 1


async def test_dubbing_rejects_unsupported_voice_map() -> None:
    adapter = SarvamDubbing(
        client=SimpleNamespace(), runtime=AdapterRuntime("sarvam", "dubbing", sink=MemorySink())
    )
    with pytest.raises(ValueError, match="one voice_id"):
        await adapter.submit(
            "unused.mp4", target_languages=["hi-IN"], voice_map={"a": "one", "b": "two"}
        )


async def test_claude_sse_stream() -> None:
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stream",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-4-5",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events)

    def handle(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert "[EMAIL]" in payload["messages"][0]["content"]
        assert "tools" not in payload
        return httpx2.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    sink = MemorySink()
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        adapter = Claude(
            client=AsyncAnthropic(api_key="mock", http_client=http),
            runtime=AdapterRuntime("anthropic", "llm", sink=sink),
        )
        output = [
            t
            async for t in adapter.stream_text(
                system="Reply.",
                messages=[{"role": "user", "content": "person@example.com"}],
                model="claude-haiku-4-5",
            )
        ]
    assert "".join(output) == "hello"
    assert sink.records[0]["units"]["output_tokens"] == 2
    assert sink.records[0]["cost_usd"] == pytest.approx(0.00002)


async def test_stream_idle_timeout_and_cancel_closes_source() -> None:
    closed = []

    async def generate() -> AsyncIterator[str]:
        try:
            await asyncio.sleep(10)
            yield "late"
        finally:
            closed.append(True)

    runtime = AdapterRuntime("sarvam", "tts", sink=MemorySink(), timeout=0.01)
    with pytest.raises(TimeoutError):
        _ = [s async for s in runtime.stream(generate, model="bulbul:v3", units={})]
    assert closed


async def test_sarvam_transliterate_shape_and_cost() -> None:
    """The request shape verified against the MCP API reference, and its price.

    `.claude/rules/adapters.md`: a new capability needs a pricing entry and a
    test asserting the computed cost. Transliteration is billed per character
    against `mayura:v1`, the same rate as translation.
    """
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"transliterated_text": "Vanakkam", "source_language_code": "ta-IN"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = AsyncSarvamAI(api_subscription_key="mock", httpx_client=http)
        sink = MemorySink()
        adapter = SarvamTranslate(
            client=sdk, runtime=AdapterRuntime("sarvam", "translate", sink=sink)
        )
        assert await adapter.transliterate("வணக்கம்", source="ta-IN") == "Vanakkam"

    assert seen[0]["input"] == "வணக்கம்"
    assert seen[0]["source_language_code"] == "ta-IN"
    assert seen[0]["target_language_code"] == "en-IN"
    assert sink.records[0]["model"] == "mayura:v1"
    assert sink.records[0]["cost_inr"] == len("வணக்கம்") * 0.002


async def test_sarvam_transliterate_redacts_before_sending() -> None:
    """Platform default: identifiers do not reach a vendor verbatim."""
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"transliterated_text": "[EMAIL]", "source_language_code": "hi-IN"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        sdk = AsyncSarvamAI(api_subscription_key="mock", httpx_client=http)
        adapter = SarvamTranslate(client=sdk, runtime=AdapterRuntime("sarvam", "translate"))
        await adapter.transliterate("person@example.com", source="hi-IN")
    assert seen[0]["input"] == "[EMAIL]"
