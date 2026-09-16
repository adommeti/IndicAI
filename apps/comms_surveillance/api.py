"""The reviewer, lead and governance API over the surveillance queue (uc3/P6).

Three readers, three different things they are allowed to see:

- `compliance_reviewer` works the queue: the flag list, one flag with its
  transcript and evidence span, the audio to hear it, and an append-only
  disposition.
- `compliance_lead` is a strict superset, plus the random QA-sample stream the
  false-negative estimate is built from.
- `governance` gets the aggregates and the audit chain's health, and nothing
  that quotes a call. See `auth.py` for why that is enforced as a deny rather
  than as a smaller grant.

Every one of those rules is a server-side dependency, checked before the route
body runs. `.claude/rules/apps.md`: UI hiding is not access control, so the UI
calls `/me` to decide what to *render* and this module decides what to
*answer*. The role matrix is pinned by `platform/tests/test_uc3_api.py`.

Two things this module is careful not to leak, because both were reachable in
the obvious implementation:

**404 versus 403.** A refused caller never learns whether the flag id they
named exists. The role dependency runs before path parsing and before the
lookup, so `/flags/<any uuid>` is 403 for governance whether the flag is real
or invented.

**Fields riding along.** The aggregate and chain-status responses are built by
projecting named fields, never by returning a helper's dict whole.
`audit.summarise` carries `head_hash` and `first_break_id`; the chain-status
response carries neither, because a row hash is exactly the thing the chain
exists to protect and a governance reader has no use for one.
"""

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from indic_platform.db.models import AnalysisRun, Call, Disposition, Flag, TranscriptSegment
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.concurrency import run_in_threadpool

