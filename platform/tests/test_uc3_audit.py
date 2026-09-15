"""The append-only, hash-chained audit trail (PRD E8).

The pure tests pin the hash contract. The integration tests are the ones that
matter for the acceptance criteria, because the properties being claimed --
"a superuser edit is detected", "the app role cannot UPDATE" -- are properties
of a real Postgres, not of Python.
"""

import os
import uuid as uuidlib
from datetime import UTC, datetime
from typing import Any

import pytest
from comms_surveillance import audit


def _skip_without_db() -> None:
    import socket
    from urllib.parse import urlparse

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), 1):
            pass
    except OSError:
        pytest.skip(f"Postgres at {parsed.hostname}:{parsed.port} is not reachable")


# --- the hash contract ---------------------------------------------------------


def test_canonical_json_is_stable_under_key_order_and_whitespace() -> None:
    """Two processes hashing the same row must produce identical bytes."""
    a = {"b": 1, "a": "x", "c": [3, 2]}
    b = {"c": [3, 2], "a": "x", "b": 1}
    assert audit.canonical_json(a) == audit.canonical_json(b)
    assert b" " not in audit.canonical_json(a)


def test_canonical_json_keeps_native_script_as_itself() -> None:
    """`ensure_ascii` would hash a Devanagari evidence span as escape sequences.

    Harmless until something else — a reviewer UI, an export, a different
    library — hashes the same row without that setting and gets a different
    answer.
    """
    payload = {"evidence_span": "मैंने अप्रकाशित नतीजे देखे हैं"}
    assert "मैंने".encode() in audit.canonical_json(payload)


def test_canonical_json_handles_the_types_json_has_no_opinion_about() -> None:
    payload = {
        "id": uuidlib.uuid4(),
        "created_at": datetime.now(UTC),
        "score": 0.5,
    }
    assert audit.canonical_json(payload)  # must not raise


def test_the_hash_is_the_formula_e8_specifies() -> None:
    """`row_hash = sha256(prev_hash || canonical_json(row_without_hashes))`."""
    import hashlib

    payload = {"a": 1}
    expected = hashlib.sha256(b"abc" + audit.canonical_json(payload)).hexdigest()
    assert audit.row_hash("abc", payload) == expected


def test_changing_any_field_changes_the_hash() -> None:
    base = {"category": "conduct", "evidence_span": "x", "severity": "low"}
    first = audit.row_hash("", base)
    for key, value in [("category", "mnpi_insider"), ("evidence_span", "y"), ("severity", "high")]:
        assert audit.row_hash("", {**base, key: value}) != first


def test_the_same_row_at_a_different_chain_position_hashes_differently() -> None:
    """What makes it a chain rather than a set of independent checksums."""
    payload = {"a": 1}
    assert audit.row_hash("", payload) != audit.row_hash("deadbeef", payload)


def test_the_hashes_and_seq_are_not_part_of_what_is_hashed() -> None:
    """A row cannot contain its own hash, and `seq` is assigned by the database
    after the application has hashed the row."""
    assert audit.NOT_HASHED == {"prev_hash", "row_hash", "seq"}


def test_summarise_reports_a_break_count_a_monitor_can_alert_on() -> None:
    clean = [audit.ChainResult("flags", 3, True)]
    broken = [
        audit.ChainResult("flags", 3, True),
        audit.ChainResult("dispositions", 2, False, 7, "abc", "edited"),
    ]
    assert audit.summarise(clean)["breaks"] == 0
    assert audit.summarise(clean)["ok"] is True
    assert audit.summarise(broken)["breaks"] == 1
    assert audit.summarise(broken)["ok"] is False


# --- stack: the acceptance criteria ----------------------------------------------


