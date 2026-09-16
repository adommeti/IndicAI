"""uc3's retention sweep: the two gates that keep it rehearsing, and the wall it stops at.

Two things about this module are unusual enough that a test suite has to pin them
deliberately, because both look like bugs to a reader who has not read ADR 0004.

1. **Deleting nothing is the correct default.** The window comes from
   `UC3_CALL_RETENTION_DAYS` and the safety catch from `UC3_RETENTION_ENABLED`, and a
   pass deletes only when both are open. Most of this file is the unit half, which
   exists to make sure nobody ever "fixes" the unset window by giving it a default: a
   typo in an environment variable must not become a deletion policy, and a missing
   legal answer must not become ninety days because uc1 chose ninety days.

2. **The `calls` row cannot be deleted at all**, by design and by grant. The audit
   chain (`analysis_runs`, `flags`) foreign-keys onto it, and the app's role holds
   INSERT/SELECT and nothing else on those tables, so the row that would clear the way
   cannot be removed either. `test_the_retention_boundary_is_two_different_refusals`
   proves both halves against a live PostgreSQL 16 as `uc3_app`, because that claim is
   a property of a database's grants and constraints, not of Python.

Time is frozen throughout, at an instant in **2021** that no other suite writes rows
into, and every fixture row's `source_key` carries the `FIXTURE` prefix. Retention is a
global delete by construction -- it removes every in-scope segment, not just this
file's -- so the frozen year is what keeps the counts below describing rows this file
created. The integration half cleans up in a `finally` and is re-runnable: it must not
join `test_uc3_audit.py` in `docs/build/BLOCKERS.md` as a file that only passes once.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from celery.schedules import crontab
from comms_surveillance import retention
from comms_surveillance.retention import (
    APP,
    RECORDING_POLICY,
    TRANSCRIPT_POLICY,
    Outcome,
    Recording,
    UnownedRecordingStore,
    purge_recordings,
    purge_transcripts,
    record,
    sweep,
)
from indic_platform.db.models import (
    AnalysisRun,
    Call,
    Flag,
    RetentionDeletion,
    TranscriptSegment,
)
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

FIXTURE = "uc3-retention-test"
#: 04:00 UTC, the minute the beat entry fires, in a year no other suite writes into.
FROZEN = datetime(2021, 6, 15, 4, 0, tzinfo=UTC)
WINDOW_DAYS = 30
CUTOFF = FROZEN - timedelta(days=WINDOW_DAYS)
SECOND = timedelta(seconds=1)

#: SQLSTATEs, not English. A Postgres upgrade may reword either message; neither code
#: has changed since these tables existed, and the two codes are the whole point of the
#: boundary test -- two refusals with two different causes.
FOREIGN_KEY_VIOLATION = "23503"
INSUFFICIENT_PRIVILEGE = "42501"

GATES = ("UC3_CALL_RETENTION_DAYS", "UC3_RETENTION_ENABLED", "UC3_RETENTION_BATCH_SIZE")


def frozen(at: datetime = FROZEN) -> retention.Clock:
    """A clock that does not move. `freezegun` is not a dependency of this repo."""
    return lambda: at


@pytest.fixture
def no_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every unit test from "nothing is configured".

    The ambient environment of whoever runs pytest is not part of the contract, and a
    developer with `UC3_RETENTION_ENABLED=true` exported must not get a different
    verdict from this file than CI does.
    """
    for name in GATES:
        monkeypatch.delenv(name, raising=False)


# --- gate 1: the window, which is allowed to be absent -------------------------