from comms_surveillance import audit, auth, detector, metrics, storage
from comms_surveillance.auth import (
    Authenticated,
    CaseReader,
    LeadOnly,
    MetricsReader,
    Principal,
    check_dev_bypass,
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """A deployment that asks for the dev bypass in prod does not start.

    Louder than refusing each request, and it fails where a deploy is watching.
    The per-request check in `auth.principal` still stands, for the case where
    the variable is set on a process that is already up.
    """
    check_dev_bypass()
    yield


# `/docs`, `/redoc` and `/openapi.json` are served by FastAPI with no dependency
# attached, so they sit outside the role matrix entirely: an unauthenticated
# caller can read the full shape of a surveillance API -- every route, every
# field name, the disposition vocabulary. No transcript content leaks, but it is
# a map of the system handed to anyone who asks, and nothing in this app needs
# it in a deployed environment. They stay on where the dev bypass is allowed,
# because that is where they are actually used.
_PUBLIC_SCHEMA = auth.dev_bypass_allowed()

app = FastAPI(
    title="comms surveillance",
    lifespan=lifespan,
    docs_url="/docs" if _PUBLIC_SCHEMA else None,
    redoc_url="/redoc" if _PUBLIC_SCHEMA else None,
    openapi_url="/openapi.json" if _PUBLIC_SCHEMA else None,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "stage": "P6"}


# --- session ------------------------------------------------------------------


def engine() -> Any:
    return create_async_engine(os.environ["DATABASE_URL"])


async def db_session() -> Any:
    """One session per request. Overridden wholesale in tests."""
    eng = engine()
    try:
        async with AsyncSession(eng) as session:
            yield session
    finally:
        await eng.dispose()


Session = Annotated[AsyncSession, Depends(db_session)]


# --- the policy clause behind a category ---------------------------------------


# `flags` has no `policy_clause` column: the clause is not a property of the
# finding, it is the current text of the section the category is named after,
# and pinning a copy on every row would let the queue quote a definition that
# Compliance has since rewritten. It is resolved from `policy.md` at read time
# instead, through `detector.policy_document()` so both halves read one file.
_CLAUSE_STOPS = ("**Flag.**", "**Do not flag.**")


def policy_clause(category: str) -> str:
    """The conduct and the line, verbatim from `policy.md`, for one category.

    Returns an empty string for a category the policy does not define -- an
    older flag whose category was retired, say. Empty, never a guess: a
    reviewer acting on a clause the document does not contain is worse than a
    reviewer who can see there is none.
    """
    for block in detector.policy_document().split("\n## ")[1:]:
        name, _, body = block.partition("\n")
        if name.strip() != category:
            continue
        for stop in _CLAUSE_STOPS:
            body = body.split(stop)[0]
        return body.strip().strip("-").strip()
    return ""


# --- reading flags -------------------------------------------------------------


# The reviewer's queue order (PRD E7): worst first, and oldest first inside a
# severity so a flag cannot be starved by newer ones at the same level.
SEVERITY_ORDER = case({"high": 0, "medium": 1, "low": 2}, value=Flag.severity, else_=3)

Severity = Literal["low", "medium", "high"]


def _latest_disposition_subquery() -> Any:
    """flag_id -> the `seq` of its most recent disposition.

    `seq` rather than `created_at`: two dispositions written in one transaction
    share a statement timestamp, so `created_at` is not a total order and "the
    latest" would be a coin toss. The chain already has a total order; use it.
    """
    return (
        select(Disposition.flag_id, func.max(Disposition.seq).label("seq"))
        .group_by(Disposition.flag_id)
        .subquery()
    )


def _summary(flag: Flag, disposition: str | None) -> dict[str, Any]:
    return {
        "flag_id": str(flag.id),
        "call_id": str(flag.call_id),
        "category": flag.category,
        "severity": flag.severity,
        "speaker": flag.speaker,
        "start_ms": flag.start_ms,
        "created_at": flag.created_at.isoformat() if flag.created_at else None,
        "disposition": disposition,
    }


async def flag_summaries(
    session: AsyncSession,
    *,
    severity: str | None = None,
    undispositioned: bool = False,
    qa_sample: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """The queue, in severity order, each flag carrying its latest disposition."""
    latest = _latest_disposition_subquery()
    query = (
        select(Flag, Disposition.disposition)
        .outerjoin(latest, latest.c.flag_id == Flag.id)
        .outerjoin(Disposition, Disposition.seq == latest.c.seq)
        .order_by(SEVERITY_ORDER, Flag.created_at.asc(), Flag.seq.asc())
        .limit(limit)
    )
    if severity is not None:
        query = query.where(Flag.severity == severity)
    if undispositioned:
        query = query.where(latest.c.seq.is_(None))
    if qa_sample:
        # A QA-sampled call is one nothing else escalated -- `combine` only adds
        # the reason when the list is otherwise empty -- so the sample's
        # denominator stays uncontaminated. The reason is on the analysis run,
        # which is the only durable record that this call was sampled.
        query = query.join(AnalysisRun, AnalysisRun.id == Flag.run_id).where(
            AnalysisRun.output["escalation_reasons"].contains(["qa_sample"])
        )
    rows = (await session.execute(query)).all()
    return [_summary(flag, disposition) for flag, disposition in rows]


async def flag_detail(session: AsyncSession, flag_id: uuid.UUID) -> dict[str, Any]:
    """One flag, its call's transcript and every disposition ever written on it.

    Raises `LookupError` for an unknown flag; the caller turns that into a 404,
    which is safe here because only a reviewer or a lead ever reaches this
    function.
    """
    flag = await session.get(Flag, flag_id)
    if flag is None:
        raise LookupError(str(flag_id))

    segments = (
        await session.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.call_id == flag.call_id)
            .order_by(TranscriptSegment.start_ms, TranscriptSegment.seg_id)
        )
    ).all()
    history = (
        await session.scalars(
            select(Disposition)
            .where(Disposition.flag_id == flag_id)
            .order_by(Disposition.seq.desc())
        )
    ).all()

    detail = _summary(flag, history[0].disposition if history else None)
    detail.update(
        {
            "evidence_span": flag.evidence_span,
            "english_rendering": flag.english_rendering,
            "reasoning": flag.reasoning,
            "policy_clause": policy_clause(flag.category),
            "transcript": [
                {
                    "seg_id": segment.seg_id,
                    "speaker": segment.speaker,
                    "start_ms": segment.start_ms,
                    "end_ms": segment.end_ms,
                    "text": segment.text,
                    "text_roman": segment.text_roman,
                }
                for segment in segments
            ],
            "dispositions": [
                {
                    "disposition": row.disposition,
                    "note": row.note,
                    "reviewer_id": row.reviewer_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in history
            ],
        }
    )
    return detail


# --- routes: identity ----------------------------------------------------------


@app.get("/me")
def me(caller: Annotated[Principal, Authenticated]) -> dict[str, Any]:
    """Who the server thinks you are, and which roles it recognised.

    Open to any verified subject, including one holding none of the three
    roles: a caller with no grant needs to be told that, and refusing `/me`
    would leave the UI unable to render anything but a blank page.
    """
    return caller.as_json()


# --- routes: the queue ---------------------------------------------------------


@app.get("/flags")
async def list_flags(
    session: Session,
    caller: Annotated[Principal, CaseReader],
    severity: Severity | None = None,
    undispositioned: bool = False,
) -> dict[str, Any]:
    return {
        "flags": await flag_summaries(session, severity=severity, undispositioned=undispositioned)
    }


@app.get("/flags/{flag_id}")
async def get_flag(
    flag_id: uuid.UUID,
    session: Session,
    caller: Annotated[Principal, CaseReader],
) -> dict[str, Any]:
    try:
        return await flag_detail(session, flag_id)
    except LookupError as exc:
        raise HTTPException(404, "Unknown flag") from exc


# How long a signed recording URL lives. Short because it is a bearer token for
# a call recording: anyone who gets the string gets the audio, so the window in
# which a copied URL is still worth anything is the thing to minimise. Long
# enough for a browser to start playing and seek within the clip.
AUDIO_URL_TTL_S = 180


@app.get("/flags/{flag_id}/audio")
async def flag_audio(
    flag_id: uuid.UUID,
    session: Session,
    caller: Annotated[Principal, CaseReader],
) -> dict[str, Any]:
    """A short-lived presigned URL for the recording, and where to seek to.

    The URL is returned and nothing else: it is never logged, never put on a
    Langfuse span (`.claude/rules/adapters.md` forbids tracing signed URLs) and
    never persisted. `start_ms` comes back with it so the player can seek to the
    evidence rather than making a reviewer hunt for it.

    Signing lives here rather than in `storage.py` because `storage.py` is the
    ingestion side -- it lists and fetches objects for the pipeline -- and this
    is the only caller that ever hands a URL to a browser. The sibling app does
    the same thing in the same place (`training_localizer.api.media`).
    """
    flag = await session.get(Flag, flag_id)
    if flag is None:
        raise HTTPException(404, "Unknown flag")
    call = await session.get(Call, flag.call_id)
    if call is None or not call.source_key:
        raise HTTPException(404, "This flag's recording is no longer available")

    try:
        minio = storage.client()
        url = await run_in_threadpool(
            minio.presigned_get_object,
            storage.BUCKET,
            call.source_key,
            expires=timedelta(seconds=AUDIO_URL_TTL_S),
        )
    except KeyError as exc:  # missing MINIO_* credentials
        raise HTTPException(503, "Object storage is not configured") from exc
    return {"url": url, "expires_in_s": AUDIO_URL_TTL_S, "start_ms": flag.start_ms}


DispositionValue = Literal["confirmed", "false_positive", "needs_more_context", "escalated"]


class DispositionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    disposition: DispositionValue
    # No `reviewer_id`: identity comes from the SSO claim and a body field must
    # not be able to sign somebody else's name to an audited decision.
    note: str = Field(default="", max_length=4000)


def _receipt(row: Disposition, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "disposition_id": str(row.id),
        "seq": row.seq,
        "row_hash": row.row_hash,
        "replayed": replayed,
    }


@app.post("/flags/{flag_id}/dispositions", status_code=201)
async def create_disposition(
    flag_id: uuid.UUID,
    body: DispositionIn,
    session: Session,
    response: Response,
    caller: Annotated[Principal, CaseReader],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)] = None,
) -> dict[str, Any]:
    """Record a decision. Append-only: a changed mind is a new row.

    Written through `audit.append` and never with a plain `session.add`, so the row
    takes its place in the hash chain under the table's advisory lock. A disposition
    inserted around the side is a disposition that breaks the chain for every row
    after it.

    ## `Idempotency-Key`, and why this endpoint needs one more than most

    A duplicate POST here is not a row somebody tidies up later. `dispositions` is
    append-only and hash-chained, and the application's database role holds INSERT and
    SELECT on it and nothing else, so a second ruling on the same flag by the same
    reviewer is permanent and cannot be withdrawn by this service at all. A page
    refresh, a proxy retry or a dropped connection was enough to cause one: the only
    guard was the UI disabling its own button while the request was in flight.

    Send the same key to retry safely. A replay returns the ORIGINAL receipt with 200
    and `replayed: true`, so a client that never saw the first response gets the first
    row's id rather than a new one. Without a key the old behaviour stands -- every
    POST appends -- because a key derived from the request body would be worse than
    none: it would silently collapse a genuine change of mind, which this table is
    explicitly designed to record as a new row, into a replay of the old one.

    The uniqueness is enforced by the database (`uq_dispositions_flag_idempotency_key`),
    not by the check below. The check is the fast path that returns the original
    receipt; the constraint is what holds when two retries race, and the
    `IntegrityError` arm is how that race resolves into the same answer.
    """
    if await session.get(Flag, flag_id) is None:
        raise HTTPException(404, "Unknown flag")

    key = idempotency_key.strip() if idempotency_key else None
    if key:
        existing = (
            await session.scalars(
                select(Disposition).where(
                    Disposition.flag_id == flag_id, Disposition.idempotency_key == key
                )
            )
        ).first()
        if existing is not None:
            response.status_code = 200
            return _receipt(existing, replayed=True)

    try:
        row = await audit.append(
            session,
            Disposition(
                flag_id=flag_id,
                disposition=body.disposition,
                note=body.note,
                reviewer_id=caller.identity,
                idempotency_key=key,
            ),
        )
        await session.commit()
    except IntegrityError:
        # Two retries raced past the check above and the constraint caught the second.
        # The rollback is required before the session can be used again, and the answer
        # is the row the winner wrote: both callers asked for one ruling and there is
        # one ruling.
        await session.rollback()
        if not key:
            raise
        winner = (
            await session.scalars(
                select(Disposition).where(
                    Disposition.flag_id == flag_id, Disposition.idempotency_key == key
                )
            )
        ).first()
        if winner is None:
            raise
        response.status_code = 200
        return _receipt(winner, replayed=True)
    return _receipt(row)