async def _engine() -> tuple[Any, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(os.environ["DATABASE_URL"])
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _seed_call(factory: Any) -> uuidlib.UUID:
    from indic_platform.db.models import Call

    call_id = uuidlib.uuid4()
    async with factory() as db:
        db.add(
            Call(
                id=call_id,
                source_uri=f"s3://t/{call_id}.wav",
                source_key=f"audit/{call_id}.wav",
                status="transcribed",
            )
        )
        await db.commit()
    return call_id


def _run(call_id: uuidlib.UUID, stage: str = "deep_analysis") -> Any:
    from indic_platform.db.models import AnalysisRun

    return AnalysisRun(
        call_id=call_id,
        stage=stage,
        model="claude-sonnet-5",
        policy_version="p1",
        lexicon_version="l1",
        prompt_version="pr1",
        input_sha256="0" * 64,
        output={"flags": []},
    )


@pytest.mark.integration
async def test_a_superuser_edit_is_detected_by_chain_verify() -> None:
    """The acceptance criterion: the tamper test.

    A direct UPDATE, bypassing the application entirely, must show up as a
    break. This is the whole reason the chain exists — the app role cannot
    UPDATE (see the next test), so the threat model is somebody with more
    privilege, and against them detection is the control.
    """
    from indic_platform.db.models import AnalysisRun
    from sqlalchemy import select, text

    _skip_without_db()
    engine, factory = await _engine()
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            for stage in ("triage", "deep_analysis", "triage"):
                await audit.append(db, _run(call_id, stage))
            await db.commit()

        async with factory() as db:
            before = await audit.verify_chain(db, "analysis_runs")
        assert before.ok, before.reason

        # Tamper: change a stored field without touching the hashes, exactly as
        # somebody editing the audit trail by hand would.
        async with factory() as db:
            target = (
                await db.execute(
                    select(AnalysisRun.id)
                    .where(AnalysisRun.call_id == call_id)
                    .order_by(AnalysisRun.seq)
                    .offset(1)
                    .limit(1)
                )
            ).scalar_one()
            await db.execute(
                text("update analysis_runs set model = :m where id = :i"),
                {"m": "some-other-model", "i": target},
            )
            await db.commit()

        async with factory() as db:
            after = await audit.verify_chain(db, "analysis_runs")
        assert after.ok is False
        assert after.first_break_id == str(target)
        assert "edited" in after.reason
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_a_deleted_row_is_detected_as_a_break() -> None:
    """Deletion breaks the link, not the row: the *next* row is where it shows."""
    from indic_platform.db.models import AnalysisRun
    from sqlalchemy import select, text

    _skip_without_db()
    engine, factory = await _engine()
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            for _ in range(3):
                await audit.append(db, _run(call_id))
            await db.commit()

        async with factory() as db:
            middle = (
                await db.execute(
                    select(AnalysisRun.id)
                    .where(AnalysisRun.call_id == call_id)
                    .order_by(AnalysisRun.seq)
                    .offset(1)
                    .limit(1)
                )
            ).scalar_one()
            await db.execute(text("delete from analysis_runs where id = :i"), {"i": middle})
            await db.commit()

        async with factory() as db:
            result = await audit.verify_chain(db, "analysis_runs")
        assert result.ok is False
        assert "inserted, deleted or reordered" in result.reason
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_an_untampered_chain_verifies_across_all_three_tables() -> None:
    """The happy path, and the output the report quotes."""
    from indic_platform.db.models import Disposition, Flag

    _skip_without_db()
    engine, factory = await _engine()
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            run = await audit.append(db, _run(call_id))
            flag = await audit.append(
                db,
                Flag(
                    call_id=call_id,
                    run_id=run.id,
                    category="guaranteed_returns",
                    severity="high",
                    speaker="SPEAKER_00",
                    evidence_span="there is no downside at all",
                ),
            )
            await audit.append(
                db,
                Disposition(
                    flag_id=flag.id,
                    disposition="confirmed",
                    note="escalated to compliance lead",
                    reviewer_id="reviewer-1",
                ),
            )
            await db.commit()

        async with factory() as db:
            summary = audit.summarise(await audit.verify_all(db))
        assert summary["ok"] is True
        assert summary["breaks"] == 0
        assert {t["table"] for t in summary["tables"]} == set(audit.CHAINED)
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_a_changed_mind_is_a_new_row_not_an_edit() -> None:
    """Append-only in practice: two dispositions on one flag, both preserved."""
    from indic_platform.db.models import Disposition, Flag
    from sqlalchemy import func, select

    _skip_without_db()
    engine, factory = await _engine()
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            run = await audit.append(db, _run(call_id))
            flag = await audit.append(
                db,
                Flag(
                    call_id=call_id,
                    run_id=run.id,
                    category="conduct",
                    severity="medium",
                    evidence_span="x",
                ),
            )
            for decision in ("needs_more_context", "false_positive"):
                await audit.append(
                    db,
                    Disposition(
                        flag_id=flag.id,
                        disposition=decision,
                        reviewer_id="reviewer-1",
                    ),
                )
            await db.commit()

        async with factory() as db:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(Disposition)
                    .where(Disposition.flag_id == flag.id)
                )
            ).scalar_one()
            assert count == 2, "the earlier decision must still be there"
            assert (await audit.verify_chain(db, "dispositions")).ok
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_the_app_role_cannot_update_or_delete_the_audit_tables() -> None:
    """The acceptance criterion: the app role cannot UPDATE.

    Asserted by connecting *as that role* and watching Postgres refuse, not by
    reading the grant table — a grant that looks right and a statement that is
    actually refused are different claims, and only the second is the control.
    """
    _skip_without_db()
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError
    from sqlalchemy.ext.asyncio import create_async_engine

    engine, factory = await _engine()
    login = f"uc3_app_test_{uuidlib.uuid4().hex[:8]}"
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            run = await audit.append(db, _run(call_id))
            await db.commit()

        # A login role that is a member of uc3_app: what a deployment does.
        async with factory() as db:
            await db.execute(text(f"create role {login} login password 'test-only-not-a-secret'"))
            await db.execute(text(f"grant uc3_app to {login}"))
            await db.execute(text("grant usage on schema public to uc3_app"))
            await db.commit()

        url = os.environ["DATABASE_URL"]
        scheme, _, rest = url.partition("://")
        _, _, hostpart = rest.rpartition("@")
        as_app = create_async_engine(f"{scheme}://{login}:test-only-not-a-secret@{hostpart}")
        try:
            async with as_app.begin() as conn:
                # It can read.
                await conn.execute(text("select count(*) from analysis_runs"))
            for statement in (
                "update analysis_runs set model = 'tampered'",
                "delete from analysis_runs",
                "update flags set severity = 'low'",
                "delete from dispositions",
            ):
                # psycopg's InsufficientPrivilege surfaces as ProgrammingError.
                with pytest.raises(ProgrammingError):
                    async with as_app.begin() as conn:
                        await conn.execute(text(statement))
        finally:
            await as_app.dispose()

        # And the row it could not touch is still there and still verifies.
        async with factory() as db:
            assert (await audit.verify_chain(db, "analysis_runs")).ok
            assert run.id
    finally:
        async with factory() as db:
            await db.execute(text(f"drop role if exists {login}"))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
