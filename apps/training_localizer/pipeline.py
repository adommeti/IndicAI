"""Celery tasks for the localization pipeline (PRD D5).

    adapt -> translate -> post_edit -> backtranslate_qa -> quiz -> (human gate)

Each task is independently re-runnable. A stage never overwrites: it reads the
latest version of the stage before it and writes `version + 1` of its own, so
re-running `post_edit` after a glossary change does not re-translate, and a
reviewer edit triggers re-production rather than re-translation.

The stage logic lives in `stages.py` as pure functions with injected vendors.
This module is the part that needs a broker and a database: it loads inputs,
calls a stage, and commits its output with the versions and identifiers that
`.claude/rules/adapters.md` and PRD D6 require on every persisted row.
"""

import asyncio
import os
import uuid
from collections.abc import Awaitable
from typing import Any

from celery import Celery, chain
from indic_platform.adapters.claude import Claude
from indic_platform.adapters.sarvam_translate import SarvamTranslate
from indic_platform.db.models import Localization, Module, QuizItem, Segment
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from training_localizer import stages
from training_localizer.stages import SourceSegment
from training_localizer.terminology import Glossary, load_glossary, resolve_locked_id

LANGUAGES = ("hi-IN", "te-IN", "ta-IN")
STAGES = ("adapt", "translate", "post_edit", "backtranslate", "approved")

celery_app = Celery(
    "training_localizer",
    broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)

# Per-stage retry policy (PRD D5). Deliberately narrow: `AdapterRuntime` already
# retries 429/5xx three times with jitter, so retrying a vendor error here would
# multiply spend. What this covers is infrastructure -- a database or broker blip
# between the worker and Postgres. A ValueError from a stage is a bug and must
# surface, not be retried into a stuck queue.
RETRY_ON = (OSError, TimeoutError, DBAPIError)
STAGE_TASK = {
    "autoretry_for": RETRY_ON,
    "retry_backoff": True,
    "retry_backoff_max": 300,
    "retry_jitter": True,
    "max_retries": 2,
}


def run[T](coro: Awaitable[T]) -> T:
    """Celery workers are synchronous; the adapters are not."""
    return asyncio.run(coro)  # type: ignore[arg-type]


def engine() -> Any:
    return create_async_engine(os.environ["DATABASE_URL"])


async def load_segments(
    db: AsyncSession, module_id: uuid.UUID, glossary: Glossary | None = None
) -> list[SourceSegment]:
    """Segments with their LOCKED statement resolved.

    `segments.locked` is a boolean (D6); which approved statement a locked
    segment IS comes from matching its English against approved_renderings.yaml,
    the same way D4 step 1 says the system may mark them.
    """
    glossary = glossary or load_glossary()
    rows = (
        await db.scalars(
            select(Segment).where(Segment.module_id == module_id).order_by(Segment.seg_id)
        )
    ).all()
    return [
        SourceSegment(
            seg_id=r.seg_id,
            start_ms=r.start_ms,
            end_ms=r.end_ms,
            source_text=r.source_text,
            locked=r.locked,
            locked_id=resolve_locked_id(r.source_text, glossary) if r.locked else None,
        )
        for r in rows
    ]


async def latest(
    db: AsyncSession, module_id: uuid.UUID, language: str, stage: str
) -> dict[int, Localization]:
    """The newest version of `stage` for each segment.

    Reading the max version rather than a fixed one is what lets an upstream
    stage be re-run without telling the downstream stages a number.
    """
    newest = (
        select(
            Localization.seg_id,
            func.max(Localization.version).label("version"),
        )
        .where(
            Localization.module_id == module_id,
            Localization.language == language,
            Localization.stage == stage,
        )
        .group_by(Localization.seg_id)
        .subquery()
    )
    rows = (
        await db.scalars(
            select(Localization)
            .join(
                newest,
                (Localization.seg_id == newest.c.seg_id)
                & (Localization.version == newest.c.version),
            )
            # The join above is already scoped by the subquery's WHERE, but the
            # outer select needs its own predicates: `localizations` is keyed on
            # five columns and the join only pins two of them.
            .where(
                Localization.module_id == module_id,
                Localization.language == language,
                Localization.stage == stage,
            )
        )
    ).all()
    return {row.seg_id: row for row in rows}


