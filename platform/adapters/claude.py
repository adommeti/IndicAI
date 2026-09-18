import contextlib
import hashlib
from collections.abc import AsyncIterator, Callable
from typing import Any

from anthropic import AsyncAnthropic
from indic_platform.adapters.base import T
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.config.settings import anthropic_api_key
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
        # The key is resolved rather than left to the SDK's implicit `ANTHROPIC_API_KEY`
        # lookup: inside a Claude Code session that variable belongs to the agent
        # harness and is stripped, so the SDK would build a client with no credential
        # and every live eval would fail at the vendor rather than at configuration.
        # `anthropic_api_key` prefers the project-scoped name and falls back to the
        # standard one, so CI and local development are unchanged.
        # None is passed through untouched: the SDK's own "no key" error is clearer
        # than anything invented here, and a caller supplying `client` bypasses this.
        self.client = client or AsyncAnthropic(api_key=anthropic_api_key(), max_retries=0)
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
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        model: str,
        cache_system: bool = True,
        max_tokens: int = 1024,
        timeout_s: float | None = None,
    ) -> T:
        """`max_tokens` caps the response, so a caller whose schema is a LIST must
        size it for the whole list. 1024 suits a single verdict or decision and is
        the default every existing caller keeps; it is far too small for a batched
        schema, and the failure is silent-looking -- the response is truncated,
        `stop_reason` comes back `max_tokens`, `parsed_output` is None, and what
        surfaces reads like a refusal. `training_localizer.stages.adapt` measured
        2,725 output tokens for one 18-segment module against this 1024 cap.

        Raising the cap does not by itself cost more: billing is for tokens
        generated, not for the ceiling. It does cost TIME, which is why `timeout_s`
        is a sibling argument: `self.timeout(model)` is keyed on the model alone,
        so it cannot know that this particular call asked for thousands of tokens.
        Measured on `stages.adapt` at its raised cap: 22.5s for a Tamil module,
        25.3s for Hindi, 47.8s for Telugu -- against a 60s model default. A caller
        that raises `max_tokens` should consider raising this too.
        """
        if not cache_system:
            raise ValueError("Stable system prompts must use caching")
        clean_system = self.redact(system)
        version = hashlib.sha256(clean_system.encode()).hexdigest()[:16]
        units: dict[str, float] = {}
        budget = self.timeout(model) if timeout_s is None else timeout_s

        async def generate() -> Any:
            result = await self.client.with_options(max_retries=0).messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=[
                    {"type": "text", "text": clean_system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": self.wrap(self.redact(user))}],
                output_format=schema,
                # Sonnet 5 rejects legacy sampling controls (SDK MIGRATION.md).
                # Keep zero on older models; do not claim determinism for Sonnet 5.
                extra_body={} if model == "claude-sonnet-5" else {"temperature": 0},
                timeout=budget,
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
            generate, model=model, units=units, timeout=budget, prompt_version=version
        )
        if result.stop_reason != "end_turn" or result.parsed_output is None:
            # Name the reason. "refused" and "truncated" need different fixes, and
            # reporting a truncation as a refusal sends the reader looking for a
            # content problem that is not there.
            detail = (
                f"hit max_tokens={max_tokens} and was truncated; raise max_tokens "
                f"for this call or send a smaller batch"
                if result.stop_reason == "max_tokens"
                else f"stop_reason={result.stop_reason!r}"
            )
            raise ValueError(f"Claude returned no usable structured output: {detail}")
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
                {"role": message["role"], "content": self.wrap(self.redact(message["content"]))}
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
