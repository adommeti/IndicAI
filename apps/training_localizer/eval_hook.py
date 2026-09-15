"""Drive the pipeline stages from the eval runner, without a database.

`run_uc2.py --translate training_localizer.eval_hook:<name>` scores the real
stages against the golden set. The Celery tasks in `pipeline.py` need Postgres
and a broker; the stage functions in `stages.py` do not, so the eval path calls
them directly and the numbers still describe the shipped code.

Two entry points, named for exactly what they run, because the difference shows
up in the metrics and a report must not blur it:

  `full`                  adapt (Claude) -> translate (Mayura) -> post_edit
                          (Claude + enforcement). Needs ANTHROPIC_API_KEY and
                          SARVAM_API_KEY.
  `translate_and_enforce` translate (Mayura) -> post_edit (enforcement only).
                          Needs SARVAM_API_KEY. No adapt, so nothing compresses
                          and timing-fit is expected to fail; terminology
                          adherence is still a real measurement of the shipped
                          enforcer.

The runner's Translator contract is synchronous, so a whole (module, language)
is localized inside one event loop on the first call for it and served from
cache after that. Doing it per segment would open a vendor client per segment
and re-run adapt, which is a per-module call, once for every segment in it.
"""

import asyncio
import os
from typing import Any, Protocol

from training_localizer import stages
from training_localizer.stages import SourceSegment
from training_localizer.terminology import load_glossary, resolve_locked_id

# Segments per module translated at once. Sequential by default: Sarvam's
# account rate limit rejected a concurrency of 4 with 429s that outlasted the
# adapter runtime's three retries, and a golden run that dies two thirds of the
# way through has cost money and measured nothing. Raise it with
# UC2_EVAL_CONCURRENCY only against an account whose limit you know.
CONCURRENCY = int(os.environ.get("UC2_EVAL_CONCURRENCY", "1"))


class RunnerSegment(Protocol):
    """The eval runner's Segment, structurally."""

    module_id: str
    seg_id: int
    start_ms: int
    end_ms: int
    source_text: str
    locked: bool
    locked_id: str | None


def _source(segment: RunnerSegment) -> SourceSegment:
    return SourceSegment(
        seg_id=segment.seg_id,
        start_ms=segment.start_ms,
        end_ms=segment.end_ms,
        source_text=segment.source_text,
        locked=segment.locked,
        locked_id=segment.locked_id,
    )


class Localizer:
    """One pipeline run over the golden set, batched per (module, language)."""

    def __init__(self, *, use_claude: bool) -> None:
        self.use_claude = use_claude
        self.glossary = load_glossary()
        self._done: dict[tuple[str, str], dict[int, str]] = {}
        self.change_log: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {"adapt": {}, "post_edit_model": use_claude}

    def __call__(self, segment: RunnerSegment, language: str) -> str:
        key = (segment.module_id, language)
        if key not in self._done:
            self._done[key] = asyncio.run(self._module(segment.module_id, language))
        return self._done[key][segment.seg_id]

    def _segments(self, module_id: str) -> list[SourceSegment]:
        from indic_platform.eval.runners.run_uc2 import load_segments

        out: list[SourceSegment] = []
        for s in load_segments():
            if s.module_id != module_id:
                continue
            source = _source(s)  # type: ignore[arg-type]
            if source.locked and source.locked_id is None:
                source = SourceSegment(
                    seg_id=source.seg_id,
                    start_ms=source.start_ms,
                    end_ms=source.end_ms,
                    source_text=source.source_text,
                    locked=True,
                    locked_id=resolve_locked_id(source.source_text, self.glossary),
                )
            out.append(source)
        return out

    async def _module(self, module_id: str, language: str) -> dict[int, str]:
        from indic_platform.adapters.sarvam_translate import SarvamTranslate

        segments = self._segments(module_id)
        english = {s.seg_id: s.source_text for s in segments}
        claude = None
        try:
            if self.use_claude:
                from indic_platform.adapters.claude import Claude

                claude = Claude()
                adapted, meta = await stages.adapt(segments, language, structured=claude.structured)
                self.meta["adapt"][f"{module_id}:{language}"] = meta
                english = {seg_id: item.text for seg_id, item in adapted.items()}

            mayura = SarvamTranslate()
            limit = asyncio.Semaphore(CONCURRENCY)

            async def one(segment: SourceSegment) -> tuple[int, str]:
                locked_text = (
                    str(self.glossary.statements[segment.locked_id][language])
                    if segment.locked_id is not None
                    else None
                )
                async with limit:
                    translated = await stages.translate(
                        english[segment.seg_id],
                        language,
                        translator=lambda t, lang: mayura.translate(t, target=lang, mode="formal"),
                        locked_text=locked_text,
                    )
                text, changes, _ = await stages.post_edit(
                    source_text=english[segment.seg_id],
                    translated=translated,
                    language=language,
                    glossary=self.glossary,
                    structured=claude.structured if claude else None,
                    locked_id=segment.locked_id,
                )
                self.change_log += [
                    {
                        "module_id": module_id,
                        "seg_id": segment.seg_id,
                        "language": language,
                        **change.as_json(),
                    }
                    for change in changes
                ]
                return segment.seg_id, text

            results = await asyncio.gather(*(one(s) for s in segments))
        finally:
            if claude is not None:
                await claude.client.close()
        return dict(results)


_full: Localizer | None = None
_enforce_only: Localizer | None = None


def full(segment: RunnerSegment, language: str) -> str:
    """adapt -> translate -> post_edit, every stage as shipped."""
    global _full
    if _full is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "eval_hook:full needs ANTHROPIC_API_KEY for adapt and post_edit. "
                "Use eval_hook:translate_and_enforce to measure the Sarvam-only path."
            )
        _full = Localizer(use_claude=True)
    return _full(segment, language)


def translate_and_enforce(segment: RunnerSegment, language: str) -> str:
    """translate -> deterministic post_edit. No adapt, so no compression."""
    global _enforce_only
    if _enforce_only is None:
        _enforce_only = Localizer(use_claude=False)
    return _enforce_only(segment, language)


def change_log() -> list[dict[str, Any]]:
    """Every post_edit change from the run, for the report."""
    active = _full or _enforce_only
    return active.change_log if active else []