async def next_version(db: AsyncSession, module_id: uuid.UUID, language: str, stage: str) -> int:
    current = await db.scalar(
        select(func.max(Localization.version)).where(
            Localization.module_id == module_id,
            Localization.language == language,
            Localization.stage == stage,
        )
    )
    return int(current or 0) + 1


async def write_stage(
    db: AsyncSession,
    *,
    module_id: uuid.UUID,
    language: str,
    stage: str,
    version: int,
    texts: dict[int, str],
    meta: dict[int, dict[str, Any]],
    created_by: str,
) -> None:
    for seg_id, text in sorted(texts.items()):
        db.add(
            Localization(
                module_id=module_id,
                seg_id=seg_id,
                language=language,
                stage=stage,
                version=version,
                text=text,
                meta=meta.get(seg_id, {}),
                created_by=created_by,
            )
        )


# --- tasks --------------------------------------------------------------------


@celery_app.task(name="uc2.adapt", bind=True, **STAGE_TASK)
def adapt_task(self: Any, module_id: str, language: str) -> dict[str, Any]:
    return run(_adapt(uuid.UUID(module_id), language))


async def _adapt(module_id: uuid.UUID, language: str) -> dict[str, Any]:
    eng = engine()
    claude = Claude()
    try:
        async with AsyncSession(eng) as db, db.begin():
            segments = await load_segments(db, module_id)
            adapted, meta = await stages.adapt(segments, language, structured=claude.structured)
            version = await next_version(db, module_id, language, "adapt")
            await write_stage(
                db,
                module_id=module_id,
                language=language,
                stage="adapt",
                version=version,
                texts={seg_id: item.text for seg_id, item in adapted.items()},
                meta={
                    seg_id: {
                        "rationale": item.rationale,
                        "model": stages.ADAPT_MODEL,
                        "prompt_version": stages.prompt_version("adapt"),
                        "retried": seg_id in meta["retried"],
                        "over_budget": seg_id in meta["over_budget"],
                    }
                    for seg_id, item in adapted.items()
                },
                created_by="uc2.adapt",
            )
        return {"stage": "adapt", "language": language, "version": version, **meta}
    finally:
        await claude.client.close()
        await eng.dispose()


@celery_app.task(name="uc2.translate", bind=True, **STAGE_TASK)
def translate_task(self: Any, module_id: str, language: str) -> dict[str, Any]:
    return run(_translate(uuid.UUID(module_id), language))


async def _translate(module_id: uuid.UUID, language: str) -> dict[str, Any]:
    eng = engine()
    mayura = SarvamTranslate()  # SarvamAdapter holds no closable resource of its own
    glossary = load_glossary()
    try:
        async with AsyncSession(eng) as db, db.begin():
            segments = {s.seg_id: s for s in await load_segments(db, module_id)}
            source = await latest(db, module_id, language, "adapt")
            if not source:
                raise LookupError("adapt has not run for this module and language")
            texts: dict[int, str] = {}
            for seg_id, row in sorted(source.items()):
                segment = segments[seg_id]
                locked_text = (
                    str(glossary.statements[segment.locked_id][language])
                    if segment.locked and segment.locked_id
                    else None
                )
                texts[seg_id] = await stages.translate(
                    row.text,
                    language,
                    translator=lambda t, lang: mayura.translate(t, target=lang, mode="formal"),
                    locked_text=locked_text,
                )
            version = await next_version(db, module_id, language, "translate")
            await write_stage(
                db,
                module_id=module_id,
                language=language,
                stage="translate",
                version=version,
                texts=texts,
                meta={
                    seg_id: {
                        "model": "mayura:v1",
                        "mode": "formal",
                        "from_adapt": source[seg_id].version,
                    }
                    for seg_id in texts
                },
                created_by="uc2.translate",
            )
        return {
            "stage": "translate",
            "language": language,
            "version": version,
            "segments": len(texts),
        }
    finally:
        await eng.dispose()