@pytest.mark.usefixtures("no_gates")
@pytest.mark.parametrize("raw", ["", "   "])
def test_an_absent_window_is_none_and_never_a_number(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Prevents an unanswered legal question turning into a deletion policy.

    The failure this guards against is a later edit adding `default=90` "so the job does
    something": that would delete a month of call transcripts on the first night, under
    a rule Compliance never set.
    """
    monkeypatch.setenv("UC3_CALL_RETENTION_DAYS", raw)
    assert retention.retention_days() is None


@pytest.mark.usefixtures("no_gates")
def test_an_unset_window_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevents the unset case being read from some other suite's exported variable."""
    assert os.environ.get("UC3_CALL_RETENTION_DAYS") is None
    assert retention.retention_days() is None


@pytest.mark.usefixtures("no_gates")
@pytest.mark.parametrize("raw", ["ninety", "90d", "9.5", "thirty days", "None", "0x1e"])
def test_a_typo_in_the_window_is_treated_as_unset_not_as_a_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Prevents a mistyped variable from inventing a window nobody chose.

    Falling back to a default here is the dangerous direction: the operator believes
    they configured one rule, the job applies another, and the evidence is a warning in
    a log nobody reads.
    """
    monkeypatch.setenv("UC3_CALL_RETENTION_DAYS", raw)
    assert retention.retention_days() is None


@pytest.mark.usefixtures("no_gates")
@pytest.mark.parametrize("raw", ["0", "-1", "-365"])
def test_a_non_positive_window_is_treated_as_unset_not_as_delete_everything(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Prevents `UC3_CALL_RETENTION_DAYS=0` meaning "cutoff is now", i.e. every row."""
    monkeypatch.setenv("UC3_CALL_RETENTION_DAYS", raw)
    assert retention.retention_days() is None


@pytest.mark.usefixtures("no_gates")
@pytest.mark.parametrize(("raw", "expected"), [("30", 30), ("1", 1), (" 45 ", 45), ("3650", 3650)])
def test_a_configured_window_is_returned_verbatim(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: int
) -> None:
    """Prevents the refusal above from swallowing a window that IS set."""
    monkeypatch.setenv("UC3_CALL_RETENTION_DAYS", raw)
    assert retention.retention_days() == expected


# --- gate 2: the safety catch --------------------------------------------------


@pytest.mark.usefixtures("no_gates")
@pytest.mark.parametrize("raw", ["", "   ", "false", "False", "FALSE", "1", "yes", "on", "y", "t"])
def test_anything_but_the_word_true_keeps_the_catch_closed(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Prevents a truthy-looking value arming deletion.

    `"1"` and `"yes"` are the values a hurried operator reaches for, and the ones a
    permissive `bool(value)` would accept. Deletion is not a setting to guess at, so
    only the spelled-out word opens the catch.
    """
    monkeypatch.setenv("UC3_RETENTION_ENABLED", raw)
    assert retention.deletion_enabled() is False


@pytest.mark.usefixtures("no_gates")
def test_an_unset_catch_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevents a deployment that forgot the variable from deleting anyway."""
    assert retention.deletion_enabled() is False


@pytest.mark.usefixtures("no_gates")
@pytest.mark.parametrize("raw", ["true", "TRUE", "True", "  true  ", "tRuE"])
def test_the_word_true_in_any_casing_opens_the_catch(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Prevents the catch being un-openable: case and whitespace are not a policy.

    `.strip().lower() == "true"` is what the module does, so `TRUE` from a YAML file
    that upper-cased its booleans arms deletion exactly as `true` does.
    """
    monkeypatch.setenv("UC3_RETENTION_ENABLED", raw)
    assert retention.deletion_enabled() is True


# --- both gates, as one decision ----------------------------------------------


@pytest.mark.usefixtures("no_gates")
@pytest.mark.parametrize(
    ("days", "enabled", "rehearses"),
    [
        (None, None, True),
        ("30", None, True),
        ("30", "false", True),
        (None, "true", True),
        ("", "true", True),
        ("0", "true", True),
        ("thirty", "true", True),
        ("30", "true", False),
    ],
)
def test_a_pass_only_deletes_when_both_gates_are_open(
    monkeypatch: pytest.MonkeyPatch, days: str | None, enabled: str | None, rehearses: bool
) -> None:
    """Prevents either gate alone being enough to delete.

    The dangerous cases are the middle rows: a window set "ready for later" with the
    catch still closed, and the catch armed by an operator who believes a window is
    configured when the variable is misspelt or non-positive. Both must rehearse.
    """
    if days is not None:
        monkeypatch.setenv("UC3_CALL_RETENTION_DAYS", days)
    if enabled is not None:
        monkeypatch.setenv("UC3_RETENTION_ENABLED", enabled)
    assert retention.dry_run_default() is rehearses


# --- an unconfigured pass is completed, not failed -----------------------------


@pytest.mark.usefixtures("no_gates")
async def test_an_unconfigured_transcript_pass_completes_and_names_the_missing_adr() -> None:
    """Prevents "no rule yet" being logged as a broken job, or as a silent success.

    `failed` would page somebody every night for a job working as intended; a row with
    no `reason` would leave a reviewer in a year's time unable to tell "the rule ran and
    matched nothing" from "there was no rule". Neither is acceptable, so the row is
    `completed` and carries the ADR it is waiting on.
    """
    outcome = await purge_transcripts(_no_database(), clock=frozen())

    assert outcome.status == "completed"
    assert outcome.status != "failed"
    assert outcome.policy == TRANSCRIPT_POLICY
    assert outcome.rows_matched == 0
    assert outcome.rows_deleted == 0
    assert outcome.batches == 0
    assert outcome.dry_run is True
    assert outcome.detail["reason"] == retention.UNCONFIGURED
    assert "0004" in outcome.detail["reason"]
    # Nothing matched, so there is no covered window to claim.
    assert (outcome.window_start, outcome.window_end) == (None, None)


@pytest.mark.usefixtures("no_gates")
async def test_an_unconfigured_recording_pass_completes_and_names_the_missing_adr() -> None:
    """Prevents the recording policy diverging from the transcript one when unconfigured."""
    outcome = await purge_recordings(clock=frozen())

    assert outcome.status == "completed"
    assert outcome.policy == RECORDING_POLICY
    assert outcome.target == "object-store"
    assert (outcome.rows_matched, outcome.rows_deleted, outcome.batches) == (0, 0, 0)
    assert outcome.dry_run is True
    assert outcome.detail["reason"] == retention.UNCONFIGURED


@pytest.mark.usefixtures("no_gates")
async def test_an_unconfigured_pass_never_opens_a_database_connection() -> None:
    """Prevents a nightly rehearsal that cannot run when the rule is absent.

    The session factory here raises on use. A pass with no window must decide that from
    the environment alone, so the sweep still writes its "I am deployed and idle" row on
    a night the database is degraded.
    """
    outcome = await purge_transcripts(_no_database(), clock=frozen(), limit=10)
    assert outcome.rows_matched == 0
    assert outcome.detail["reason"] == retention.UNCONFIGURED


def _no_database() -> async_sessionmaker[AsyncSession]:
    """A factory that fails loudly if a test touches the database it should not."""

    def explode() -> AsyncSession:
        raise AssertionError("this pass must not open a database session")

    return cast("async_sessionmaker[AsyncSession]", explode)


# --- the bookkeeping the audit row's constraints depend on ---------------------


def _outcome(**overrides: Any) -> Outcome:
    base: dict[str, Any] = {
        "policy": TRANSCRIPT_POLICY,
        "target": "transcript_segments",
        "retention_days": WINDOW_DAYS,
        "cutoff_at": CUTOFF,
        "started_at": FROZEN,
        "finished_at": FROZEN,
        "dry_run": False,
        "batch_size": 500,
    }
    return Outcome(**{**base, **overrides})


def test_the_covered_window_widens_over_batches_and_never_runs_backwards() -> None:
    """Prevents a later batch narrowing the window an earlier batch already covered.

    `ck_retention_deletions_window_scope` ties the window to the match count, so a
    window that tracked only the latest batch would claim a pass covered 200 rows it
    never looked at.
    """
    outcome = _outcome()
    outcome.saw([CUTOFF - timedelta(days=10), CUTOFF - timedelta(days=5)])
    outcome.saw([CUTOFF - timedelta(days=40)])
    outcome.saw([CUTOFF - timedelta(days=20)])

    assert outcome.window_start == CUTOFF - timedelta(days=40)
    assert outcome.window_end == CUTOFF - timedelta(days=5)


def test_an_empty_batch_leaves_the_window_untouched() -> None:
    """Prevents a zero-match pass claiming a window, which the check constraint refuses.

    `(rows_matched = 0) = (window_start is null)`: a final empty batch that reset or
    invented a bound would make the audit insert fail at 04:00 with a constraint error.
    """
    outcome = _outcome()
    outcome.saw([])
    assert (outcome.window_start, outcome.window_end) == (None, None)

    outcome.saw([CUTOFF - SECOND])
    outcome.saw([])
    assert outcome.window_start == CUTOFF - SECOND
    assert outcome.window_end == CUTOFF - SECOND


def test_the_audit_row_carries_every_field_of_the_pass_and_is_stamped_for_uc3() -> None:
    """Prevents a pass being logged under the wrong app or with a field silently dropped.

    `retention_deletions` is shared with uc1. A row whose `app` were wrong, or whose
    counts did not match the pass, would make the one table a reviewer consults say
    something untrue about which application deleted what.
    """
    job_run_id = uuid.uuid4()
    outcome = _outcome(
        rows_matched=7,
        rows_deleted=7,
        batches=3,
        window_start=CUTOFF - timedelta(days=9),
        window_end=CUTOFF - SECOND,
        detail={"reason": "because"},
    )

    row = outcome.row(job_run_id)

    assert isinstance(row, RetentionDeletion)
    assert row.app == APP == "comms_surveillance"
    assert row.job_run_id == job_run_id
    assert row.policy == outcome.policy
    assert row.target == outcome.target
    assert row.retention_days == outcome.retention_days
    assert row.cutoff_at == outcome.cutoff_at
    assert row.window_start == outcome.window_start
    assert row.window_end == outcome.window_end
    assert row.rows_matched == outcome.rows_matched
    assert row.rows_deleted == outcome.rows_deleted
    assert row.batches == outcome.batches
    assert row.batch_size == outcome.batch_size
    assert row.dry_run == outcome.dry_run
    assert row.status == outcome.status
    assert row.detail == outcome.detail
    assert row.started_at == outcome.started_at
    assert row.finished_at == outcome.finished_at
    # Two passes are two rows: the id is not derived from the job run.
    assert outcome.row(job_run_id).id != row.id
    # The rule is reconstructible from the row alone.
    assert row.started_at - timedelta(days=row.retention_days) == row.cutoff_at


# --- recordings: the store uc3 does not own ------------------------------------


@dataclass
class FakeRecordingStore:
    """An object store uc3 has been granted deletion authority over: the hypothetical.

    Hand-written rather than a Mock, because the property under test is that the
    protocol is enough -- installing a store is the whole change -- and a Mock satisfies
    any protocol, including one the module does not actually call.
    """

    items: list[Recording]
    removed: list[str] = field(default_factory=list)
    listings: list[int] = field(default_factory=list)

    def list_before(self, cutoff: datetime, *, limit: int) -> Iterable[Recording]:
        found = sorted(
            (item for item in self.items if item.last_modified < cutoff),
            key=lambda item: item.key,
        )[:limit]
        self.listings.append(len(found))
        return found

    def remove(self, keys: Sequence[str]) -> int:
        doomed = set(keys)
        before = len(self.items)
        self.items = [item for item in self.items if item.key not in doomed]
        self.removed.extend(keys)
        return before - len(self.items)


def _recordings(*offsets: timedelta) -> list[Recording]:
    return [
        Recording(key=f"calls/{i:03d}.wav", last_modified=CUTOFF + off)
        for i, off in enumerate(offsets)
    ]


@pytest.mark.usefixtures("no_gates")
async def test_the_default_recording_store_matches_nothing_even_with_both_gates_open() -> None:
    """Prevents a deployment arming deletion and quietly deleting the system of record.

    The recordings are the business's own store; uc3 reads that prefix and has no
    authority to write it. So opening both gates must still produce a rehearsal, and the
    row must say why rather than reading as "there was nothing old enough".
    """
    assert isinstance(retention.recording_store(), UnownedRecordingStore)

    outcome = await purge_recordings(clock=frozen(), days=WINDOW_DAYS, limit=10, dry_run=False)

    assert outcome.status == "completed"
    assert outcome.dry_run is True, "an unowned store must not be asked to delete"
    assert (outcome.rows_matched, outcome.rows_deleted, outcome.batches) == (0, 0, 0)
    assert outcome.detail["reason"] == UnownedRecordingStore.reason
    assert "ADR 0004" in outcome.detail["reason"]
    assert outcome.cutoff_at == CUTOFF


def test_the_default_recording_store_refuses_to_delete_rather_than_reporting_zero() -> None:
    """Prevents "cannot delete" being indistinguishable from "deleted nothing".

    A `return 0` here would let a future caller that skipped the `dry_run` forcing above
    report a clean, successful pass over a store it never had permission to touch. The
    raise makes that mistake land in the audit row as `failed`.
    """
    with pytest.raises(PermissionError) as raised:
        UnownedRecordingStore().remove(["calls/000.wav"])
    assert UnownedRecordingStore.reason in str(raised.value)
    assert list(UnownedRecordingStore().list_before(FROZEN, limit=10)) == []


@pytest.mark.usefixtures("no_gates")
async def test_an_authorised_store_deletes_the_recordings_past_the_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents the policy being inert once somebody does have deletion authority.

    Both gates set through the environment, so this exercises the real configuration
    path rather than the `days=`/`dry_run=` overrides the other tests use.
    """
    monkeypatch.setenv("UC3_CALL_RETENTION_DAYS", str(WINDOW_DAYS))
    monkeypatch.setenv("UC3_RETENTION_ENABLED", "true")
    store = FakeRecordingStore(items=_recordings(-SECOND, -timedelta(days=5), SECOND))
    try:
        retention.set_recording_store(store)
        assert retention.dry_run_default() is False
        outcome = await purge_recordings(
            clock=frozen(), limit=10, dry_run=retention.dry_run_default()
        )
    finally:
        retention.set_recording_store(None)

    assert outcome.retention_days == WINDOW_DAYS
    assert outcome.dry_run is False
    assert outcome.rows_matched == 2
    assert outcome.rows_deleted == 2
    assert store.removed == ["calls/000.wav", "calls/001.wav"]
    assert [item.key for item in store.items] == ["calls/002.wav"]
    assert outcome.window_start == CUTOFF - timedelta(days=5)
    assert outcome.window_end == CUTOFF - SECOND
    assert outcome.status == "completed"
    # The default is restored: one test's store is not the next test's.
    assert isinstance(retention.recording_store(), UnownedRecordingStore)


@pytest.mark.usefixtures("no_gates")
async def test_a_recording_store_that_breaks_is_recorded_as_failed_not_as_a_clean_zero() -> None:
    """Prevents an unreachable bucket looking identical to a bucket with nothing old in it."""

    class Broken(FakeRecordingStore):
        def list_before(self, cutoff: datetime, *, limit: int) -> Iterable[Recording]:
            raise OSError("bucket unreachable")

    try:
        retention.set_recording_store(Broken(items=[]))
        outcome = await purge_recordings(clock=frozen(), days=WINDOW_DAYS, dry_run=False)
    finally:
        retention.set_recording_store(None)

    assert outcome.status == "failed"
    assert outcome.detail["error"] == "OSError"


# --- the schedule --------------------------------------------------------------


def test_the_sweep_is_scheduled_at_four_am_on_uc3s_own_celery_app() -> None:
    """Prevents the job existing as a function nobody ever calls.

    A retention policy that is implemented but never scheduled looks, in the database,
    exactly like one that ran every night and found nothing.
    """
    from comms_surveillance.ingest import celery_app

    entry = retention.BEAT_SCHEDULE["uc3-retention-sweep"]
    assert entry["task"] == "uc3.retention_sweep"
    assert entry["schedule"] == crontab(hour=4, minute=0)
    assert "uc3.retention_sweep" in celery_app.tasks
    assert celery_app.conf.timezone == "UTC"
    # Merged into the app's schedule, not substituted for it.
    scheduled = celery_app.conf.beat_schedule
    assert scheduled["uc3-retention-sweep"]["schedule"] == crontab(hour=4, minute=0)
    assert "uc3-nightly-sweep" in scheduled
    assert "uc3-chain-verify" in scheduled


def test_the_sweep_does_not_share_its_minute_with_another_nightly_job() -> None:
    """Prevents two nightly jobs opening write transactions on one Postgres together.

    The ordering matters as much as the collision: the sweep must run after uc3's 02:00
    ingest has written the rows it measures and before the 05:00 chain verify, so a
    night's deletions fall inside the walk that follows them.
    """
    from comms_surveillance.ingest import celery_app
    from helpdesk_agent import retention as uc1_retention

    ours = crontab(hour=4, minute=0)
    scheduled = celery_app.conf.beat_schedule
    assert scheduled["uc3-nightly-sweep"]["schedule"] == crontab(hour=2, minute=0)
    assert scheduled["uc3-chain-verify"]["schedule"] == crontab(hour=5, minute=0)
    assert scheduled["uc3-nightly-sweep"]["schedule"] != ours
    assert scheduled["uc3-chain-verify"]["schedule"] != ours
    # uc1 runs on its own Celery app but against the same database.
    uc1 = uc1_retention.BEAT_SCHEDULE["uc1-retention-sweep"]["schedule"]
    assert uc1 == crontab(hour=3, minute=30)
    assert uc1 != ours


def test_the_sweep_retries_transient_faults_and_not_bugs() -> None:
    """Prevents a `KeyError` being retried sixteen times into the log at 04:00."""
    assert retention.TASK["max_retries"] == 3
    assert set(retention.RETRY_ON) == {OSError, TimeoutError, DBAPIError}
    assert ValueError not in retention.RETRY_ON


# --- the sweep, without a database ---------------------------------------------


class _EmptyResult:
    def all(self) -> list[Any]:
        return []


class _FakeTransaction:
    async def __aenter__(self) -> _FakeTransaction:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, added: list[Any]) -> None:
        self._added = added

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, statement: Any) -> _EmptyResult:
        return _EmptyResult()

    def add(self, obj: Any) -> None:
        self._added.append(obj)


class _FakeFactory:
    """The smallest thing `purge_transcripts` and `record` need: sessions over no rows."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def __call__(self) -> _FakeSession:
        return _FakeSession(self.added)


@pytest.mark.usefixtures("no_gates")
async def test_one_sweep_logs_both_policies_under_one_job_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents a night's sweep reading as two unrelated rows that share a timestamp.

    Also pins the asymmetry a reviewer must be able to see at a glance: with both gates
    open the sweep is a real pass, and the recording row inside it is still a rehearsal,
    because authority over the two stores is not the same question.
    """
    monkeypatch.setenv("UC3_CALL_RETENTION_DAYS", str(WINDOW_DAYS))
    monkeypatch.setenv("UC3_RETENTION_ENABLED", "true")
    factory = _FakeFactory()

    result = await sweep(clock=frozen(), limit=50, factory=cast("Any", factory))

    assert result["dry_run"] is False
    assert result["enabled"] is True
    assert result["retention_days"] == WINDOW_DAYS
    rows = [row for row in factory.added if isinstance(row, RetentionDeletion)]
    assert len(rows) == 2
    assert {row.policy for row in rows} == {TRANSCRIPT_POLICY, RECORDING_POLICY}
    assert {str(row.job_run_id) for row in rows} == {result["job_run_id"]}
    assert {row.app for row in rows} == {APP}
    assert sorted(result["log_ids"]) == sorted(str(row.id) for row in rows)
    by_policy = {row.policy: row for row in rows}
    assert by_policy[TRANSCRIPT_POLICY].dry_run is False
    assert by_policy[RECORDING_POLICY].dry_run is True
    assert by_policy[RECORDING_POLICY].detail["reason"] == UnownedRecordingStore.reason
    assert [p["policy"] for p in result["policies"]] == [TRANSCRIPT_POLICY, RECORDING_POLICY]


# --- the database half ---------------------------------------------------------


def _database_url() -> str:
    """The stack's database, or a skip.

    CI's integration job provisions Postgres and fails on any skip, so with DATABASE_URL
    set an unreachable server is a failure, never a skip.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")
    return url


async def _clear(factory: async_sessionmaker[AsyncSession]) -> None:
    """Remove this file's rows: its own, and any a killed earlier run left behind.

    Scoped by the `FIXTURE` source-key prefix and, for the audit rows, by a cutoff in
    2021 under this app -- so it can never reach another suite's data. Run before each
    case as well as after, because the point of the 2021 window is that only this file's
    rows are inside it.
    """
    async with factory() as db, db.begin():
        ids = list(
            (await db.scalars(select(Call.id).where(Call.source_key.like(f"{FIXTURE}/%")))).all()
        )
        if ids:
            await db.execute(delete(Flag).where(Flag.call_id.in_(ids)))
            await db.execute(delete(AnalysisRun).where(AnalysisRun.call_id.in_(ids)))
            await db.execute(delete(TranscriptSegment).where(TranscriptSegment.call_id.in_(ids)))
            await db.execute(delete(Call).where(Call.id.in_(ids)))
        await db.execute(
            delete(RetentionDeletion).where(
                RetentionDeletion.app == APP,
                RetentionDeletion.cutoff_at < datetime(2022, 1, 1, tzinfo=UTC),
            )
        )


@asynccontextmanager
async def _db() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _clear(factory)
        yield factory
    finally:
        await _clear(factory)
        await engine.dispose()


async def _seed_call(
    factory: async_sessionmaker[AsyncSession],
    *,
    recorded_at: datetime | None,
    ingested_at: datetime,
    segments: int = 1,
) -> tuple[uuid.UUID, list[int]]:
    """One call with `segments` transcript rows. Every NOT NULL column supplied."""
    call_id = uuid.uuid4()
    seg_ids = list(range(segments))
    async with factory() as db, db.begin():
        db.add(
            Call(
                id=call_id,
                source_uri=f"s3://calls/{FIXTURE}/{call_id}.wav",
                source_key=f"{FIXTURE}/{call_id}.wav",
                recorded_at=recorded_at,
                ingested_at=ingested_at,
                duration_s=12,
                participants=["agent"],
                languages=["hi-IN"],
                status="transcribed",
                stt_model="saarika:v2",
            )
        )
        for seg_id in seg_ids:
            db.add(
                TranscriptSegment(
                    call_id=call_id,
                    seg_id=seg_id,
                    speaker="spk_0",
                    start_ms=seg_id * 1000,
                    end_ms=seg_id * 1000 + 900,
                    text="कल मीटिंग है",
                    text_roman="kal meeting hai",
                    language="hi-IN",
                    roman_source="indic-transliteration:iast",
                )
            )
    return call_id, seg_ids


async def _alive(factory: async_sessionmaker[AsyncSession], call_id: uuid.UUID) -> set[int]:
    """Which of this call's segments still exist. Never a count over the table."""
    async with factory() as db:
        rows = await db.scalars(
            select(TranscriptSegment.seg_id).where(TranscriptSegment.call_id == call_id)
        )
    return set(rows.all())


@pytest.mark.integration
async def test_the_retention_boundary_is_two_different_refusals() -> None:
    """Prevents the module's central claim being a comment rather than a fact.

    uc3 can delete a call's transcript and cannot delete the call. Both halves are
    properties of this database as the application's own role (`uc3_app`, NOLOGIN, so
    reached with SET ROLE), and they fail for two different reasons:

      * the `calls` row is pinned by a foreign key from the hash-chained `analysis_runs`;
      * the chained row that would clear the way cannot be deleted at all, because the
        role holds INSERT/SELECT on it and nothing else.

    If either refusal ever became the other -- an ON DELETE CASCADE added to the FK, or
    a DELETE grant added to the role -- a nightly retention pass would start erasing
    audit evidence, and a single-cause assertion would not notice. So the test asserts
    both SQLSTATEs and that they differ. Nothing is asserted about the English text: a
    Postgres upgrade may reword it, and the codes are the stable contract.
    """
    engine = create_async_engine(_database_url())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    try:
        await _clear(factory)
        call_id, _ = await _seed_call(
            factory,
            recorded_at=FROZEN - timedelta(days=400),
            ingested_at=FROZEN - timedelta(days=400),
            segments=1,
        )
        # A separate transaction on purpose: the unit of work does not order an insert
        # into `analysis_runs` after one into `calls` (there is no ORM relationship
        # between them, only a database-level foreign key), so the call must already be
        # committed before its analysis run is written.
        async with factory() as db, db.begin():
            db.add(
                AnalysisRun(
                    id=run_id,
                    call_id=call_id,
                    stage="detect",
                    model="claude-haiku-4.5",
                    created_at=FROZEN - timedelta(days=400),
                    output={"flags": []},
                    row_hash="0" * 64,
                )
            )

        deleted_segments, fk_state, privilege_state = await _three_deletes(engine, call_id, run_id)

        assert deleted_segments == 1, "the app must be able to delete a call's transcript"
        assert fk_state == FOREIGN_KEY_VIOLATION, "the chain must pin the calls row"
        assert privilege_state == INSUFFICIENT_PRIVILEGE, "the chained row must be undeletable"
        assert fk_state != privilege_state, "two refusals, two causes"
        print(
            f"boundary: transcript_segments DELETE {deleted_segments}, "
            f"calls SQLSTATE {fk_state}, analysis_runs SQLSTATE {privilege_state}"
        )
    finally:
        await _clear(factory)
        await engine.dispose()


async def _three_deletes(
    engine: Any, call_id: uuid.UUID, run_id: uuid.UUID
) -> tuple[int, str | None, str | None]:
    """Run the three deletes as `uc3_app`, each in its own savepoint, and roll it all back.

    SAVEPOINTs because an expected failure aborts the transaction otherwise and the two
    later steps would report "current transaction is aborted" instead of their own
    cause. The outer transaction is never committed, so the successful delete in step 1
    is real (Postgres executed it and reported a row count) and leaves nothing behind.
    """
    async with engine.connect() as conn:
        await conn.execute(text("set role uc3_app"))

        savepoint = await conn.begin_nested()
        result = await conn.execute(
            text("delete from transcript_segments where call_id = :id"), {"id": call_id}
        )
        deleted = result.rowcount
        await savepoint.rollback()

        fk_state = await _sqlstate_of(conn, "delete from calls where id = :id", {"id": call_id})
        privilege_state = await _sqlstate_of(
            conn, "delete from analysis_runs where id = :id", {"id": run_id}
        )
        await conn.rollback()
    return deleted, fk_state, privilege_state


async def _sqlstate_of(conn: Any, statement: str, params: dict[str, Any]) -> str | None:
    """The SQLSTATE a statement fails with, or None if it unexpectedly succeeded."""
    savepoint = await conn.begin_nested()
    try:
        await conn.execute(text(statement), params)
    except DBAPIError as exc:
        await savepoint.rollback()
        return str(getattr(exc.orig, "sqlstate", None))
    await savepoint.rollback()
    return None


@pytest.mark.integration
async def test_a_dry_run_reports_the_old_segments_and_leaves_every_one_in_place() -> None:
    """Prevents a rehearsal deleting, which is the only way to learn the gates are wrong.

    The rehearsal is what a deployment runs for weeks before ADR 0004 lands, and the
    only evidence anybody will have that the first real pass removes what they expect.
    """
    async with _db() as factory:
        old, old_segs = await _seed_call(
            factory, recorded_at=CUTOFF - SECOND, ingested_at=FROZEN, segments=3
        )
        new, new_segs = await _seed_call(
            factory, recorded_at=CUTOFF + SECOND, ingested_at=FROZEN, segments=2
        )

        outcome = await purge_transcripts(
            factory, clock=frozen(), days=WINDOW_DAYS, limit=100, dry_run=True
        )

        assert outcome.cutoff_at == CUTOFF
        assert outcome.dry_run is True
        assert outcome.rows_matched == 3
        assert outcome.rows_deleted == 0
        assert outcome.window_start == outcome.window_end == CUTOFF - SECOND
        assert await _alive(factory, old) == set(old_segs)
        assert await _alive(factory, new) == set(new_segs)
        print(f"dry run: would delete {outcome.rows_matched}, deleted {outcome.rows_deleted}")


@pytest.mark.integration
async def test_a_real_pass_deletes_the_segments_past_the_cutoff_and_no_others() -> None:
    """Prevents a retention pass reaching calls that are still inside the window.

    The row exactly on the cutoff is the one that decides whether the comparison is `<`
    or `<=`; deleting it would make every pass a day early, which is the same class of
    error as deleting a year early, only harder to see.
    """
    async with _db() as factory:
        old, _ = await _seed_call(
            factory, recorded_at=CUTOFF - timedelta(days=10), ingested_at=FROZEN, segments=2
        )
        on_the_line, on_segs = await _seed_call(
            factory, recorded_at=CUTOFF, ingested_at=FROZEN, segments=1
        )
        new, new_segs = await _seed_call(
            factory, recorded_at=CUTOFF + SECOND, ingested_at=FROZEN, segments=2
        )

        outcome = await purge_transcripts(
            factory, clock=frozen(), days=WINDOW_DAYS, limit=100, dry_run=False
        )

        assert outcome.rows_matched == 2
        assert outcome.rows_deleted == 2
        assert outcome.status == "completed"
        assert await _alive(factory, old) == set()
        assert await _alive(factory, on_the_line) == set(on_segs)
        assert await _alive(factory, new) == set(new_segs)
        # The parent call survives its transcript: that is the boundary, not a bug.
        async with factory() as db:
            assert await db.get(Call, old) is not None
        print(f"real pass: deleted {outcome.rows_deleted} segments, cutoff {outcome.cutoff_at}")


@pytest.mark.integration
async def test_a_recording_with_no_timestamp_is_not_immortal() -> None:
    """Prevents the row most likely to be mishandled being kept for ever by accident.

    `calls.recorded_at` is nullable because the source prefix is an input uc3 does not
    own. A bare `recorded_at < cutoff` is false for NULL, so a recording that arrived
    without a timestamp would never fall out of scope -- it would outlive every call
    whose metadata was complete. The clock is `coalesce(recorded_at, ingested_at)`, so
    custody time governs when the recording's own stamp is missing.
    """
    async with _db() as factory:
        stamped, _ = await _seed_call(
            factory, recorded_at=CUTOFF - SECOND, ingested_at=FROZEN, segments=1
        )
        unstamped, _ = await _seed_call(
            factory, recorded_at=None, ingested_at=CUTOFF - timedelta(days=3), segments=1
        )
        unstamped_recent, recent_segs = await _seed_call(
            factory, recorded_at=None, ingested_at=CUTOFF + timedelta(days=3), segments=1
        )

        outcome = await purge_transcripts(
            factory, clock=frozen(), days=WINDOW_DAYS, limit=100, dry_run=False
        )

        assert outcome.rows_matched == 2
        assert await _alive(factory, unstamped) == set(), "a NULL recorded_at is not a shield"
        assert await _alive(factory, stamped) == set()
        # ...and the fallback does not drag a recently ingested call into scope either.
        assert await _alive(factory, unstamped_recent) == set(recent_segs)
        assert outcome.window_start == CUTOFF - timedelta(days=3)


@pytest.mark.integration
async def test_a_dry_run_over_more_rows_than_one_batch_terminates() -> None:
    """Prevents the pass that deletes nothing never finishing.

    A dry run removes no rows, so an OFFSET-free `LIMIT` would hand back the same batch
    for ever: the job would run until the worker was killed, having logged nothing. The
    keyset must advance on the scan's own ordering, not on rows disappearing. The
    timeout is the assertion -- a regression hangs, and a hang is not a test result.
    """
    async with _db() as factory:
        call_id, seg_ids = await _seed_call(
            factory, recorded_at=CUTOFF - timedelta(days=2), ingested_at=FROZEN, segments=5
        )
        second, second_segs = await _seed_call(
            factory, recorded_at=CUTOFF - timedelta(days=1), ingested_at=FROZEN, segments=2
        )

        async with asyncio.timeout(60):
            outcome = await purge_transcripts(
                factory, clock=frozen(), days=WINDOW_DAYS, limit=2, dry_run=True
            )

        assert outcome.rows_matched == 7
        assert outcome.rows_deleted == 0
        assert outcome.batches == 4  # 2 + 2 + 2 + 1
        assert await _alive(factory, call_id) == set(seg_ids)
        assert await _alive(factory, second) == set(second_segs)
        print(f"paging: {outcome.rows_matched} rows in {outcome.batches} batches of 2")


@pytest.mark.integration
async def test_the_audit_row_a_reviewer_reads_a_year_later_is_written_and_readable() -> None:
    """Prevents a deletion that nobody can later prove happened, or happened lawfully.

    The row has to carry the rule (`policy`, `retention_days`, `cutoff_at`), the scope
    (`target`, the covered window) and the result (`rows_matched` vs `rows_deleted`),
    under the right `app` -- `retention_deletions` is shared with uc1.
    """
    async with _db() as factory:
        oldest = CUTOFF - timedelta(days=40)
        newest = CUTOFF - SECOND
        await _seed_call(factory, recorded_at=oldest, ingested_at=FROZEN, segments=1)
        await _seed_call(factory, recorded_at=newest, ingested_at=FROZEN, segments=2)

        job_run_id = uuid.uuid4()
        outcome = await purge_transcripts(
            factory, clock=frozen(), days=WINDOW_DAYS, limit=100, dry_run=False
        )
        row_id = await record(factory, outcome, job_run_id)

        async with factory() as db:
            row = await db.scalar(select(RetentionDeletion).where(RetentionDeletion.id == row_id))
        assert row is not None
        assert row.app == APP
        assert row.job_run_id == job_run_id
        assert row.policy == TRANSCRIPT_POLICY
        assert row.target == "transcript_segments"
        assert row.retention_days == WINDOW_DAYS
        assert row.cutoff_at == CUTOFF
        assert row.rows_matched == 3
        assert row.rows_deleted == 3
        assert row.window_start == oldest
        assert row.window_end == newest
        assert row.dry_run is False
        assert row.status == "completed"
        assert row.started_at - timedelta(days=row.retention_days) == row.cutoff_at
        # An audit record of a privacy deletion must not quote what it deleted.
        assert "मीटिंग" not in str(row.detail)
        print(f"logged: app={row.app} policy={row.policy} deleted={row.rows_deleted}")
