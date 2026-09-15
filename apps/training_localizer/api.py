"""Upload a module, kick off localization, read its status."""

import os
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from indic_platform.db.models import Module, QuizItem, Segment
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.authentication import AuthCredentials, BaseUser
from starlette.concurrency import run_in_threadpool

from training_localizer.pipeline import (
    LANGUAGES,
    approve_quiz_item,
    approve_segment,
    load_review,
    localize,
    module_status,
)
from training_localizer.review import OverrideRequired, review_rows, review_summary
from training_localizer.terminology import load_glossary, resolve_locked_id

app = FastAPI(title="training localizer")

# The reviewer UI is a static bundle; `npm run build` in apps/training_localizer/ui
# produces it. Mounted last (see the bottom of this module) so it cannot shadow an
# API route, and skipped entirely when it has not been built -- a missing bundle
# must not stop the API from serving.
UI_DIST = Path(__file__).parent / "ui" / "dist"

Language = Literal["hi-IN", "te-IN", "ta-IN"]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "stage": "P2"}


def authenticated_owner(request: Request) -> str:
    """Same fail-closed SSO contract as the helpdesk app.

    Uploading a compliance module and spending vendor budget are both privileged;
    neither body fields nor identity headers authenticate a caller. A bare
    deployment without trusted SSO middleware refuses every write.
    """
    user = request.scope.get("user")
    credentials = request.scope.get("auth")
    if (
        not isinstance(user, BaseUser)
        or not user.is_authenticated
        or not isinstance(credentials, AuthCredentials)
        or "authenticated" not in credentials.scopes
    ):
        raise HTTPException(401, "Authenticated training owner required")
    try:
        identity = user.identity
    except NotImplementedError as exc:
        raise HTTPException(401, "Verified owner subject required") from exc
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 128:
        raise HTTPException(401, "Invalid owner subject")
    return identity


class SegmentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seg_id: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    source_text: str = Field(min_length=1, max_length=8000)
    locked: bool = False

    @field_validator("end_ms")
    @classmethod
    def _ordered(cls, end_ms: int, info: Any) -> int:
        start = info.data.get("start_ms")
        if start is not None and end_ms <= start:
            raise ValueError("end_ms must be after start_ms")
        return end_ms


class ModuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    # The MP4 is uploaded to MinIO out of band and referenced here; the API
    # takes the URI, not the bytes, so an upload cannot block a worker.
    video_uri: str | None = Field(default=None, max_length=2000)
    segments: list[SegmentIn] = Field(min_length=1, max_length=2000)

    @field_validator("segments")
    @classmethod
    def _unique_ids(cls, segments: list[SegmentIn]) -> list[SegmentIn]:
        if len({s.seg_id for s in segments}) != len(segments):
            raise ValueError("seg_id must be unique within a module")
        return segments


def engine() -> Any:
    return create_async_engine(os.environ["DATABASE_URL"])


@app.post("/modules", status_code=201)
async def create_module(
    body: ModuleIn, owner: Annotated[str, Depends(authenticated_owner)]
) -> dict[str, Any]:
    """Create the module and its segments, marking LOCKED ones.

    A segment the caller marked `locked` whose English does not match an
    approved statement is rejected rather than stored: a locked segment with no
    approved rendering has no defined target text, and accepting it would push
    an unanswerable segment down the pipeline.
    """
    glossary = load_glossary()
    unmatched = [
        s.seg_id
        for s in body.segments
        if s.locked and resolve_locked_id(s.source_text, glossary) is None
    ]
    if unmatched:
        raise HTTPException(
            422,
            f"Locked segments have no approved rendering: {unmatched}. "
            "Add the statement to approved_renderings.yaml or unmark the segment.",
        )
    module_id = uuid.uuid4()
    eng = engine()
    try:
        async with AsyncSession(eng) as db, db.begin():
            db.add(Module(id=module_id, title=body.title, source_lang="en-IN", status="uploaded"))
            await db.flush()
            for segment in body.segments:
                db.add(
                    Segment(
                        module_id=module_id,
                        seg_id=segment.seg_id,
                        start_ms=segment.start_ms,
                        end_ms=segment.end_ms,
                        source_text=segment.source_text,
                        locked=segment.locked,
                    )
                )
    finally:
        await eng.dispose()
    return {
        "module_id": str(module_id),
        "segments": len(body.segments),
        "locked": sum(1 for s in body.segments if s.locked),
        "created_by": owner,
    }


