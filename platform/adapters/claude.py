import contextlib
import hashlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from anthropic import AsyncAnthropic
from indic_platform.adapters.base import T
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.security.harden import validate_or_reject, wrap_untrusted
from indic_platform.security.redact import redact


class Claude:
    def __init__(
        self,
        *,
        client: AsyncAnthropic | None = None,
        runtime: AdapterRuntime | None = None,
        redactor: Callable[[str], str] = redact,
        wrapper: Callable[[str], str] = wrap_untrusted,
    ) -> None:
        self.client = client or AsyncAnthropic(max_retries=0)
        self.runtime = runtime or AdapterRuntime("anthropic", "llm", timeout=60)
        # Injectable because PRD E9 requires redaction *off* for UC3
        # transcripts: an off-channel-comms finding can hinge on the phone
        # number itself, and `[PHONE]` in the evidence makes the flag
        # unreviewable. An app that needs this documents it in its README and
        # passes a policy object -- it does not monkeypatch
        # (`.claude/rules/adapters.md`). The default stays full redaction.
        self.redact = redactor
        # Likewise the delimiter: E6's system prompt names <transcript>.
        self.wrap = wrapper

    @staticmethod
    def timeout(model: str) -> float:
        return 20 if "haiku" in model else 60

    async def structured(
        self, *, system: str, user: str, schema: type[T], model: str, cache_system: bool = True
    ) -> T:
        if not cache_system:
            raise ValueError("Stable system prompts must use caching")
        clean_system = self.redact(system)
        version = hashlib.sha256(clean_system.encode()).hexdigest()[:16]
        units: dict[str, float] = {}

        async def generate() -> Any:
            result = await self.client.with_options(max_retries=0).messages.parse(
                model=model,
                max_tokens=1024,
                system=[
                    {"type": "text", "text": clean_system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": self.wrap(self.redact(user))}],
                output_format=schema,
                # Sonnet 5 rejects legacy sampling controls (SDK MIGRATION.md).
                # Keep zero on older models; do not claim determinism for Sonnet 5.
                extra_body={} if model == "claude-sonnet-5" else {"temperature": 0},
                timeout=self.timeout(model),
            )
            units.update(
                {
                    k: float(getattr(result.usage, k, 0) or 0)
                    for k in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                }
            )
            return result

        result = await self.runtime.call(
            generate, model=model, units=units, timeout=self.timeout(model), prompt_version=version
        )
        if result.stop_reason != "end_turn" or result.parsed_output is None:
            raise ValueError("Claude refused or returned incomplete structured output")
        return validate_or_reject(result.parsed_output.model_dump_json(), schema)

    async def stream_text(
        self, *, system: str, messages: list[dict[str, Any]], model: str
    ) -> AsyncIterator[str]:
        clean_system = self.redact(system)
        version = hashlib.sha256(clean_system.encode()).hexdigest()[:16]
        safe: list[Any] = []
        for message in messages:
            if message.get("role") not in {"user", "assistant"} or not isinstance(
                message.get("content"), str
            ):
                raise ValueError("Only user/assistant text messages are supported")
            safe.append(
                {"role": message["role"], "content": wrap_untrusted(redact(message["content"]))}
            )
        units: dict[str, float] = {}

        async def generate() -> AsyncIterator[str]:
            async with self.client.with_options(max_retries=0).messages.stream(
                model=model,
                max_tokens=1024,
                system=[
                    {"type": "text", "text": clean_system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=safe,
                timeout=self.timeout(model),
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                result = await stream.get_final_message()
                units.update(
                    {
                        k: float(getattr(result.usage, k, 0) or 0)
                        for k in (
                            "input_tokens",
                            "output_tokens",
                            "cache_read_input_tokens",
                            "cache_creation_input_tokens",
                        )
                    }
                )

        async with contextlib.aclosing(
            self.runtime.stream(generate, model=model, units=units, prompt_version=version)
        ) as stream:
            async for text in stream:
                yield text