@celery_app.task(name="uc2.post_edit", bind=True, **STAGE_TASK)
def post_edit_task(self: Any, module_id: str, language: str) -> dict[str, Any]:
    return run(_post_edit(uuid.UUID(module_id), language))


async def _post_edit(module_id: uuid.UUID, language: str) -> dict[str, Any]:
    eng = engine()
    claude = Claude()
    glossary: Glossary = load_glossary()
    try:
        async with AsyncSession(eng) as db, db.begin():
            segments = {s.seg_id: s for s in await load_segments(db, module_id)}
            adapted = await latest(db, module_id, language, "adapt")
            translated = await latest(db, module_id, language, "translate")
            if not translated:
                raise LookupError("translate has not run for this module and language")
            texts: dict[int, str] = {}
            meta: dict[int, dict[str, Any]] = {}
            change_log: list[dict[str, Any]] = []
            for seg_id, row in sorted(translated.items()):
                segment = segments[seg_id]
                english = adapted[seg_id].text if seg_id in adapted else segment.source_text
                text, changes, stage_meta = await stages.post_edit(
                    source_text=english,
                    translated=row.text,
                    language=language,
                    glossary=glossary,
                    structured=claude.structured,
                    locked_id=segment.locked_id,
                )
                texts[seg_id] = text
                meta[seg_id] = {
                    **stage_meta,
                    "change_log": [c.as_json() for c in changes],
                    "from_translate": row.version,
                }
                change_log += [{"seg_id": seg_id, **c.as_json()} for c in changes]
            version = await next_version(db, module_id, language, "post_edit")
            await write_stage(
                db,
                module_id=module_id,
                language=language,
                stage="post_edit",
                version=version,
                texts=texts,
                meta=meta,
                created_by="uc2.post_edit",
            )
        return {
            "stage": "post_edit",
            "language": language,
            "version": version,
            "changes": len(change_log),
            "change_log": change_log[:20],
        }
    finally:
        await claude.client.close()
        await eng.dispose()


@celery_app.task(name="uc2.backtranslate_qa", bind=True, **STAGE_TASK)
def backtranslate_qa_task(self: Any, module_id: str, language: str) -> dict[str, Any]:
    return run(_backtranslate_qa(uuid.UUID(module_id), language))


async def _backtranslate_qa(module_id: uuid.UUID, language: str) -> dict[str, Any]:
    eng = engine()
    claude = Claude()
    try:
        async with AsyncSession(eng) as db, db.begin():
            segments = {s.seg_id: s for s in await load_segments(db, module_id)}
            adapted = await latest(db, module_id, language, "adapt")
            edited = await latest(db, module_id, language, "post_edit")
            if not edited:
                raise LookupError("post_edit has not run for this module and language")
            texts: dict[int, str] = {}
            meta: dict[int, dict[str, Any]] = {}
            flagged: list[int] = []
            for seg_id, row in sorted(edited.items()):
                english = (
                    adapted[seg_id].text if seg_id in adapted else segments[seg_id].source_text
                )
                back, verdict = await stages.backtranslate_qa(
                    source_text=english,
                    produced=row.text,
                    language=language,
                    structured=claude.structured,
                )
                texts[seg_id] = back
                meta[seg_id] = {
                    "qa_score": verdict.score,
                    "qa_reason": verdict.reason,
                    "lost_or_changed": verdict.lost_or_changed,
                    "flagged": stages.flagged_for_review(verdict),
                    "model": stages.JUDGE_MODEL,
                    "prompt_version": stages.prompt_version("qa_judge"),
                    "from_post_edit": row.version,
                }
                if stages.flagged_for_review(verdict):
                    flagged.append(seg_id)
            version = await next_version(db, module_id, language, "backtranslate")
            await write_stage(
                db,
                module_id=module_id,
                language=language,
                stage="backtranslate",
                version=version,
                texts=texts,
                meta=meta,
                created_by="uc2.backtranslate_qa",
            )
            scores = [m["qa_score"] for m in meta.values()]
        return {
            "stage": "backtranslate",
            "language": language,
            "version": version,
            "fidelity_mean": sum(scores) / len(scores) if scores else 0.0,
            "flagged": flagged,
        }
    finally:
        await claude.client.close()
        await eng.dispose()