@app.post("/modules/{module_id}/localize", status_code=202)
async def localize_module(
    module_id: uuid.UUID,
    owner: Annotated[str, Depends(authenticated_owner)],
    languages: Annotated[str, Query(max_length=64)] = ",".join(LANGUAGES),
) -> dict[str, Any]:
    """Queue the chain. Returns immediately; poll /status for progress."""
    requested = [item.strip() for item in languages.split(",") if item.strip()]
    unknown = [item for item in requested if item not in LANGUAGES]
    if unknown or not requested:
        raise HTTPException(422, f"Unsupported languages: {unknown or 'none given'}")
    eng = engine()
    try:
        async with AsyncSession(eng) as db:
            if await db.get(Module, module_id) is None:
                raise HTTPException(404, "Unknown module")
    finally:
        await eng.dispose()
    # Celery's .delay() is a blocking broker round-trip; keep it off the loop.
    task = await run_in_threadpool(localize.delay, str(module_id), requested)
    return {"module_id": str(module_id), "languages": requested, "task_id": task.id}


@app.get("/modules/{module_id}/status")
async def status(
    module_id: uuid.UUID, owner: Annotated[str, Depends(authenticated_owner)]
) -> dict[str, Any]:
    eng = engine()
    try:
        async with AsyncSession(eng) as db:
            return await module_status(db, module_id)
    except LookupError as exc:
        raise HTTPException(404, "Unknown module") from exc
    finally:
        await eng.dispose()


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Language
    text: str = Field(min_length=1, max_length=8000)
    # Only needed when the segment is LOCKED and `text` differs from its
    # approved rendering; the API decides, the client cannot opt out.
    override_reason: str | None = Field(default=None, max_length=2000)


class QuizApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(min_length=2, max_length=16)
    approved: bool = True


@app.get("/modules/{module_id}/review")
async def review(
    module_id: uuid.UUID,
    reviewer: Annotated[str, Depends(authenticated_owner)],
    language: Language = "hi-IN",
) -> dict[str, Any]:
    """The reviewer table for one module-language, plus its quiz items."""
    eng = engine()
    try:
        async with AsyncSession(eng) as db:
            if await db.get(Module, module_id) is None:
                raise HTTPException(404, "Unknown module")
            segments, stages = await load_review(db, module_id, language)
            quiz = (
                await db.scalars(
                    select(QuizItem)
                    .where(QuizItem.module_id == module_id)
                    .order_by(QuizItem.language, QuizItem.item_id)
                )
            ).all()
    finally:
        await eng.dispose()

    rows = review_rows(
        segments=segments,
        post_edit=stages["post_edit"],
        backtranslate=stages["backtranslate"],
        approved=stages["approved"],
        language=language,
        glossary=load_glossary(),
    )
    return {
        "module_id": str(module_id),
        "language": language,
        "reviewer": reviewer,
        "summary": review_summary(rows),
        "segments": [row.as_json() for row in rows],
        "quiz_items": [
            {
                "item_id": q.item_id,
                "language": q.language,
                "seg_id": q.seg_id,
                "question": q.question,
                "options": q.options,
                "answer": q.answer,
                "rationale": q.rationale,
                "approved": q.approved,
            }
            for q in quiz
        ],
    }


@app.put("/modules/{module_id}/segments/{seg_id}/approve")
async def approve(
    module_id: uuid.UUID,
    seg_id: int,
    body: ApproveIn,
    reviewer: Annotated[str, Depends(authenticated_owner)],
) -> dict[str, Any]:
    """Save a reviewer's text as the approved version of this segment.

    A LOCKED segment that does not match its approved rendering is refused with
    409 and the expected text, until the reviewer supplies an override reason.
    """
    eng = engine()
    try:
        async with AsyncSession(eng) as db, db.begin():
            if await db.get(Module, module_id) is None:
                raise HTTPException(404, "Unknown module")
            try:
                return await approve_segment(
                    db,
                    module_id=module_id,
                    seg_id=seg_id,
                    language=body.language,
                    text=body.text,
                    reviewer=reviewer,
                    override_reason=body.override_reason,
                )
            except OverrideRequired as exc:
                raise HTTPException(
                    409,
                    {
                        "error": "locked_override_required",
                        "locked_id": exc.locked_id,
                        "expected": exc.expected,
                        "hint": (
                            "This is a LOCKED compliance statement. Restore the approved "
                            "rendering, or supply override_reason of at least 10 characters "
                            "explaining why it must differ."
                        ),
                    },
                ) from exc
            except LookupError as exc:
                raise HTTPException(404, "Unknown segment") from exc
    finally:
        await eng.dispose()


@app.put("/modules/{module_id}/quiz/{item_id}/approve")
async def approve_quiz(
    module_id: uuid.UUID,
    item_id: int,
    body: QuizApproveIn,
    reviewer: Annotated[str, Depends(authenticated_owner)],
) -> dict[str, Any]:
    eng = engine()
    try:
        async with AsyncSession(eng) as db, db.begin():
            try:
                return await approve_quiz_item(
                    db,
                    module_id=module_id,
                    language=body.language,
                    item_id=item_id,
                    approved=body.approved,
                )
            except LookupError as exc:
                raise HTTPException(404, "Unknown quiz item") from exc
    finally:
        await eng.dispose()


if UI_DIST.is_dir():
    app.mount("/", StaticFiles(directory=UI_DIST, html=True), name="ui")