@app.get("/qa-sample")
async def qa_sample(
    session: Session,
    caller: Annotated[Principal, LeadOnly],
) -> dict[str, Any]:
    """Flags raised on calls that only Stage 2's random QA sample escalated.

    The lead's stream, not the reviewer's: it is a measurement of the detector,
    not a queue of work, and mixing it into the reviewer's queue would bias the
    false-negative denominator with calls that were reviewed for other reasons.
    """
    items = await flag_summaries(session, qa_sample=True)
    return {"items": [{**item, "qa_sampled": True} for item in items]}


# --- routes: metrics and chain status -----------------------------------------


# Keys a `precision_over_time` bucket may carry into a response. An allow-list
# rather than a pass-through, because this is one of the two responses a
# governance reader is allowed to see and `metrics.py` is a separate module: a
# field added there must not be able to become a transcript quote here without
# somebody editing this line. Owners of `metrics.py`: a new bucket key needs to
# be added here too, and `test_uc3_api` will say so if it is not.
OVER_TIME_KEYS = frozenset(
    {
        "bucket",
        "bucket_start",
        "bucket_end",
        "category",
        "flags",
        "confirmed",
        "false_positive",
        "decided",
        "precision",
    }
)


def _category_precision(row: metrics.CategoryPrecision) -> dict[str, Any]:
    """Projected field by field; `precision` stays None when nothing is decided.

    None, never 0.0 and never 1.0. A precision of zero decided reviews is not a
    precision of zero -- that is the uc3/P1 defect (`evidence_failure_rate: 0.0`
    over zero checks) and the contract names it explicitly.
    """
    return {
        "category": row.category,
        "confirmed": row.confirmed,
        "false_positive": row.false_positive,
        "decided": row.decided,
        "precision": row.precision,
    }