@celery_app.task(name="uc2.quiz", bind=True, **STAGE_TASK)
def quiz_task(self: Any, module_id: str, language: str = "en-IN") -> dict[str, Any]:
    return run(_quiz(uuid.UUID(module_id), language))


async def _quiz(module_id: uuid.UUID, language: str) -> dict[str, Any]:
    eng = engine()
    claude = Claude()
    try:
        async with AsyncSession(eng) as db, db.begin():
            segments = await load_segments(db, module_id)
            items = await stages.quiz(segments, structured=claude.structured)
            # `quiz_items` has no `version` column in D6, so a re-run replaces
            # this module-language's set rather than appending a second one:
            # otherwise two runs leave ten items with no way to say which five
            # are current. Approved items are kept -- a reviewer's decision is
            # not something a re-run gets to discard.
            approved = set(
                (
                    await db.scalars(
                        select(QuizItem.item_id).where(
                            QuizItem.module_id == module_id,
                            QuizItem.language == language,
                            QuizItem.approved.is_(True),
                        )
                    )
                ).all()
            )
            await db.execute(
                delete(QuizItem).where(
                    QuizItem.module_id == module_id,
                    QuizItem.language == language,
                    QuizItem.approved.is_(False),
                )
            )
            await db.flush()
            item_id = 1
            written = 0
            for item in items:
                while item_id in approved:
                    item_id += 1
                db.add(
                    QuizItem(
                        module_id=module_id,
                        language=language,
                        item_id=item_id,
                        seg_id=item.seg_id,
                        question=item.question,
                        options=item.options,
                        answer=item.answer,
                        rationale=item.rationale,
                        approved=False,
                    )
                )
                item_id += 1
                written += 1
        return {
            "stage": "quiz",
            "language": language,
            "items": written,
            "kept_approved": len(approved),
        }
    finally:
        await claude.client.close()
        await eng.dispose()


@celery_app.task(name="uc2.localize")
def localize(module_id: str, languages: list[str] | None = None) -> dict[str, Any]:
    """Queue the D5 chain per language.

    Each stage is dispatched as its own task, so each carries its own retry
    policy and a failure stops that language's chain without taking the others
    down with it. Signatures are immutable (`.si`) because a stage takes
    `(module_id, language)`, not the previous stage's return value.

    Chained rather than run in parallel within a language: every stage reads the
    newest version of the one before it, so overlapping runs on a single
    (module, language) would race for the same version number.
    """
    targets = list(languages or LANGUAGES)
    queued: dict[str, str] = {}
    for language in targets:
        result = chain(
            adapt_task.si(module_id, language),
            translate_task.si(module_id, language),
            post_edit_task.si(module_id, language),
            backtranslate_qa_task.si(module_id, language),
        ).apply_async()
        queued[language] = result.id
    queued["quiz"] = quiz_task.si(module_id, "en-IN").apply_async().id
    return {"module_id": module_id, "queued": queued}


