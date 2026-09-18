"""Retention for uc3, and the two reasons it deletes less than it looks like it should.

PRD T5 puts UC3's window at "per Compliance's recordkeeping rule" and F4 asks for a
retention job that is implemented, tested and logs its deletions. This module is that
job. What it will not do is invent the rule.

## 1. The rule does not exist yet, so nothing is deleted by default

`docs/adr/0004-surveillance-scope.md` is deliberately unwritten -- `docs/adr/README.md`
says so, and says this module is where that shows: "uc3/P7 leaves the retention
deletion job disabled until this ADR sets the rule". Retention is a lawful-basis
question (how long may a recording of a named employee be kept, and on what grounds),
and an engineer picking 90 days because uc1 picked 90 days is guessing at law.

So deletion has two independent gates and needs both:

  UC3_CALL_RETENTION_DAYS   the window. Unset means no window, which means
                            "keep everything" -- the PRD's own default -- and a pass
                            that matches nothing and says why.
  UC3_RETENTION_ENABLED     the safety catch, default false. Even a configured window
                            only rehearses until somebody sets this, which is the
                            deployment step that should require a person who has read
                            ADR 0004.

The sweep still *runs* nightly with both unset, and still writes its audit rows. That
is deliberate and is the most useful thing it can do before the policy exists: a
dry-run row every night proves the schedule is actually deployed and shows exactly
what the first real pass would remove. A job that does not run until the policy lands
is a job nobody discovers is misconfigured until the night it matters -- and a
retention policy that has never executed looks identical, in the database, to one that
ran and found nothing.

## 2. The audit chain pins the call row, permanently

uc3's `analysis_runs`, `flags` and `dispositions` are append-only and hash-chained, and
the app's database role holds INSERT/SELECT on them and nothing else (migration
`0008_uc3_audit_role`). Both `analysis_runs.call_id` and `flags.call_id` are foreign
keys onto `calls.id`. Those two facts together are a hard boundary, verified against a
live PostgreSQL 16 rather than reasoned about (`platform/tests/test_uc3_retention.py`
runs the same three steps):

    delete from transcript_segments where call_id = ...   ->  DELETE 1
    delete from calls where id = ...                      ->  ERROR: update or delete on
                                                              table "calls" violates
                                                              foreign key constraint
                                                              "analysis_runs_call_id_fkey"
    delete from analysis_runs where id = ...              ->  ERROR: permission denied
                                                              for table analysis_runs

So once a call has been analysed, its `calls` row cannot be removed by this application
at all, and the row cannot be cleared out of the way either. `calls` is therefore not a
policy target here. Pretending otherwise would produce a retention job that fails every
night on a foreign key, which is worse than one that states its limit.

What that costs, stated plainly because a reviewer must not have to derive it:
`flags.evidence_span` and `analysis_runs.output` hold verbatim call content, inside the
chain. This job cannot delete them, and neither can anything else uc3 runs. An erasure
request that reaches quoted evidence needs a privileged operator and a chain re-anchor,
which is a Compliance decision and not an engineering one. ADR 0016 records that; it is
the same kind of bound ADR 0011 puts on the chain's non-repudiation claim.

What this job therefore deletes: the verbatim transcript (`transcript_segments.text` and
`text_roman`, the authoritative copy of what was said) and the source recordings. That
is the bulk of the sensitive content and all of the audio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from celery.schedules import crontab
from indic_platform.db.models import Call, RetentionDeletion, TranscriptSegment
from indic_platform.tasks import BudgetAwareTask
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from comms_surveillance.ingest import celery_app

log = logging.getLogger(__name__)

APP = "comms_surveillance"
TRANSCRIPT_POLICY = "uc3_transcripts"
TRANSCRIPT_TARGET = "transcript_segments"
RECORDING_POLICY = "uc3_recordings"
RECORDING_TARGET = "object-store"

COMPLETED = "completed"
FAILED = "failed"
#: Written to `detail.reason` when a pass did nothing because no window is set.
UNCONFIGURED = "no retention window configured (docs/adr/0004 unwritten)"

DEFAULT_BATCH_SIZE = 500

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    return value if value > 0 else default


def retention_days() -> int | None:
    """The configured window, or None when Compliance has not set one.

    None is a real answer here, not a missing value to paper over with a default.
    "Keep everything for the configured period" (PRD P7) with no configured period
    means keep everything, and a module-level `= 90` would quietly turn an unanswered
    legal question into a deletion policy.
    """
    raw = os.environ.get("UC3_CALL_RETENTION_DAYS", "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        log.warning("UC3_CALL_RETENTION_DAYS=%r is not an integer; treating as unset", raw)
        return None
    if days <= 0:
        log.warning("UC3_CALL_RETENTION_DAYS=%r is not positive; treating as unset", raw)
        return None
    return days


def deletion_enabled() -> bool:
    """The safety catch. Anything but an exact `true` keeps the job rehearsing."""
    return os.environ.get("UC3_RETENTION_ENABLED", "").strip().lower() == "true"


def batch_size() -> int:
    return _int_env("UC3_RETENTION_BATCH_SIZE", DEFAULT_BATCH_SIZE)


def dry_run_default() -> bool:
    """A pass is a rehearsal unless BOTH gates are open.

    Expressed as one function so the decision lives in one place, and `sweep` folds an
    explicit `dry_run` argument into it with `or` rather than replacing it -- so no
    scheduled or task-invoked pass can delete with either gate closed, whatever it was
    passed.

    The narrower functions (`purge_transcripts`, `purge_recordings`) still honour their
    own `dry_run` and `days` arguments, because a test and an operator rehearsing one
    policy by hand need that. They default to `dry_run=True`, so the careless call is
    the safe one; `sweep` is the entry point every scheduled pass goes through, and it
    is where the gates are enforced rather than merely consulted.
    """
    return not (deletion_enabled() and retention_days() is not None)


@dataclass
class Outcome:
    """The `retention_deletions` row one policy pass earned, before it is written.

    A dataclass rather than the ORM object, so a pass can be reasoned about and tested
    without a database, and so a pass that failed *because* of the database still has
    something to report.
    """

    policy: str
    target: str
    retention_days: int
    cutoff_at: datetime
    started_at: datetime
    finished_at: datetime
    dry_run: bool
    batch_size: int
    rows_matched: int = 0
    rows_deleted: int = 0
    batches: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    status: str = COMPLETED
    detail: dict[str, Any] = field(default_factory=dict)

    def saw(self, stamps: Sequence[datetime]) -> None:
        """Widen the covered window by one batch's timestamps.

        The audit row's check constraint ties the window to the match count: a row
        cannot claim "matched 900" with no window naming which 900.
        """
        stamps = [s for s in stamps if s is not None]
        if not stamps:
            return
        low, high = min(stamps), max(stamps)
        self.window_start = low if self.window_start is None else min(self.window_start, low)
        self.window_end = high if self.window_end is None else max(self.window_end, high)

    def row(self, job_run_id: uuid.UUID) -> RetentionDeletion:
        return RetentionDeletion(
            id=uuid.uuid4(),
            job_run_id=job_run_id,
            app=APP,
            policy=self.policy,
            target=self.target,
            retention_days=self.retention_days,
            cutoff_at=self.cutoff_at,
            window_start=self.window_start,
            window_end=self.window_end,
            rows_matched=self.rows_matched,
            rows_deleted=self.rows_deleted,
            batches=self.batches,
            batch_size=self.batch_size,
            dry_run=self.dry_run,
            status=self.status,
            detail=self.detail,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )


def _unconfigured(policy: str, target: str, clock: Clock, size: int) -> Outcome:
    """A completed pass that deleted nothing because there is no rule to apply.

    Recorded as `completed`, not `failed`: nothing went wrong. `retention_days=0` and
    `cutoff_at = now` say "the window covers nothing", and `detail.reason` names the
    missing ADR so the row is self-explaining a year later.
    """
    now = clock()
    return Outcome(
        policy=policy,
        target=target,
        retention_days=0,
        cutoff_at=now,
        started_at=now,
        finished_at=now,
        dry_run=True,
        batch_size=size,
        detail={"reason": UNCONFIGURED},
    )


# --- the clock a call is measured against -------------------------------------

#: When the call happened, falling back to when we took custody of it.
#:
#: `calls.recorded_at` is nullable -- the source prefix is an input uc3 does not own,
#: and a recording can arrive without a timestamp. Left as a bare column that would make
#: `recorded_at < cutoff` false for every NULL, so a recording with no stamp would be
#: immortal: exactly the row most likely to be mishandled, kept forever by an accident
#: of metadata. `ingested_at` is NOT NULL with a server default, so the coalesce always
#: yields a comparable instant and no row escapes the window by being incomplete.
CALL_CLOCK = func.coalesce(Call.recorded_at, Call.ingested_at)


async def _scan(
    factory: async_sessionmaker[AsyncSession],
    *,
    cutoff: datetime,
    after: tuple[datetime, uuid.UUID, int] | None,
    limit: int,
) -> list[tuple[uuid.UUID, int, datetime]]:
    """One batch of in-scope segments, oldest first, paged by keyset.

    `transcript_segments` has no timestamp of its own and a composite primary key
    `(call_id, seg_id)`, so the key is `(call clock, call_id, seg_id)`. Keyset rather
    than OFFSET for the same reason uc1 uses it: a dry run deletes nothing, so an
    offset-free LIMIT hands back the same batch for ever and the pass never terminates.
    """
    stmt = (
        select(TranscriptSegment.call_id, TranscriptSegment.seg_id, CALL_CLOCK)
        .join(Call, TranscriptSegment.call_id == Call.id)
        .where(CALL_CLOCK < cutoff)
        .order_by(CALL_CLOCK, TranscriptSegment.call_id, TranscriptSegment.seg_id)
        .limit(limit)
    )
    if after is not None:
        last_clock, last_call, last_seg = after
        stmt = stmt.where(
            or_(
                CALL_CLOCK > last_clock,
                and_(CALL_CLOCK == last_clock, TranscriptSegment.call_id > last_call),
                and_(
                    CALL_CLOCK == last_clock,
                    TranscriptSegment.call_id == last_call,
                    TranscriptSegment.seg_id > last_seg,
                ),
            )
        )
    async with factory() as db:
        rows = (await db.execute(stmt)).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def _delete(
    factory: async_sessionmaker[AsyncSession], keys: Sequence[tuple[uuid.UUID, int]]
) -> int:
    """Delete one batch in its own transaction; return what was really removed.

    `RETURNING` rather than arithmetic on the intended count: if an operator or a
    concurrent pass removed some of these rows first, the number written to the audit
    row has to be what this pass deleted.
    """
    if not keys:
        return 0
    stmt = (
        delete(TranscriptSegment)
        .where(
            or_(
                *[
                    and_(TranscriptSegment.call_id == call_id, TranscriptSegment.seg_id == seg_id)
                    for call_id, seg_id in keys
                ]
            )
        )
        .returning(TranscriptSegment.call_id)
    )
    async with factory() as db, db.begin():
        return len((await db.execute(stmt)).scalars().all())


async def purge_transcripts(
    factory: async_sessionmaker[AsyncSession],
    *,
    clock: Clock = utcnow,
    days: int | None = None,
    limit: int | None = None,
    dry_run: bool = True,
) -> Outcome:
    """Delete transcript segments for calls older than the configured window.

    `dry_run` defaults to True here, unlike uc1's equivalent. A caller that forgets to
    pass it rehearses; there is no signature in this module whose default deletes.
    """
    size = limit if limit is not None else batch_size()
    window = days if days is not None else retention_days()
    if window is None:
        return _unconfigured(TRANSCRIPT_POLICY, TRANSCRIPT_TARGET, clock, size)

    started = clock()
    cutoff = started - timedelta(days=window)
    outcome = Outcome(
        policy=TRANSCRIPT_POLICY,
        target=TRANSCRIPT_TARGET,
        retention_days=window,
        cutoff_at=cutoff,
        started_at=started,
        finished_at=started,
        dry_run=dry_run,
        batch_size=size,
    )
    try:
        after: tuple[datetime, uuid.UUID, int] | None = None
        while True:
            found = await _scan(factory, cutoff=cutoff, after=after, limit=size)
            if not found:
                break
            after = (found[-1][2], found[-1][0], found[-1][1])
            outcome.batches += 1
            outcome.rows_matched += len(found)
            outcome.saw([stamp for _, _, stamp in found])
            if not dry_run:
                outcome.rows_deleted += await _delete(
                    factory, [(call_id, seg_id) for call_id, seg_id, _ in found]
                )
    except Exception as exc:
        outcome.status = FAILED
        outcome.detail["error"] = type(exc).__name__
        log.exception("uc3 transcript retention pass failed after %d batches", outcome.batches)
    outcome.finished_at = clock()
    return outcome


# --- recordings: the store uc3 does not own -----------------------------------


@dataclass(frozen=True)
class Recording:
    key: str
    last_modified: datetime


class RecordingStore(Protocol):
    """The object store the source recordings live in."""

    def list_before(self, cutoff: datetime, *, limit: int) -> Iterable[Recording]: ...

    def remove(self, keys: Sequence[str]) -> int: ...


class UnownedRecordingStore:
    """The default: match nothing, delete nothing, and say why.

    uc3 ingests from a prefix it reads and does not write (`storage.fetch_object` is
    read-only; the only writer in `storage.py` loads test fixtures). The recordings are
    the business's system of record, and a surveillance tool deleting from the source of
    record on a schedule is a decision for the same ADR 0004 conversation that sets the
    window -- possibly a different answer, since the recording's own retention may be
    governed by a rule that has nothing to do with surveillance.

    So this default is not a stub standing in for missing code. It is the correct
    behaviour until somebody answers the question, and it is a class rather than a
    `None` so the policy still runs, still writes its audit row, and still says in that
    row that it was not authorised to act.
    """

    reason = "recordings are the source of record; deletion authority is not uc3's (ADR 0004)"

    def list_before(self, cutoff: datetime, *, limit: int) -> Iterable[Recording]:
        return ()

    def remove(self, keys: Sequence[str]) -> int:
        raise PermissionError(self.reason)


_STORE: RecordingStore = UnownedRecordingStore()


def set_recording_store(store: RecordingStore | None) -> None:
    """Install a store (a deployment with deletion authority, or a test double)."""
    global _STORE
    _STORE = store if store is not None else UnownedRecordingStore()


def recording_store() -> RecordingStore:
    return _STORE


async def purge_recordings(
    *,
    clock: Clock = utcnow,
    days: int | None = None,
    limit: int | None = None,
    dry_run: bool = True,
) -> Outcome:
    """Delete source recordings older than the window, if anything is authorised to."""
    size = limit if limit is not None else batch_size()
    window = days if days is not None else retention_days()
    if window is None:
        return _unconfigured(RECORDING_POLICY, RECORDING_TARGET, clock, size)

    started = clock()
    cutoff = started - timedelta(days=window)
    store = recording_store()
    outcome = Outcome(
        policy=RECORDING_POLICY,
        target=RECORDING_TARGET,
        retention_days=window,
        cutoff_at=cutoff,
        started_at=started,
        finished_at=started,
        dry_run=dry_run,
        batch_size=size,
    )
    if isinstance(store, UnownedRecordingStore):
        outcome.detail["reason"] = UnownedRecordingStore.reason
        outcome.dry_run = True
    try:
        found = list(store.list_before(cutoff, limit=size))
        outcome.batches = 1 if found else 0
        outcome.rows_matched = len(found)
        if len(found) >= size:
            # One batch, no paging: an object store's listing API is the store's to
            # page, not this function's, and a silent cap would make `rows_matched`
            # read as "this is all there was". Say it in the audit row instead, so the
            # next pass picking up the remainder is expected rather than surprising.
            outcome.detail["truncated"] = True
        outcome.saw([item.last_modified for item in found])
        if found and not outcome.dry_run:
            outcome.rows_deleted = store.remove([item.key for item in found])
    except Exception as exc:
        outcome.status = FAILED
        outcome.detail["error"] = type(exc).__name__
        log.exception("uc3 recording retention pass failed")
    outcome.finished_at = clock()
    return outcome


# --- the sweep ----------------------------------------------------------------


async def record(
    factory: async_sessionmaker[AsyncSession], outcome: Outcome, job_run_id: uuid.UUID
) -> uuid.UUID:
    """Write the audit row for one pass and return its id.

    `retention_deletions` is shared with uc1 and carries an `app` column, so uc3 needs
    no table of its own -- and a reviewer asking "did retention run last night" gets one
    place to look for both applications.
    """
    row = outcome.row(job_run_id)
    row_id = row.id
    async with factory() as db, db.begin():
        db.add(row)
    log.info(
        "uc3 retention %s: matched=%d deleted=%d batches=%d cutoff=%s dry_run=%s status=%s",
        outcome.policy,
        outcome.rows_matched,
        outcome.rows_deleted,
        outcome.batches,
        outcome.cutoff_at.isoformat(),
        outcome.dry_run,
        outcome.status,
    )
    return row_id


async def sweep(
    *,
    clock: Clock = utcnow,
    dry_run: bool | None = None,
    limit: int | None = None,
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict[str, Any]:
    """Run every uc3 retention policy once and log each pass.

    One `job_run_id` covers both policies, so the audit table shows a night's sweep
    rather than two rows that happen to share a timestamp. A policy that raises does not
    stop the other: its Outcome comes back `failed` and is logged as such, because "the
    transcript rule ran, the recording rule broke" is precisely the state a reviewer has
    to be able to see.
    """
    # A closed gate wins over an explicit argument. `sweep(dry_run=False)` -- and the
    # Celery task, which takes the same keyword -- must NOT be able to delete while
    # `UC3_RETENTION_ENABLED` is unset or no window is configured: an operator passing
    # a flag is not the person who read ADR 0004, and the two gates exist precisely so
    # that turning deletion on is a deliberate act rather than a call argument. So the
    # argument can only ever make a pass MORE cautious, never less.
    rehearse = dry_run_default() or (False if dry_run is None else dry_run)
    own_engine = None
    if factory is None:
        own_engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
        factory = async_sessionmaker(own_engine, expire_on_commit=False)
    job_run_id = uuid.uuid4()
    try:
        outcomes = [
            await purge_transcripts(factory, clock=clock, limit=limit, dry_run=rehearse),
            await purge_recordings(clock=clock, limit=limit, dry_run=rehearse),
        ]
        logged = [str(await record(factory, outcome, job_run_id)) for outcome in outcomes]
    finally:
        if own_engine is not None:
            await own_engine.dispose()
    return {
        "job_run_id": str(job_run_id),
        "dry_run": rehearse,
        "enabled": deletion_enabled(),
        "retention_days": retention_days(),
        "log_ids": logged,
        "policies": [
            {
                "policy": o.policy,
                "target": o.target,
                "retention_days": o.retention_days,
                "cutoff_at": o.cutoff_at.isoformat(),
                "rows_matched": o.rows_matched,
                "rows_deleted": o.rows_deleted,
                "batches": o.batches,
                "dry_run": o.dry_run,
                "status": o.status,
                "detail": o.detail,
            }
            for o in outcomes
        ],
    }


# --- Celery task and beat schedule -------------------------------------------

# A sweep is idempotent and cheap to repeat -- anything it missed is still over the
# window on the next attempt -- so it retries on transient faults and on nothing else.
# A bug is not retried sixteen times into the log.
RETRY_ON = (OSError, TimeoutError, DBAPIError)
TASK = {
    "autoretry_for": RETRY_ON,
    # Its own dict, separate from the app module's: a retention sweep must carry the
    # spend boundary too, and defining the options here meant it silently did not.
    "base": BudgetAwareTask,
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": 3,
}


@celery_app.task(name="uc3.retention_sweep", **TASK)
def retention_sweep(dry_run: bool | None = None) -> dict[str, Any]:
    """Nightly entry point. Celery workers are synchronous."""
    return asyncio.run(sweep(dry_run=dry_run))


# 04:00 UTC: after uc3's own 02:00 ingest sweep has finished writing the rows this pass
# measures, before uc3's 05:00 chain verify (so a night's deletions are inside the walk
# that follows them), and clear of uc1's 03:30 so two applications do not open write
# transactions on the same Postgres in the same minute.
BEAT_SCHEDULE = {
    "uc3-retention-sweep": {"task": "uc3.retention_sweep", "schedule": crontab(hour=4, minute=0)},
}
# Assigned, not mutated in place: `conf.beat_schedule` falls through to Celery's shared
# default dict when the app has never set one, and `.update()` on that would edit the
# default for every app in the process.
celery_app.conf.beat_schedule = {**celery_app.conf.beat_schedule, **BEAT_SCHEDULE}
celery_app.conf.timezone = "UTC"
