"""Upload a module, kick off localization, read its status."""

import os
import uuid
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from indic_platform.db.models import Module, Segment
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.authentication import AuthCredentials, BaseUser

from training_localizer.pipeline import LANGUAGES, localize, module_status
from training_localizer.terminology import load_glossary, resolve_locked_id

app = FastAPI(title="training localizer")

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
    task = localize.delay(str(module_id), requested)
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
