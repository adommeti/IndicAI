"""Drive the pipeline stages from the eval runner, without a database.

`run_uc2.py --translate training_localizer.eval_hook:<name>` scores the real
stages against the golden set. The Celery tasks in `pipeline.py` need Postgres
and a broker; the stage functions in `stages.py` do not, so the eval path calls
them directly and the numbers still describe the shipped code.

Two entry points, named for exactly what they run, because the difference shows
up in the metrics and a report must not blur it:

  `full`                 adapt (Claude) -> translate (Mayura) -> post_edit
                         (Claude + enforcement). Needs ANTHROPIC_API_KEY and
                         SARVAM_API_KEY.
  `translate_and_enforce` translate (Mayura) -> post_edit (enforcement only).
                         Needs SARVAM_API_KEY. No adapt, so nothing compresses
                         and timing-fit is expected to fail; terminology
                         adherence is still a real measurement of the shipped
                         enforcer.

Both are synchronous because the runner's Translator contract is, and both
memoise per (seg_id, language) so a language's adapt pass runs once.
"""

import asyncio
import os
from typing import Any, Protocol

from training_localizer import stages
from training_localizer.stages import SourceSegment
from training_localizer.terminology import load_glossary, resolve_locked_id


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
    """One pipeline run over the golden set, cached per (module, seg, language)."""

    def __init__(self, *, use_claude: bool) -> None:
        self.use_claude = use_claude
        self.glossary = load_glossary()
        self._adapted: dict[tuple[str, str], dict[int, str]] = {}
        self._cache: dict[tuple[str, int, str], str] = {}
        self.change_log: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {"adapt": {}, "post_edit_model": use_claude}

    def __call__(self, segment: RunnerSegment, language: str) -> str:
        key = (segment.module_id, segment.seg_id, language)
        if key not in self._cache:
            self._cache[key] = asyncio.run(self._localize(segment, language))
        return self._cache[key]

    async def _adapt_module(self, segment: RunnerSegment, language: str) -> dict[int, str]:
        """adapt runs per module, not per segment: it sees the whole script."""
        from indic_platform.eval.runners.run_uc2 import load_segments

        key = (segment.module_id, language)
        if key in self._adapted:
            return self._adapted[key]
        module = [
            _source(s)  # type: ignore[arg-type]
            for s in load_segments()
            if s.module_id == segment.module_id
        ]
        from indic_platform.adapters.claude import Claude

        claude = Claude()
        try:
            adapted, meta = await stages.adapt(module, language, structured=claude.structured)
        finally:
            await claude.client.close()
        self.meta["adapt"][f"{segment.module_id}:{language}"] = meta
        self._adapted[key] = {seg_id: item.text for seg_id, item in adapted.items()}
        return self._adapted[key]

    async def _localize(self, segment: RunnerSegment, language: str) -> str:
        from indic_platform.adapters.sarvam_translate import SarvamTranslate

        locked_id = segment.locked_id or (
            resolve_locked_id(segment.source_text, self.glossary) if segment.locked else None
        )
        english = segment.source_text
        if self.use_claude:
            english = (await self._adapt_module(segment, language)).get(
                segment.seg_id, segment.source_text
            )

        locked_text = (
            str(self.glossary.statements[locked_id][language]) if locked_id is not None else None
        )
        if locked_text is not None:
            translated = locked_text
        else:
            mayura = SarvamTranslate()
            try:
                translated = await stages.translate(
                    english,
                    language,
                    translator=lambda t, lang: mayura.translate(t, target=lang, mode="formal"),
                )
            finally:
                await mayura.close()

        structured = None
        if self.use_claude:
            from indic_platform.adapters.claude import Claude

            claude = Claude()
            try:
                text, changes, _ = await stages.post_edit(
                    source_text=english,
                    translated=translated,
                    language=language,
                    glossary=self.glossary,
                    structured=claude.structured,
                    locked_id=locked_id,
                )
            finally:
                await claude.client.close()
        else:
            text, changes, _ = await stages.post_edit(
                source_text=english,
                translated=translated,
                language=language,
                glossary=self.glossary,
                structured=structured,
                locked_id=locked_id,
            )
        self.change_log += [
            {"module_id": segment.module_id, "seg_id": segment.seg_id, "language": language}
            | change.as_json()
            for change in changes
        ]
        return text


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
    return (_full or _enforce_only or Localizer(use_claude=False)).change_log
