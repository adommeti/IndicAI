"""PRD C8's retention rules: what they delete, what they refuse to delete, what they log.

Time is frozen throughout. Every entry point takes a `clock`, and these tests
pass one that returns a fixed instant, so the cutoff is an exact timestamp
rather than "about ninety days ago" -- which is the only way the boundary cases
below (one second either side of the window) mean anything.

The frozen instant sits in **2021**, far outside the range any other suite
writes into, and every fixture row carries `trace_id == FIXTURE`. The database
these integration tests run against is shared, and a retention job is a
*global* delete by construction: it removes every helpdesk turn older than the
cutoff, not just this file's. Both precautions exist so that the count and
window assertions describe rows this file created and nothing else -- an
absolute count over `turns`, or a window overlapping the present, would be a
different suite's failure waiting to happen.

The audio half is tested twice over, for a reason worth stating plainly:

1. `purge_audio` against a `FakeAudioSink` proves the 30-day rule, the keyset
   paging, the batching, the dry run and the failure path all work.
2. `test_the_audio_sink_uc1_has_today_matches_nothing_and_records_why` proves
   that the sink uc1 actually resolves is `NoAudioSink` and that a real pass
   over it matches zero objects.

Test 2 is not a formality. uc1 stores no audio at rest -- the voice pipeline
holds PCM in memory for the utterance, there is no LiveKit egress and the app
uses no bucket -- so the 30-day rule has nothing to act on today, and a test
suite that showed only test 1 would read as if it did. The criterion is
implemented and unexercised in production until a recording sink exists; these
two tests are what make that distinction visible instead of buried.
"""

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from celery.schedules import crontab
from helpdesk_agent import retention
from helpdesk_agent.retention import (
    AUDIO_POLICY,
    TRANSCRIPT_POLICY,
    AudioObject,
    NoAudioSink,
    Outcome,
    purge_audio,
    purge_transcripts,
    record,
    sweep,
)
from indic_platform.db.models import RetentionDeletion, Turn
from indic_platform.db.models import Session as SessionRow
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

FIXTURE = "uc1-retention-test"
# A Tuesday at 03:30 UTC -- the minute the beat entry fires -- in a year no
# other suite writes rows into.
FROZEN = datetime(2021, 6, 15, 3, 30, tzinfo=UTC)
TRANSCRIPT_CUTOFF = FROZEN - timedelta(days=90)
AUDIO_CUTOFF = FROZEN - timedelta(days=30)
SECOND = timedelta(seconds=1)


def frozen(at: datetime = FROZEN) -> retention.Clock:
    """A clock that does not move. `freezegun` is not a dependency of this repo."""
    return lambda: at


# --- windows come from the environment, and refuse nonsense -------------------


def test_the_default_windows_are_the_prds_thirty_and_ninety_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UC1_AUDIO_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("UC1_TRANSCRIPT_RETENTION_DAYS", raising=False)
    monkeypatch.delenv("UC1_RETENTION_BATCH_SIZE", raising=False)
    assert retention.audio_days() == 30
    assert retention.transcript_days() == 90
    assert retention.batch_size() == 500