async def test_ten_thousand_rows_verify_in_under_ten_seconds() -> None:
    """The acceptance criterion, measured rather than asserted from the shape.

    Rows are inserted directly with pre-computed hashes: `audit.append` takes an
    advisory lock and a round trip per row, which is right for correctness and
    wrong for building a fixture. What is being timed is the *verification*.
    """
    import time

    from sqlalchemy import text

    _skip_without_db()
    engine, factory = await _engine()
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            start_head = await audit.head(db, "analysis_runs")

        rows = []
        prev = start_head
        now = datetime.now(UTC)
        for _ in range(10_000):
            row = {
                "id": uuidlib.uuid4(),
                "call_id": call_id,
                "stage": "triage",
                "model": "claude-haiku-4-5",
                "policy_version": "p1",
                "lexicon_version": "l1",
                "prompt_version": "pr1",
                "input_sha256": "0" * 64,
                "output": {"flags": []},
                "created_at": now,
            }
            digest = audit.row_hash(prev, row)
            rows.append({**row, "prev_hash": prev, "row_hash": digest})
            prev = digest

        async with factory() as db:
            await db.execute(
                text(
                    "insert into analysis_runs (id, call_id, stage, model, policy_version,"
                    " lexicon_version, prompt_version, input_sha256, output, created_at,"
                    " prev_hash, row_hash) values (:id, :call_id, :stage, :model, :policy_version,"
                    " :lexicon_version, :prompt_version, :input_sha256, cast(:output as jsonb),"
                    " :created_at, :prev_hash, :row_hash)"
                ),
                [{**r, "output": "{}"} for r in rows],
            )
            await db.commit()

        async with factory() as db:
            began = time.perf_counter()
            result = await audit.verify_chain(db, "analysis_runs")
            elapsed = time.perf_counter() - began

        print(f"\nchain_verify: {result.rows} rows in {elapsed:.2f}s")
        assert result.rows >= 10_000
        assert result.ok, result.reason
        assert elapsed < 10.0, f"{result.rows} rows took {elapsed:.2f}s"
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_concurrent_appends_produce_a_chain_not_a_fork() -> None:
    """Computing `prev_hash` is a read-modify-write.

    Without the advisory lock two appenders both read the same head and write
    rows sharing a `prev_hash` — a fork that every later verification reports as
    a break, caused by nothing but concurrency.
    """
    import asyncio

    _skip_without_db()
    engine, factory = await _engine()
    try:
        call_id = await _seed_call(factory)

        async def appender() -> None:
            async with factory() as db:
                await audit.append(db, _run(call_id))
                await db.commit()

        await asyncio.gather(*(appender() for _ in range(12)))

        async with factory() as db:
            result = await audit.verify_chain(db, "analysis_runs")
        assert result.ok, f"{result.reason} at seq {result.first_break_seq}"
        assert result.rows >= 12
    finally:
        await engine.dispose()
