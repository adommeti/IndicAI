"""Delete what PRD C8 says must not be kept, and write down that it happened.

C8: "Audio retained 30 days, transcripts 90 days, then deleted by a scheduled
job; deletions logged." This module is that job -- one nightly Celery beat sweep
that runs each policy in turn and writes a `retention_deletions` row per policy
per pass (migration 0012).

**The audio policy matches nothing today, and that is a real state, not a stub.**
uc1 keeps no audio at rest: `voice_pipeline.py` holds PCM in a bounded pre-roll
ring buffer for the duration of an utterance, there is no LiveKit egress or
recording service in `docker-compose.yml`, and the helpdesk app uses no MinIO
bucket (README, "Data flow and retention"). So there is no store for a 30-day
rule to sweep, and inventing one -- deleting from an empty bucket and calling
the criterion green -- would be a control that has never once been executed
reporting that it works. What is built instead is the policy around a sink:
`AudioSink` is the two-method interface a recording store has to offer,
`purge_audio` implements the 30-day rule against any sink, and the sink uc1
resolves today is `NoAudioSink`, which lists nothing. Its log row is honest
about why: `status=completed`, `rows_matched=0`, and
`detail={"sink": "none", "reason": ...}`. The moment an egress writes audio
somewhere, `set_audio_sink(...)` at worker start-up is the whole change, and the
policy, the batching and the logging are already tested (with a fake sink, in
`platform/tests/test_uc1_retention.py`).

**Deletes by policy, never TRUNCATE** (`.claude/rules/migrations.md`). The
transcript policy is scoped twice: to turns whose session belongs to *this* app,
because `turns` is a shared platform table and uc1's retention rule has no
authority over another application's rows, and to rows created strictly before
the cutoff.

**Batched, with a transaction per batch.** A backlog -- the first run after the
policy is switched on, or the first run after an outage -- can be arbitrarily
large. One `DELETE` over all of it holds a write transaction open for as long as
it takes, pins the autovacuum horizon behind it, and rolls back every row if it
is interrupted at the end. Each batch is instead selected by keyset
(`(created_at, id)`), deleted by id in its own transaction, and counted from the
`RETURNING` clause, so what is recorded is what this pass actually removed even
if another worker removed some of it first. Progress from a killed run is kept.

**Dry run rehearses.** `dry_run=True` scans exactly the same scope, reports what
it would delete, deletes nothing, and still writes its log row with `dry_run`
set -- the check constraint `ck_retention_deletions_dry_run` refuses any row
claiming a dry run deleted something. That is how a retention window is changed
safely: rehearse, read the count and the covered window, then run it.

Configuration (all optional; the defaults are the PRD's numbers):

| Variable | Default | Meaning |
|---|---|---|
| `UC1_AUDIO_RETENTION_DAYS` | 30 | age at which recorded audio is deleted |
| `UC1_TRANSCRIPT_RETENTION_DAYS` | 90 | age at which `turns` rows are deleted |
| `UC1_RETENTION_BATCH_SIZE` | 500 | rows per batch, i.e. per transaction |
| `UC1_RETENTION_DRY_RUN` | false | deploy the beat in rehearsal mode |

The beat entry is declared here, on the Celery app `ticketing.py` already
creates for this application, so uc1 has one broker, one worker and one beat
schedule rather than two. Registering the task and the schedule is this module's
import: a worker must load it (`celery -A helpdesk_agent.retention worker -B`,
which imports `ticketing` transitively and registers `uc1.file_ticket` as well).

Out of scope, deliberately and visibly: `sessions` rows (metadata, and the
parent of `ticket_filings` and `adapter_calls` foreign keys), `adapter_calls`
(cost and latency metadata, no transcript text), and `ticket_filings.payload`,
which holds the ticket body an employee's description became. That last one is
transcript-derived text outliving the 90-day rule; it is recorded in the
hand-back rather than swept here, because a ticket is an operational record in
Zammad's lifecycle and deleting half of it on a transcript rule would be a
policy decision the PRD has not made.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from celery.schedules import crontab
from indic_platform.db.models import RetentionDeletion, Session, Turn
from indic_platform.tasks import BudgetAwareTask
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from helpdesk_agent.ticketing import celery_app

log = logging.getLogger(__name__)

APP = "helpdesk_agent"
AUDIO_POLICY = "uc1_audio_30d"
TRANSCRIPT_POLICY = "uc1_transcripts_90d"
TRANSCRIPT_TARGET = "turns"
COMPLETED = "completed"
FAILED = "failed"

DEFAULT_AUDIO_DAYS = 30
DEFAULT_TRANSCRIPT_DAYS = 90
DEFAULT_BATCH_SIZE = 500

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _int_env(name: str, default: int) -> int:
    """A positive integer from the environment, or the default.

    A malformed or non-positive value is refused rather than coerced: a typo
    that silently became a 0-day retention window would delete everything the
    first time the beat fired.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def audio_days() -> int:
    return _int_env("UC1_AUDIO_RETENTION_DAYS", DEFAULT_AUDIO_DAYS)


