"""The response cap, and what a truncated structured reply must look like.

`Claude.structured` caps every response with `max_tokens`. A caller whose schema
is a single verdict is fine at the 1024 default; a caller whose schema is a LIST
sized by its input is not, and the way it fails is the reason this file exists:
the reply is truncated, `stop_reason` comes back `max_tokens`, `parsed_output` is
None, and the error the caller saw used to read "Claude refused or returned
incomplete structured output" -- which sends the reader hunting for a content
problem that is not there.

Measured on a live call before this was fixed: `training_localizer.stages.adapt`
sent one 18-segment module and the reply stopped dead at exactly 1024 output
tokens. With the cap raised it completed in 2,725 (`stop_reason: end_turn`).
"""

import json
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from indic_platform.adapters.claude import Claude
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.obs.langfuse import MemorySink
from pydantic import BaseModel


class Answer(BaseModel):
    answer: str


def message(stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": '{"answer":"ok"}'}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }


async def call(stop_reason: str = "end_turn", **kwargs: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Drive `structured` against a mock transport and return (result, request bodies)."""
    sent: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=message(stop_reason))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        adapter = Claude(
            client=AsyncAnthropic(api_key="mock", http_client=http, max_retries=0),
            runtime=AdapterRuntime("anthropic", "llm", sink=MemorySink(), retry_base=0),
        )
        result = await adapter.structured(
            system="Return JSON.", user="hello", schema=Answer, model="claude-haiku-4-5", **kwargs
        )
    return result, sent


async def test_the_default_cap_is_unchanged_for_callers_that_do_not_ask() -> None:
    """Every pre-existing caller keeps 1024; widening the signature must not move it."""
    _, sent = await call()
    assert sent[0]["max_tokens"] == 1024


async def test_a_caller_can_size_the_cap_for_a_batched_schema() -> None:
    """`adapt` returns one object per segment, so its ceiling is its own business."""
    _, sent = await call(max_tokens=8192)
    assert sent[0]["max_tokens"] == 8192, "the caller's cap must reach the wire"


async def test_a_truncated_reply_is_reported_as_truncation_not_refusal() -> None:
    """The regression: a `max_tokens` stop must name the cap and how to fix it.

    Reported as a refusal, this costs the reader a hunt through prompt text and
    content policy for a problem that is really one integer.
    """
    with pytest.raises(ValueError) as caught:
        await call(stop_reason="max_tokens", max_tokens=1024)
    text = str(caught.value)
    assert "max_tokens=1024" in text, "say which cap was hit"
    assert "truncated" in text, "say what happened"
    assert "refus" not in text.lower(), "a truncation is not a refusal"


async def test_a_real_refusal_still_names_its_own_stop_reason() -> None:
    with pytest.raises(ValueError) as caught:
        await call(stop_reason="refusal")
    text = str(caught.value)
    assert "refusal" in text
    assert "truncated" not in text, "only a max_tokens stop is a truncation"


async def test_the_model_keyed_timeout_is_the_default() -> None:
    """Unchanged for every caller that does not ask."""
    _, sent = await call()
    assert sent[0]["max_tokens"] == 1024  # the pair travel together


async def test_a_caller_can_extend_the_timeout_for_a_long_generation() -> None:
    """A raised cap costs time, and `Claude.timeout` is keyed on the model alone.

    It cannot know this call asked for thousands of tokens, so a caller that
    raises `max_tokens` must be able to raise the clock too. `stages.adapt`
    measured 47.8s for a Telugu module against the 60s model default -- close
    enough that it failed intermittently on exactly the Telugu modules.
    """
    import anthropic

    slow: list[float | None] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        slow.append(request.extensions.get("timeout", {}).get("read"))
        return httpx2.Response(200, json=message())

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        adapter = Claude(
            client=anthropic.AsyncAnthropic(api_key="mock", http_client=http, max_retries=0),
            runtime=AdapterRuntime("anthropic", "llm", sink=MemorySink(), retry_base=0),
        )
        await adapter.structured(
            system="Return JSON.",
            user="hello",
            schema=Answer,
            model="claude-sonnet-5",
            timeout_s=180.0,
        )
    assert slow and slow[0] == 180.0, "the caller's clock must reach the transport"