def test_a_window_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UC1_TRANSCRIPT_RETENTION_DAYS", "45")
    monkeypatch.setenv("UC1_AUDIO_RETENTION_DAYS", "7")
    assert retention.transcript_days() == 45
    assert retention.audio_days() == 7


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_non_positive_window_is_refused_rather_than_deleting_everything(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A zero-day window makes the cutoff `now`, which is every row there is."""
    monkeypatch.setenv("UC1_TRANSCRIPT_RETENTION_DAYS", bad)
    with pytest.raises(ValueError):
        retention.transcript_days()


def test_dry_run_can_be_switched_on_for_a_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UC1_RETENTION_DRY_RUN", raising=False)
    assert retention.dry_run_default() is False
    monkeypatch.setenv("UC1_RETENTION_DRY_RUN", "true")
    assert retention.dry_run_default() is True


# --- the audio policy, against a sink that has something in it ----------------


@dataclass
class FakeAudioSink:
    """A recording store with objects in it: the sink uc1 does not have.

    Deliberately shaped like an object store rather than a table -- listing is
    ordered by key and paged by "after this key", because that is what S3/MinIO
    offers and what `AudioSink` therefore asks for.
    """

    objects: list[AudioObject]
    name: str = "fake"

    def __post_init__(self) -> None:
        self.deleted: list[str] = []
        self.pages: list[int] = []

    async def older_than(
        self, cutoff: datetime, *, after: str | None, limit: int
    ) -> list[AudioObject]:
        found = sorted(
            (
                obj
                for obj in self.objects
                if obj.recorded_at < cutoff and (after is None or obj.key > after)
            ),
            key=lambda obj: obj.key,
        )
        page = found[:limit]
        self.pages.append(len(page))
        return page

    async def delete(self, keys: Sequence[str]) -> list[str]:
        doomed = set(keys)
        self.objects = [obj for obj in self.objects if obj.key not in doomed]
        self.deleted.extend(keys)
        return list(keys)


def _objects(*offsets: timedelta) -> list[AudioObject]:
    return [
        AudioObject(key=f"rec/{i:03d}.wav", recorded_at=AUDIO_CUTOFF + off)
        for i, off in enumerate(offsets)
    ]


async def test_audio_one_second_over_the_window_goes_and_one_second_under_it_stays() -> None:
    sink = FakeAudioSink(objects=_objects(-SECOND, SECOND, timedelta(0)))
    outcome = await purge_audio(clock=frozen(), days=30, limit=10, sink=sink)

    assert outcome.cutoff_at == AUDIO_CUTOFF
    assert outcome.rows_matched == 1
    assert outcome.rows_deleted == 1
    assert sink.deleted == ["rec/000.wav"]
    # The row exactly on the cutoff is not yet older than the window.
    assert sorted(obj.key for obj in sink.objects) == ["rec/001.wav", "rec/002.wav"]


async def test_audio_deletions_are_batched_across_more_objects_than_one_batch() -> None:
    sink = FakeAudioSink(objects=_objects(*[-SECOND * (i + 1) for i in range(7)]))
    outcome = await purge_audio(clock=frozen(), days=30, limit=3, sink=sink)

    assert (outcome.rows_matched, outcome.rows_deleted) == (7, 7)
    assert outcome.batches == 3
    assert sink.pages == [3, 3, 1, 0]
    assert sink.objects == []


async def test_audio_dry_run_reports_what_it_would_delete_and_deletes_nothing() -> None:
    sink = FakeAudioSink(objects=_objects(*[-SECOND * (i + 1) for i in range(5)]))
    outcome = await purge_audio(clock=frozen(), days=30, limit=2, dry_run=True, sink=sink)

    assert outcome.dry_run is True
    assert outcome.rows_matched == 5
    assert outcome.rows_deleted == 0
    assert sink.deleted == []
    assert len(sink.objects) == 5
    # Paging by key, not by offset: a dry run removes nothing, so an offset-free
    # LIMIT would hand back the same two objects forever.
    assert sink.pages == [2, 2, 1, 0]


async def test_a_failed_audio_pass_is_recorded_as_failed_not_as_a_clean_zero() -> None:
    class Broken(FakeAudioSink):
        async def older_than(
            self, cutoff: datetime, *, after: str | None, limit: int
        ) -> list[AudioObject]:
            raise OSError("bucket unreachable")

    outcome = await purge_audio(clock=frozen(), days=30, sink=Broken(objects=[]))
    assert outcome.status == "failed"
    assert outcome.detail["error"] == "OSError"


async def test_the_audio_sink_uc1_has_today_matches_nothing_and_records_why() -> None:
    """The honest state of the 30-day rule: implemented, and matching nothing.

    uc1 writes no audio anywhere -- `voice_pipeline.py` keeps PCM in a bounded
    in-memory pre-roll for the utterance, `docker-compose.yml` runs no LiveKit
    egress or recording service, and the helpdesk app uses no MinIO bucket. So
    the sink that resolves is `NoAudioSink` and a pass over it can only ever
    report zero. This test exists so that zero is read as "there is nothing to
    delete", which the log row says in as many words, rather than as "the
    deletion ran and found nothing", which would be a claim about a store that
    does not exist.
    """
    assert isinstance(retention.audio_sink(), NoAudioSink)

    outcome = await purge_audio(clock=frozen(), days=30)

    assert outcome.policy == AUDIO_POLICY
    assert outcome.target == "audio:none"
    assert (outcome.rows_matched, outcome.rows_deleted, outcome.batches) == (0, 0, 0)
    assert outcome.status == "completed"
    assert outcome.detail == {"sink": "none", "reason": NoAudioSink.reason}
    # Nothing matched, so there is no window to claim one was covered.
    assert (outcome.window_start, outcome.window_end) == (None, None)
    print(f"audio policy today: {outcome.detail['reason']} -> matched 0")


async def test_a_sink_can_be_installed_the_moment_one_exists() -> None:
    """`set_audio_sink` is the whole change when a recording store appears."""
    sink = FakeAudioSink(objects=_objects(-SECOND))
    try:
        retention.set_audio_sink(sink)
        outcome = await purge_audio(clock=frozen(), days=30, limit=10)
    finally:
        retention.set_audio_sink(None)
    assert outcome.rows_deleted == 1
    assert isinstance(retention.audio_sink(), NoAudioSink)


# --- the window bookkeeping ---------------------------------------------------


def test_the_covered_window_widens_over_batches_and_never_runs_backwards() -> None:
    outcome = Outcome(
        policy="p",
        target="t",
        retention_days=90,
        cutoff_at=FROZEN,
        started_at=FROZEN,
        finished_at=FROZEN,
        dry_run=False,
        batch_size=2,
    )
    outcome.saw([FROZEN - timedelta(days=100), FROZEN - timedelta(days=95)])
    outcome.saw([FROZEN - timedelta(days=120)])
    outcome.saw([])
    assert outcome.window_start == FROZEN - timedelta(days=120)
    assert outcome.window_end == FROZEN - timedelta(days=95)


# --- the beat schedule --------------------------------------------------------


def test_the_sweep_is_on_the_apps_beat_schedule_beside_the_ticketing_task() -> None:
    """One Celery app for uc1: importing this module registers task and schedule."""
    from helpdesk_agent.ticketing import celery_app

    entry = celery_app.conf.beat_schedule["uc1-retention-sweep"]
    assert entry["task"] == "uc1.retention_sweep"
    assert entry["schedule"] == crontab(hour=3, minute=30)
    assert "uc1.retention_sweep" in celery_app.tasks
    # The app's other task is still there: the schedule was merged, not replaced.
    assert "uc1.file_ticket" in celery_app.tasks


# --- the database half --------------------------------------------------------


def _database_url() -> str:
    """The stack's database, or a skip.

    CI's integration job provisions Postgres and fails on any skip, so with
    DATABASE_URL set an unreachable server is a failure, never a skip.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")
    return url


async def _clear(factory: async_sessionmaker[AsyncSession], sessions: Sequence[uuid.UUID]) -> None:
    """Remove this file's rows -- its own, and any a killed earlier run left.

    Scoped to `trace_id == FIXTURE`, so it can never touch another suite's
    turns. Run before each case as well as after, because the point of the 2021
    window is that only this file's rows are in it.
    """
    async with factory() as db, db.begin():
        await db.execute(delete(Turn).where(Turn.trace_id == FIXTURE))
        if sessions:
            await db.execute(delete(SessionRow).where(SessionRow.id.in_(list(sessions))))


@asynccontextmanager
async def _db() -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], list[uuid.UUID]]]:
    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    made: list[uuid.UUID] = []
    try:
        await _clear(factory, ())
        yield factory, made
    finally:
        await _clear(factory, made)
        await engine.dispose()