def transcript_days() -> int:
    return _int_env("UC1_TRANSCRIPT_RETENTION_DAYS", DEFAULT_TRANSCRIPT_DAYS)


def batch_size() -> int:
    return _int_env("UC1_RETENTION_BATCH_SIZE", DEFAULT_BATCH_SIZE)


def dry_run_default() -> bool:
    return os.environ.get("UC1_RETENTION_DRY_RUN", "false").strip().lower() == "true"


# --- what one policy pass produced -------------------------------------------


@dataclass
class Outcome:
    """The `retention_deletions` row a policy pass earned, before it is written.

    A dataclass rather than the ORM object so a pass can be reasoned about (and
    tested) without a database, and so the failure path has something to report
    when the database is the thing that failed.
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
        """Widen the covered window by the timestamps of one batch."""
        if not stamps:
            return
        low, high = min(stamps), max(stamps)
        self.window_start = low if self.window_start is None else min(self.window_start, low)
        self.window_end = high if self.window_end is None else max(self.window_end, high)

    def row(self, job_run_id: uuid.UUID) -> RetentionDeletion:
        return RetentionDeletion(
            # Minted here rather than left to the column default: `record`
            # returns this id, and reading it back off a committed instance
            # would depend on the caller's `expire_on_commit`.
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


# --- audio: the policy, and the sink uc1 does not have yet --------------------


@dataclass(frozen=True)
class AudioObject:
    """One stored recording: the key to delete it by, and when it was recorded."""

    key: str
    recorded_at: datetime


class AudioSink(Protocol):
    """What a recording store has to offer for the 30-day rule to run over it.

    Two methods, both async, because the implementation that will satisfy this
    is a MinIO/S3 client whose calls block; an adapter wraps them in
    `asyncio.to_thread` rather than stalling the worker's loop.

    `older_than` pages by key (`after`), not by offset, for the same reason the
    transcript scan pages by keyset: the listing shrinks underneath the caller
    as the caller deletes from it.
    """

    name: str

    async def older_than(
        self, cutoff: datetime, *, after: str | None, limit: int
    ) -> list[AudioObject]: ...

    async def delete(self, keys: Sequence[str]) -> list[str]: ...


class NoAudioSink:
    """uc1's audio store today: there isn't one, so nothing is ever matched.

    Not a placeholder for a store that exists and is unimplemented -- a truthful
    description of the deployment. Voice audio lives in the agent process for
    the length of an utterance and is never written down, so a 30-day sweep has
    nothing to sweep. Every call here returns empty, and `purge_audio` records
    *why* in the log row so a reviewer reading "0 rows" a year later can tell
    this apart from a job that was never deployed.
    """

    name = "none"
    reason = "no recording sink is configured; uc1 stores no audio at rest"

    async def older_than(
        self, cutoff: datetime, *, after: str | None, limit: int
    ) -> list[AudioObject]:
        return []

    async def delete(self, keys: Sequence[str]) -> list[str]:
        return []


NO_AUDIO_SINK = NoAudioSink()
_sink: AudioSink | None = None


def set_audio_sink(sink: AudioSink | None) -> None:
    """Point the 30-day rule at a recording store. Call it at worker start-up."""
    global _sink
    _sink = sink


def audio_sink() -> AudioSink:
    return _sink if _sink is not None else NO_AUDIO_SINK


async def purge_audio(
    *,
    clock: Clock = utcnow,
    days: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    sink: AudioSink | None = None,
) -> Outcome:
    """Delete recordings older than the window from the configured sink."""
    store = sink if sink is not None else audio_sink()
    window_days = days if days is not None else audio_days()
    size = limit if limit is not None else batch_size()
    started = clock()
    cutoff = started - timedelta(days=window_days)
    outcome = Outcome(
        policy=AUDIO_POLICY,
        target=f"audio:{store.name}",
        retention_days=window_days,
        cutoff_at=cutoff,
        started_at=started,
        finished_at=started,
        dry_run=dry_run,
        batch_size=size,
        detail={"sink": store.name},
    )
    if isinstance(store, NoAudioSink):
        outcome.detail["reason"] = store.reason
    try:
        after: str | None = None
        while True:
            found = await store.older_than(cutoff, after=after, limit=size)
            if not found:
                break
            after = found[-1].key
            outcome.batches += 1
            outcome.rows_matched += len(found)
            outcome.saw([obj.recorded_at for obj in found])
            if not dry_run:
                gone = await store.delete([obj.key for obj in found])
                outcome.rows_deleted += len(gone)
    except Exception as exc:  # the pass failed; what it already deleted stands
        outcome.status = FAILED
        outcome.detail["error"] = type(exc).__name__
        log.exception("uc1 audio retention pass failed after %d batches", outcome.batches)
    outcome.finished_at = clock()
    return outcome


# --- transcripts: `turns` rows belonging to this app --------------------------


async def _scan(
    factory: async_sessionmaker[AsyncSession],
    *,
    cutoff: datetime,
    after: tuple[datetime, uuid.UUID] | None,
    limit: int,
) -> list[tuple[uuid.UUID, datetime]]:
    """One batch of in-scope turns, oldest first, paged by keyset.

    `(created_at, id)` rather than OFFSET: a dry run deletes nothing, so an
    offset-free `LIMIT` would hand back the same batch forever, and a real run
    shifts every offset underneath itself as it deletes.
    """
    stmt = (
        select(Turn.id, Turn.created_at)
        .join(Session, Turn.session_id == Session.id)
        .where(Session.app == APP, Turn.created_at < cutoff)
        .order_by(Turn.created_at, Turn.id)
        .limit(limit)
    )
    if after is not None:
        last_created, last_id = after
        stmt = stmt.where(
            or_(
                Turn.created_at > last_created,
                and_(Turn.created_at == last_created, Turn.id > last_id),
            )
        )
    async with factory() as db:
        rows = (await db.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


async def _delete(
    factory: async_sessionmaker[AsyncSession], ids: Sequence[uuid.UUID]
) -> list[datetime]:
    """Delete one batch in its own transaction; return what was really removed.

    `RETURNING` rather than `rowcount` arithmetic: if a concurrent pass (or an
    operator) removed some of these rows first, the count written to the audit
    row has to be what *this* pass deleted, not what it intended to.
    """
    stmt = delete(Turn).where(Turn.id.in_(list(ids))).returning(Turn.created_at)
    async with factory() as db, db.begin():
        return list((await db.execute(stmt)).scalars().all())


async def purge_transcripts(
    factory: async_sessionmaker[AsyncSession],
    *,
    clock: Clock = utcnow,
    days: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
) -> Outcome:
    """Delete this app's `turns` rows older than the transcript window."""
    window_days = days if days is not None else transcript_days()
    size = limit if limit is not None else batch_size()
    started = clock()
    cutoff = started - timedelta(days=window_days)
    outcome = Outcome(
        policy=TRANSCRIPT_POLICY,
        target=TRANSCRIPT_TARGET,
        retention_days=window_days,
        cutoff_at=cutoff,
        started_at=started,
        finished_at=started,
        dry_run=dry_run,
        batch_size=size,
    )
    try:
        after: tuple[datetime, uuid.UUID] | None = None
        while True:
            found = await _scan(factory, cutoff=cutoff, after=after, limit=size)
            if not found:
                break
            after = (found[-1][1], found[-1][0])
            outcome.batches += 1
            outcome.rows_matched += len(found)
            outcome.saw([created for _, created in found])
            if not dry_run:
                gone = await _delete(factory, [turn_id for turn_id, _ in found])
                outcome.rows_deleted += len(gone)
    except Exception as exc:
        outcome.status = FAILED
        outcome.detail["error"] = type(exc).__name__
        log.exception("uc1 transcript retention pass failed after %d batches", outcome.batches)
    outcome.finished_at = clock()
    return outcome