@app.get("/metrics/precision")
async def precision(
    session: Session,
    caller: Annotated[Principal, MetricsReader],
    bucket: Annotated[str, Query(max_length=16)] = "week",
) -> dict[str, Any]:
    # Validated here rather than left to `metrics.precision_over_time`, which
    # raises ValueError -- a 500 for what is a caller's typo.
    if bucket not in metrics.BUCKETS:
        raise HTTPException(422, f"bucket must be one of {', '.join(metrics.BUCKETS)}")
    by_category = await metrics.precision_by_category(session)
    over_time = await metrics.precision_over_time(session, bucket=bucket)
    return {
        "by_category": [_category_precision(row) for row in by_category],
        "over_time": [
            {key: value for key, value in point.items() if key in OVER_TIME_KEYS}
            for point in over_time
        ],
        # Named, so a category with nothing decided reads as "we do not know"
        # rather than disappearing from the chart.
        "unmeasured": [row.category for row in by_category if row.precision is None],
    }


@app.get("/metrics/false_negative_estimate")
async def false_negative_estimate(
    session: Session,
    caller: Annotated[Principal, MetricsReader],
) -> dict[str, Any]:
    estimate = await metrics.false_negative_estimate(session)
    return {
        # `sampled` is the whole sample; `settled` is the denominator. Both are
        # projected because they diverge, and the gap is the story: a sample
        # that is 98% unreviewed must not render as a clean bill of health over
        # the 2% that came back. `flagless` is the part of `settled` no human
        # actually read -- the detector's own word that there was nothing there.
        "sampled": estimate.sampled,
        "settled": estimate.settled,
        "missed": estimate.missed,
        "pending": estimate.pending,
        "flagless": estimate.flagless,
        "rate": estimate.rate,
        "unmeasured": estimate.rate is None,
    }


