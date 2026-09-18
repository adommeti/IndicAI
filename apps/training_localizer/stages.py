"""The five pipeline stages as pure, vendor-injectable functions.

`pipeline.py` wraps these in Celery tasks that read and write Postgres. Keeping
the logic here means every stage has a unit test with mocked vendors and no
broker, and the eval runner can drive the stages directly against the golden set
before a module has ever been uploaded.

Stage order is PRD D5: adapt -> translate -> post_edit -> backtranslate_qa -> quiz.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml
from indic_platform.adapters.base import T
from indic_platform.security.harden import wrap_untrusted
from pydantic import BaseModel, Field

from training_localizer.terminology import Change, Glossary, enforce

PROMPTS = Path(__file__).parent / "prompts"
TIMING = Path(__file__).parents[2] / "platform" / "config" / "timing.yaml"

ADAPT_MODEL = "claude-sonnet-5"
POST_EDIT_MODEL = "claude-sonnet-5"
JUDGE_MODEL = "claude-haiku-4-5"
QUIZ_MODEL = "claude-sonnet-5"

# `adapt` is the one stage whose schema is a list the size of a whole module: it
# returns adapted text AND a rationale for every non-locked segment, so the
# response scales with the module while the adapter's default cap does not. One
# 18-segment module measured 2,725 output tokens against the 1024 default, which
# truncated it -- `stop_reason` came back `max_tokens` and the adapter surfaced
# what looked like a refusal. This is a ceiling, not a spend: billing is for
# tokens generated. Sized at roughly 3x the measured need so a longer module does
# not rediscover the same wall.
ADAPT_MAX_TOKENS = 8192

# `post_edit` is per-segment, so this one is not about batching: it is about the
# script. Measured on SHORT segments against the 1024 default: hi-IN 345-604
# output tokens, te-IN 986, ta-IN 957 -- Telugu and Tamil land within 4% of the
# cap before the segment is even long, because Indic scripts tokenize far more
# heavily than Latin and the reply carries the rewritten text AND a change list.
# The 1024 default was sized for Latin output and is not safe for any of the
# three pilot languages.
POST_EDIT_MAX_TOKENS = 4096

# D10: the dub must land within +/-15% of the source segment duration.
TOLERANCE = 0.15


class Structured(Protocol):
    """`Claude.structured`, narrowed to what the stages use."""

    def __call__(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        model: str,
        cache_system: bool = True,
        max_tokens: int = 1024,
    ) -> Awaitable[T]: ...


Translator = Callable[[str, str], Awaitable[str]]


# --- prompt loading -----------------------------------------------------------


def prompt_body(name: str) -> str:
    """The D7 prompt text, without its YAML front-matter."""
    text = (PROMPTS / f"{name}.md").read_text()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text.strip()


def prompt_version(name: str) -> str:
    """Content-derived, per .claude/rules/prompts.md: never bumped by hand."""
    import hashlib

    return hashlib.sha256(prompt_body(name).encode()).hexdigest()[:16]


def load_timing() -> dict[str, Any]:
    return dict(yaml.safe_load(TIMING.read_text()))


# --- schemas ------------------------------------------------------------------


class AdaptedSegment(BaseModel):
    seg_id: int
    text: str
    rationale: str = ""


class AdaptedScript(BaseModel):
    segments: list[AdaptedSegment]


class PostEditChange(BaseModel):
    # The PRD's key is `from`, which is a Python keyword.
    from_: str = Field(alias="from", default="")
    to: str = ""
    reason: str = ""


class PostEdit(BaseModel):
    text: str
    changes: list[PostEditChange] = Field(default_factory=list)


class Backtranslation(BaseModel):
    english: str


class FidelityVerdict(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str = ""
    lost_or_changed: list[str] = Field(default_factory=list)


class QuizDraftItem(BaseModel):
    seg_id: int
    question: str
    options: list[str]
    answer: int
    rationale: str


class QuizDraft(BaseModel):
    items: list[QuizDraftItem]


@dataclass(frozen=True)
class SourceSegment:
    seg_id: int
    start_ms: int
    end_ms: int
    source_text: str
    locked: bool = False
    locked_id: str | None = None

    @property
    def duration_s(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0


# --- 1. adapt -----------------------------------------------------------------


def word_budget(segment: SourceSegment, language: str, timing: dict[str, Any]) -> int:
    """How many English words fit this segment once rendered into `language`.

    adapt rewrites English but what must fit is the translation, so the budget
    is an English word count scaled by the measured per-language speech rate in
    platform/config/timing.yaml.
    """
    rate = float(timing["languages"][language]["words_per_second"])
    return max(1, int(segment.duration_s * rate * (1 + TOLERANCE)))


def over_budget(text: str, segment: SourceSegment, language: str, timing: dict[str, Any]) -> bool:
    return len(text.split()) > word_budget(segment, language, timing)


async def adapt(
    segments: list[SourceSegment],
    language: str,
    *,
    structured: Structured,
    timing: dict[str, Any] | None = None,
    model: str = ADAPT_MODEL,
) -> tuple[dict[int, AdaptedSegment], dict[str, Any]]:
    """Adapt the English script, then hold it to the timing budget.

    The PRD's prompt asks the model to respect the budget; this enforces it. Any
    segment still over budget after the first pass is re-sent ONCE with a
    shorten hint naming the exact word count, per the prompt's instruction to
    retry once. Segments that are still long after that retry are returned as
    they are and named in `meta["over_budget"]` -- the pipeline reports the
    overrun rather than truncating a compliance obligation mid-sentence.

    LOCKED segments never go to the model and are never shortened: their text is
    a legal statement and the approved rendering is fixed.
    """
    timing = timing or load_timing()
    system = prompt_body("adapt")
    adaptable = [s for s in segments if not s.locked]
    result: dict[int, AdaptedSegment] = {
        s.seg_id: AdaptedSegment(seg_id=s.seg_id, text=s.source_text, rationale="locked: verbatim")
        for s in segments
        if s.locked
    }
    meta: dict[str, Any] = {"retried": [], "over_budget": [], "model": model}
    if not adaptable:
        return result, meta

    by_id = {s.seg_id: s for s in adaptable}
    payload = [
        {
            "seg_id": s.seg_id,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "locked": False,
            "text": s.source_text,
            "word_budget": word_budget(s, language, timing),
        }
        for s in adaptable
    ]
    draft = await structured(
        system=system,
        user=wrap_untrusted(json.dumps({"language": language, "segments": payload})),
        schema=AdaptedScript,
        model=model,
        max_tokens=ADAPT_MAX_TOKENS,
    )
    for item in draft.segments:
        if item.seg_id in by_id:
            result[item.seg_id] = item

    # One shorten retry, for exactly the segments that are still too long.
    too_long = [
        by_id[seg_id]
        for seg_id, item in result.items()
        if seg_id in by_id and over_budget(item.text, by_id[seg_id], language, timing)
    ]
    if too_long:
        meta["retried"] = sorted(s.seg_id for s in too_long)
        retry_payload = [
            {
                "seg_id": s.seg_id,
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "locked": False,
                "text": result[s.seg_id].text,
                "word_budget": word_budget(s, language, timing),
                "hint": (
                    f"shorten: this segment must fit {word_budget(s, language, timing)} "
                    f"English words once rendered into {language}; it is currently "
                    f"{len(result[s.seg_id].text.split())}. Shorten wording, not meaning."
                ),
            }
            for s in too_long
        ]
        shortened = await structured(
            system=system,
            user=wrap_untrusted(json.dumps({"language": language, "segments": retry_payload})),
            schema=AdaptedScript,
            model=model,
            max_tokens=ADAPT_MAX_TOKENS,
        )
        for item in shortened.segments:
            if item.seg_id in by_id:
                result[item.seg_id] = item

    meta["over_budget"] = sorted(
        seg_id
        for seg_id, item in result.items()
        if seg_id in by_id and over_budget(item.text, by_id[seg_id], language, timing)
    )
    missing = sorted(set(by_id) - set(result))
    if missing:
        raise ValueError(f"adapt dropped segments: {missing}")
    return result, meta


# --- 2. translate -------------------------------------------------------------


async def translate(
    text: str, language: str, *, translator: Translator, locked_text: str | None = None
) -> str:
    """Mayura, formal mode. A LOCKED segment skips the vendor entirely.

    There is nothing for a translator to decide about a locked statement: the
    approved rendering is the answer, and paying to translate it would only
    create something for post_edit to overwrite.
    """
    if locked_text is not None:
        return locked_text
    return await translator(text, language)


# --- 3. post_edit -------------------------------------------------------------


async def post_edit(
    *,
    source_text: str,
    translated: str,
    language: str,
    glossary: Glossary,
    structured: Structured | None = None,
    locked_id: str | None = None,
    model: str = POST_EDIT_MODEL,
) -> tuple[str, list[Change], dict[str, Any]]:
    """Claude proposes, the enforcer disposes.

    PRD D7 puts the 100% terminology gate here. A model cannot guarantee a hard
    gate, so the Claude leg does the part that needs judgement -- re-wording so
    an approved term reads naturally in context -- and `terminology.enforce`
    then makes the result satisfy the glossary unconditionally. When Claude is
    unavailable (`structured=None`) the enforcer runs alone and the gate still
    holds; the change log says which edits came from where.
    """
    meta: dict[str, Any] = {
        "glossary_version": glossary.version,
        "model": model if structured else None,
        "prompt_version": prompt_version("post_edit"),
        "model_available": structured is not None,
    }
    changes: list[Change] = []
    text = translated

    if structured is not None:
        payload = {
            "language": language,
            "english_source": source_text,
            "machine_translation": translated,
            "glossary": {
                "keep_english": list(glossary.keep_english),
                "approved_renderings": [
                    {"term": e["term"], "target": e[language]} for e in glossary.renderings
                ],
            },
            "locked_approved_rendering": (
                str(glossary.statements[locked_id][language]) if locked_id else None
            ),
        }
        edited = await structured(
            system=prompt_body("post_edit").replace("{language}", language),
            user=wrap_untrusted(json.dumps(payload, ensure_ascii=False)),
            schema=PostEdit,
            model=model,
            max_tokens=POST_EDIT_MAX_TOKENS,
        )
        text = edited.text
        changes += [
            Change(from_text=c.from_, to_text=c.to, reason=c.reason, enforced=False)
            for c in edited.changes
        ]

    text, enforced = enforce(
        source_text=source_text,
        translated=text,
        language=language,
        glossary=glossary,
        locked_id=locked_id,
    )
    changes += enforced
    meta["enforced_changes"] = len(enforced)
    meta["glossary_hits"] = [
        entry["term"] for entry in glossary.renderings if str(entry[language]) in text
    ]
    return text, changes, meta


# --- 4. backtranslate + qa ----------------------------------------------------


async def backtranslate_qa(
    *,
    source_text: str,
    produced: str,
    language: str,
    structured: Structured,
    model: str = JUDGE_MODEL,
) -> tuple[str, FidelityVerdict]:
    """Two independent calls, per D7: the back-translator never sees the source.

    That separation is the whole point of the check -- a model shown the English
    original will reproduce it rather than report what the target text actually
    says -- so `source_text` is used only in the second call.
    """
    back = await structured(
        system=prompt_body("backtranslate").replace("{language}", language),
        user=wrap_untrusted(produced),
        schema=Backtranslation,
        model=model,
    )
    verdict = await structured(
        system=prompt_body("qa_judge"),
        user=wrap_untrusted(f"SOURCE:\n{source_text}\n\nBACKTRANSLATION:\n{back.english}"),
        schema=FidelityVerdict,
        model=model,
    )
    return back.english, verdict


def flagged_for_review(verdict: FidelityVerdict) -> bool:
    """D7: scores <= 2 are auto-flagged for the reviewer."""
    return verdict.score <= 2


# --- 5. quiz ------------------------------------------------------------------


async def quiz(
    segments: list[SourceSegment],
    *,
    structured: Structured,
    model: str = QUIZ_MODEL,
) -> list[QuizDraftItem]:
    """Five English items tied to real segment ids (D7).

    Items citing a seg_id that is not in the module are dropped rather than
    renumbered: a comprehension item whose anchor we had to guess is not a
    comprehension item.
    """
    known = {s.seg_id for s in segments}
    payload = [{"seg_id": s.seg_id, "text": s.source_text} for s in segments]
    draft = await structured(
        system=prompt_body("quiz"),
        user=wrap_untrusted(json.dumps({"segments": payload})),
        schema=QuizDraft,
        model=model,
    )
    return [
        item
        for item in draft.items
        if item.seg_id in known and 0 <= item.answer < len(item.options) and len(item.options) == 4
    ]