# --- the sweep ----------------------------------------------------------------


async def record(
    factory: async_sessionmaker[AsyncSession], outcome: Outcome, job_run_id: uuid.UUID
) -> uuid.UUID:
    """Write the audit row for one pass and return its id."""
    row = outcome.row(job_run_id)
    row_id = row.id
    async with factory() as db, db.begin():
        db.add(row)
    log.info(
        "uc1 retention %s: matched=%d deleted=%d batches=%d cutoff=%s dry_run=%s status=%s",
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
    """Run every uc1 retention policy once and log each pass.

    One `job_run_id` covers both policies, so the audit table shows a night's
    sweep rather than two rows that happen to share a timestamp. A policy that
    raises does not stop the other: its `Outcome` comes back `failed` and is
    logged as such, because "the transcript rule ran, the audio rule broke" is
    precisely the state a reviewer must be able to see.
    """
    rehearse = dry_run_default() if dry_run is None else dry_run
    own_engine = None
    if factory is None:
        own_engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
        factory = async_sessionmaker(own_engine, expire_on_commit=False)
    job_run_id = uuid.uuid4()
    try:
        outcomes = [
            await purge_audio(clock=clock, limit=limit, dry_run=rehearse),
            await purge_transcripts(factory, clock=clock, limit=limit, dry_run=rehearse),
        ]
        logged = [str(await record(factory, outcome, job_run_id)) for outcome in outcomes]
    finally:
        if own_engine is not None:
            await own_engine.dispose()
    return {
        "job_run_id": str(job_run_id),
        "dry_run": rehearse,
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
                "status": o.status,
            }
            for o in outcomes
        ],
    }


# --- Celery task and beat schedule -------------------------------------------

# A retention sweep is idempotent and cheap to repeat -- anything it missed is
# still over the window on the next attempt -- so it retries on the faults that
# are transient (a database that was restarting) and on nothing else. A bug is
# not retried sixteen times into the log.
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


@celery_app.task(name="uc1.retention_sweep", **TASK)
def retention_sweep(dry_run: bool | None = None) -> dict[str, Any]:
    """Nightly entry point. Celery workers are synchronous."""
    return asyncio.run(sweep(dry_run=dry_run))


# 03:30 UTC: after the working day the turns came from, and offset from uc3's
# 02:00 sweep so two applications do not open write transactions on the same
# Postgres at the same minute.
BEAT_SCHEDULE = {
    "uc1-retention-sweep": {"task": "uc1.retention_sweep", "schedule": crontab(hour=3, minute=30)},
}
# Assigned, not mutated in place: `conf.beat_schedule` falls through to Celery's
# shared default dict when the app has never set one, and `.update()` on that
# would edit the default for every app in the process.
celery_app.conf.beat_schedule = {**celery_app.conf.beat_schedule, **BEAT_SCHEDULE}
celery_app.conf.timezone = "UTC"