@app.get("/audit/chain_status")
async def chain_status(
    session: Session,
    caller: Annotated[Principal, MetricsReader],
) -> dict[str, Any]:
    """The last nightly verification, not a fresh one. Metadata only.

    This reports the stored anchor -- written by `uc3.chain_verify` only after a
    clean walk -- plus each table's current row count. It deliberately does not
    call `audit.verify_all`.

    Re-walking here was the first implementation and it was wrong twice over.
    Cost: `verify_all` recomputes a sha256 for every row in all three chains,
    synchronously, inside the event loop; at uc3/P5's own measured rate that is
    roughly half a second of CPU per ten thousand rows, it blocks the loop for
    every other request while it runs, and it grows without bound because these
    tables only ever grow. Any authenticated caller -- including `governance`,
    the least privileged role in the system -- could hold the API down by
    refreshing a dashboard. Meaning: a fresh walk proves the chain is internally
    consistent *right now*, which is not the question. The chain's guarantee
    comes from the anchor recorded outside it, and that is a nightly fact. A
    per-request walk would also re-verify against an anchor nobody has updated,
    so it could only ever repeat last night's answer more expensively.

    `head_hash` and `first_break_id` are not projected. A head hash is the value
    an anchor is compared against, so publishing it hands a would-be tamperer
    the target to re-chain to, and `first_break_id` names a row in the evidence
    store, on the one endpoint governance can reach.
    """
    rows = await audit.counts(session)
    tables = []
    for table in audit.CHAINED:
        anchor = await audit.read_anchor(session, table)
        current = rows.get(table, 0)
        if anchor is None:
            # Never verified. Not a break, and not a pass either.
            tables.append(
                {
                    "table": table,
                    "rows": current,
                    "ok": None,
                    "anchor_ok": None,
                    "verified_at": None,
                    "reason": "no verification recorded yet",
                }
            )
            continue
        shrank = current < anchor.rows
        tables.append(
            {
                "table": table,
                "rows": current,
                "ok": not shrank,
                "anchor_ok": not shrank,
                "verified_at": anchor.recorded_at.isoformat(),
                "reason": (
                    f"rows dropped from {anchor.rows} to {current} since the last verification"
                    if shrank
                    else ""
                ),
            }
        )
    return {
        "tables": tables,
        "breaks": sum(1 for table in tables if table["ok"] is False),
        # `ok` is False only on a real break. A chain that has never been
        # verified is `None` here, not True: "we have not checked" must not
        # render as "no breaks found".
        "ok": None if any(t["ok"] is None for t in tables) else all(t["ok"] for t in tables),
        "unverified": [table["table"] for table in tables if table["ok"] is None],
        "checked_at": datetime.now(UTC).isoformat(),
    }