async def _turns(
    factory: async_sessionmaker[AsyncSession],
    made: list[uuid.UUID],
    stamps: Sequence[datetime],
    *,
    app: str = "helpdesk_agent",
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """One session of the given app, with a turn at each timestamp."""
    session_id = uuid.uuid4()
    made.append(session_id)
    turn_ids = [uuid.uuid4() for _ in stamps]
    async with factory() as db, db.begin():
        db.add(SessionRow(id=session_id, app=app, metadata_json={"employee_id": FIXTURE}))
        for turn_id, at in zip(turn_ids, stamps, strict=True):
            db.add(
                Turn(
                    id=turn_id,
                    session_id=session_id,
                    utterance="my vpn is down",
                    language="en-IN",
                    decision_json={"action": "answer"},
                    retrieval_json={"chunks": []},
                    latency_ms={"total": 1.0},
                    policy_version="test",
                    prompt_version="test",
                    model="test",
                    trace_id=FIXTURE,
                    created_at=at,
                )
            )
    return session_id, turn_ids


async def _alive(
    factory: async_sessionmaker[AsyncSession], turn_ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of *these* turns still exist. Never a count over the table."""
    async with factory() as db:
        rows = await db.scalars(select(Turn.id).where(Turn.id.in_(list(turn_ids))))
    return set(rows.all())


async def _logged(
    factory: async_sessionmaker[AsyncSession], job_run_id: uuid.UUID, policy: str
) -> RetentionDeletion:
    async with factory() as db:
        row = await db.scalar(
            select(RetentionDeletion).where(
                RetentionDeletion.job_run_id == job_run_id, RetentionDeletion.policy == policy
            )
        )
    assert row is not None, f"no {policy} row logged for run {job_run_id}"
    return row


@pytest.mark.integration
async def test_a_turn_a_second_older_than_ninety_days_goes_and_a_second_younger_stays() -> None:
    """The boundary, exactly: `created_at < cutoff`, with a frozen clock."""
    async with _db() as (factory, made):
        _, (older, on_it, younger) = await _turns(
            factory,
            made,
            [TRANSCRIPT_CUTOFF - SECOND, TRANSCRIPT_CUTOFF, TRANSCRIPT_CUTOFF + SECOND],
        )

        outcome = await purge_transcripts(factory, clock=frozen(), days=90, limit=100)

        assert outcome.cutoff_at == TRANSCRIPT_CUTOFF
        assert outcome.rows_matched == 1
        assert outcome.rows_deleted == 1
        assert await _alive(factory, [older, on_it, younger]) == {on_it, younger}
        print(f"transcripts: cutoff={outcome.cutoff_at.isoformat()} deleted={outcome.rows_deleted}")


@pytest.mark.integration
async def test_the_log_row_records_the_count_the_window_and_the_rule() -> None:
    """What a reviewer reads a year later, and what it has to be able to prove."""
    async with _db() as (factory, made):
        oldest = TRANSCRIPT_CUTOFF - timedelta(days=30)
        newest = TRANSCRIPT_CUTOFF - SECOND
        _, doomed = await _turns(
            factory, made, [oldest, TRANSCRIPT_CUTOFF - timedelta(days=7), newest]
        )
        _, kept = await _turns(factory, made, [TRANSCRIPT_CUTOFF + SECOND])

        job_run_id = uuid.uuid4()
        outcome = await purge_transcripts(factory, clock=frozen(), days=90, limit=100)
        log_id = await record(factory, outcome, job_run_id)

        row = await _logged(factory, job_run_id, TRANSCRIPT_POLICY)
        assert row.id == log_id
        assert row.app == "helpdesk_agent"
        assert row.target == "turns"
        assert row.retention_days == 90
        assert row.cutoff_at == TRANSCRIPT_CUTOFF
        assert row.rows_matched == 3
        assert row.rows_deleted == 3
        assert row.window_start == oldest
        assert row.window_end == newest
        assert row.dry_run is False
        assert row.status == "completed"
        # The rule is reconstructible from the row alone.
        assert row.started_at - timedelta(days=row.retention_days) == row.cutoff_at
        assert await _alive(factory, doomed) == set()
        assert await _alive(factory, kept) == set(kept)
        print(
            f"logged: {row.rows_deleted} rows over "
            f"[{row.window_start.isoformat()}, {row.window_end.isoformat()}]"
            if row.window_start and row.window_end
            else "logged"
        )


@pytest.mark.integration
async def test_a_dry_run_deletes_nothing_and_still_writes_its_log_row() -> None:
    async with _db() as (factory, made):
        _, doomed = await _turns(
            factory, made, [TRANSCRIPT_CUTOFF - SECOND, TRANSCRIPT_CUTOFF - timedelta(days=1)]
        )

        job_run_id = uuid.uuid4()
        outcome = await purge_transcripts(factory, clock=frozen(), days=90, limit=100, dry_run=True)
        await record(factory, outcome, job_run_id)

        row = await _logged(factory, job_run_id, TRANSCRIPT_POLICY)
        assert row.dry_run is True
        assert row.rows_matched == 2
        assert row.rows_deleted == 0
        assert row.window_start == TRANSCRIPT_CUTOFF - timedelta(days=1)
        assert row.window_end == TRANSCRIPT_CUTOFF - SECOND
        assert await _alive(factory, doomed) == set(doomed)
        print(f"dry run: would delete {row.rows_matched}, deleted {row.rows_deleted}")


@pytest.mark.integration
async def test_a_backlog_larger_than_one_batch_is_deleted_a_batch_at_a_time() -> None:
    """Seven rows, three per batch: three batches, seven deletions, no long transaction."""
    async with _db() as (factory, made):
        stamps = [TRANSCRIPT_CUTOFF - timedelta(days=i + 1) for i in range(7)]
        _, doomed = await _turns(factory, made, stamps)

        outcome = await purge_transcripts(factory, clock=frozen(), days=90, limit=3)

        assert outcome.batches == 3
        assert (outcome.rows_matched, outcome.rows_deleted) == (7, 7)
        assert outcome.window_start == min(stamps)
        assert outcome.window_end == max(stamps)
        assert await _alive(factory, doomed) == set()
        print(f"batching: {outcome.rows_deleted} rows in {outcome.batches} batches of 3")


@pytest.mark.integration
async def test_another_apps_turns_are_not_this_policys_to_delete() -> None:
    """`turns` is shared. uc1's rule has no authority over uc2's rows."""
    async with _db() as (factory, made):
        _, mine = await _turns(factory, made, [TRANSCRIPT_CUTOFF - SECOND])
        _, theirs = await _turns(
            factory, made, [TRANSCRIPT_CUTOFF - SECOND], app="training_localizer"
        )

        outcome = await purge_transcripts(factory, clock=frozen(), days=90, limit=100)

        assert outcome.rows_matched == 1
        assert await _alive(factory, mine) == set()
        assert await _alive(factory, theirs) == set(theirs)


@pytest.mark.integration
async def test_one_sweep_logs_both_policies_under_one_job_run_id() -> None:
    """The nightly pass: audio (nothing to delete) and transcripts, together."""
    async with _db() as (factory, made):
        _, doomed = await _turns(factory, made, [TRANSCRIPT_CUTOFF - SECOND])

        result = await sweep(clock=frozen(), dry_run=False, limit=100, factory=factory)

        job_run_id = uuid.UUID(result["job_run_id"])
        audio = await _logged(factory, job_run_id, AUDIO_POLICY)
        transcripts = await _logged(factory, job_run_id, TRANSCRIPT_POLICY)

        assert audio.retention_days == 30
        assert audio.rows_matched == 0
        assert audio.detail["reason"] == NoAudioSink.reason
        assert transcripts.retention_days == 90
        assert transcripts.rows_deleted == 1
        assert await _alive(factory, doomed) == set()
        assert [p["policy"] for p in result["policies"]] == [AUDIO_POLICY, TRANSCRIPT_POLICY]
        print(f"sweep {job_run_id}: {result['policies']}")