async def module_status(db: AsyncSession, module_id: uuid.UUID) -> dict[str, Any]:
    """Per-language stage versions and QA state, for GET /modules/{id}/status."""
    module = await db.get(Module, module_id)
    if module is None:
        raise LookupError("Unknown module")
    total = await db.scalar(
        select(func.count()).select_from(Segment).where(Segment.module_id == module_id)
    )
    quiz_counts: dict[str, int] = {
        str(row[0]): int(row[1])
        for row in (
            await db.execute(
                select(QuizItem.language, func.count())
                .where(QuizItem.module_id == module_id)
                .group_by(QuizItem.language)
            )
        ).all()
    }
    languages: dict[str, Any] = {}
    for language in LANGUAGES:
        per_stage: dict[str, Any] = {}
        for stage in STAGES:
            rows = await latest(db, module_id, language, stage)
            if not rows:
                continue
            scores = [
                r.meta["qa_score"]
                for r in rows.values()
                if isinstance(r.meta, dict) and "qa_score" in r.meta
            ]
            per_stage[stage] = {
                "version": max(r.version for r in rows.values()),
                "segments": len(rows),
                "flagged": sorted(
                    seg_id
                    for seg_id, r in rows.items()
                    if isinstance(r.meta, dict) and r.meta.get("flagged")
                ),
            }
            if scores:
                per_stage[stage]["fidelity_mean"] = sum(scores) / len(scores)
        if language in quiz_counts:
            per_stage["quiz"] = {"items": quiz_counts[language]}
        if per_stage:
            languages[language] = per_stage
    return {
        "module_id": str(module_id),
        "title": module.title,
        "status": module.status,
        "segments": int(total or 0),
        "languages": languages,
        "quiz_items_en": quiz_counts.get("en-IN", 0),
    }


async def load_review(
    db: AsyncSession, module_id: uuid.UUID, language: str
) -> tuple[list[dict[str, Any]], dict[str, dict[int, dict[str, Any]]]]:
    """Everything the reviewer view needs, in one pass over the stage rows."""
    glossary = load_glossary()
    segments = [
        {
            "seg_id": s.seg_id,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "source_text": s.source_text,
            "locked": s.locked,
            "locked_id": s.locked_id,
        }
        for s in await load_segments(db, module_id, glossary)
    ]
    stages_out: dict[str, dict[int, dict[str, Any]]] = {}
    for stage in ("post_edit", "backtranslate", "approved"):
        rows = await latest(db, module_id, language, stage)
        stages_out[stage] = {
            seg_id: {
                "text": row.text,
                "meta": row.meta,
                "version": row.version,
                "created_by": row.created_by,
            }
            for seg_id, row in rows.items()
        }
    return segments, stages_out


async def approve_segment(
    db: AsyncSession,
    *,
    module_id: uuid.UUID,
    seg_id: int,
    language: str,
    text: str,
    reviewer: str,
    override_reason: str | None = None,
) -> dict[str, Any]:
    """Write an `approved` version for one segment, with who and when.

    A reviewer edit produces a new row rather than mutating the machine output,
    so the editorial history stays intact and `uc2/P4` can tell that only
    production needs re-running (PRD D5).
    """
    from training_localizer.review import check_approval

    glossary = load_glossary()
    segment = next(
        (s for s in await load_segments(db, module_id, glossary) if s.seg_id == seg_id), None
    )
    if segment is None:
        raise LookupError(f"Unknown segment {seg_id}")
    audit = check_approval(
        text=text,
        locked_id=segment.locked_id,
        language=language,
        glossary=glossary,
        override_reason=override_reason,
    )
    version = await next_version(db, module_id, language, "approved")
    db.add(
        Localization(
            module_id=module_id,
            seg_id=seg_id,
            language=language,
            stage="approved",
            version=version,
            text=text,
            meta={**audit, "reviewer": reviewer},
            created_by=reviewer,
        )
    )
    return {"seg_id": seg_id, "language": language, "version": version, **audit}


async def approve_quiz_item(
    db: AsyncSession, *, module_id: uuid.UUID, language: str, item_id: int, approved: bool
) -> dict[str, Any]:
    item = await db.get(QuizItem, (module_id, language, item_id))
    if item is None:
        raise LookupError(f"Unknown quiz item {item_id}")
    item.approved = approved
    return {"item_id": item_id, "language": language, "approved": approved}
