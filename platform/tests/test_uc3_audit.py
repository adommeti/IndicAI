"""The append-only, hash-chained audit trail (PRD E8).

The pure tests pin the hash contract. The integration tests are the ones that
matter for the acceptance criteria, because the properties being claimed --
"a superuser edit is detected", "the app role cannot UPDATE" -- are properties
of a real Postgres, not of Python.
"""

import json
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


def test_column_defaults_are_applied_before_the_row_is_hashed() -> None:
    """The bug CI caught, and the reason a pure test now guards it.

    SQLAlchemy fills Python-side defaults (`default=uuid.uuid4`, `default=""`)
    when it emits the INSERT, which is *after* `append` computes the hash. The
    row that got hashed had `id=None`; the row that got stored had a UUID. Every
    chain broke at its first row with "row_hash does not match the row content"
    -- indistinguishable from real tampering, which is the worst way for this to
    present: a control that cries wolf is a control people learn to ignore.

    This needs no database: it is a property of the object, and the whole point
    is that it should never have taken a Postgres run to find.
    """
    from indic_platform.db.models import AnalysisRun, Disposition, Flag

    for model, kwargs in (
        (AnalysisRun, {"call_id": uuidlib.uuid4(), "stage": "triage", "model": "m"}),
        (
            Flag,
            {
                "call_id": uuidlib.uuid4(),
                "run_id": uuidlib.uuid4(),
                "category": "conduct",
                "severity": "low",
                "evidence_span": "x",
            },
        ),
        (
            Disposition,
            {"flag_id": uuidlib.uuid4(), "disposition": "confirmed", "reviewer_id": "r"},
        ),
    ):
        row = model(**kwargs)
        audit.materialise_defaults(row)
        unset = [
            column.name
            for column in row.__table__.columns
            if column.name not in audit.NOT_HASHED and getattr(row, column.name, None) is None
        ]
        assert not unset, f"{model.__tablename__} would hash a row missing {unset}"


def test_hashing_the_same_row_twice_agrees_after_defaults_are_applied() -> None:
    """The round trip the chain depends on: hash, store, read back, re-hash."""
    from indic_platform.db.models import AnalysisRun

    row = AnalysisRun(call_id=uuidlib.uuid4(), stage="triage", model="m")
    audit.materialise_defaults(row)
    first = audit.row_hash("", audit.row_payload(row))
    # A second call must change nothing: defaults are applied once, not re-drawn.
    audit.materialise_defaults(row)
    assert audit.row_hash("", audit.row_payload(row)) == first


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
    break. That is the whole reason the chain exists -- the app role cannot
    UPDATE (see below), so the threat model is somebody with more privilege, and
    against them detection is the control.

    Everything happens inside one transaction that is rolled back. A tamper test
    that commits leaves the shared chain broken for every test that runs after
    it, which is how a single deliberate break turns into six spurious failures.
    """
    from indic_platform.db.models import AnalysisRun, Call
    from sqlalchemy import select, text

    _skip_without_db()
    engine, factory = await _engine()
    try:
        async with factory() as db:
            call_id = uuidlib.uuid4()
            db.add(
                Call(
                    id=call_id,
                    source_uri="s3://t/x.wav",
                    source_key=f"audit/{call_id}.wav",
                    status="transcribed",
                )
            )
            await db.flush()
            for stage in ("triage", "deep_analysis", "triage"):
                await audit.append(db, _run(call_id, stage))
            await db.flush()

            before = await audit.verify_chain(db, "analysis_runs")
            assert before.ok, before.reason

            # Tamper: change a stored field without touching the hashes, exactly
            # as somebody editing the audit trail by hand would.
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

            after = await audit.verify_chain(db, "analysis_runs")
            assert after.ok is False
            assert after.first_break_id == str(target)
            assert "edited" in after.reason

            # Reverting the edit makes the chain verify again: the break is a
            # property of the content, not a latch that stays set.
            await db.execute(
                text("update analysis_runs set model = :m where id = :i"),
                {"m": "claude-sonnet-5", "i": target},
            )
            assert (await audit.verify_chain(db, "analysis_runs")).ok

            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_a_deleted_row_is_detected_as_a_break() -> None:
    """Deletion breaks the link, not the row: the *next* row is where it shows."""
    from indic_platform.db.models import AnalysisRun, Call
    from sqlalchemy import select, text

    _skip_without_db()
    engine, factory = await _engine()
    try:
        async with factory() as db:
            call_id = uuidlib.uuid4()
            db.add(
                Call(
                    id=call_id,
                    source_uri="s3://t/y.wav",
                    source_key=f"audit/{call_id}.wav",
                    status="transcribed",
                )
            )
            await db.flush()
            for _ in range(3):
                await audit.append(db, _run(call_id))
            await db.flush()
            assert (await audit.verify_chain(db, "analysis_runs")).ok

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

            result = await audit.verify_chain(db, "analysis_runs")
            assert result.ok is False
            assert "inserted, deleted or reordered" in result.reason

            await db.rollback()
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
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
            # No grants beyond role membership: everything else this role can
            # do has to come from migration 0008, or the test is proving a
            # privilege set that no deployment actually produces.
            await db.commit()

        url = os.environ["DATABASE_URL"]
        scheme, _, rest = url.partition("://")
        _, _, hostpart = rest.rpartition("@")
        as_app = create_async_engine(f"{scheme}://{login}:test-only-not-a-secret@{hostpart}")
        try:
            async with as_app.begin() as conn:
                # It can read.
                await conn.execute(text("select count(*) from analysis_runs"))

            # And it can still append -- a role that cannot write the audit
            # trail is not least privilege, it is a broken application. This
            # also covers the identity columns: `generated always` means an
            # INSERT-only role needs no sequence grant, so migration 0008 is
            # right to grant none.
            app_factory = async_sessionmaker(as_app, expire_on_commit=False)
            async with app_factory() as db:
                appended = await audit.append(db, _run(call_id))
                await db.commit()
            assert appended.seq is not None
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

    from indic_platform.db.models import AnalysisRun
    from sqlalchemy import text

    _skip_without_db()
    engine, factory = await _engine()
    rows: list[Any] = []
    start_head = audit.GENESIS
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            start_head = await audit.head(db, "analysis_runs")

        # Build the rows through the same functions the verifier uses. A
        # hand-written dict of "the columns that get hashed" is a second copy
        # of `row_payload`, maintained by hand, and it has now been wrong twice:
        # once binding {} while hashing {"flags": []}, and once omitting the
        # `_hash_schema` key that pinning the hashed columns introduced. Both
        # times the chain correctly reported tampering and the fixture was the
        # thing at fault. Derive it instead, so the two cannot drift.
        rows = []
        prev = start_head
        for _ in range(10_000):
            row = _run(call_id)
            audit.materialise_defaults(row)
            digest = audit.row_hash(prev, audit.row_payload(row))
            row.prev_hash = prev
            row.row_hash = digest
            rows.append(row)
            prev = digest

        columns = [c.name for c in AnalysisRun.__table__.columns if c.name != "seq"]
        bound = [
            {
                name: (json.dumps(getattr(row, name)) if name == "output" else getattr(row, name))
                for name in columns
            }
            for row in rows
        ]
        async with factory() as db:
            await db.execute(
                text(
                    f"insert into analysis_runs ({', '.join(columns)}) values ("
                    + ", ".join(
                        f"cast(:{name} as jsonb)" if name == "output" else f":{name}"
                        for name in columns
                    )
                    + ")"
                ),
                bound,
            )
            await db.commit()

        async with factory() as db:
            began = time.perf_counter()
            # No anchor comparison: this measures the walk, and an anchor
            # written by some earlier test would make the number mean
            # something else.
            result = await audit.verify_chain(db, "analysis_runs", check_anchor=False)
            elapsed = time.perf_counter() - began

        print(f"\nchain_verify: {result.rows} rows in {elapsed:.2f}s")
        assert result.rows >= 10_000
        assert result.ok, result.reason
        assert elapsed < 10.0, f"{result.rows} rows took {elapsed:.2f}s"
    finally:
        # In the `finally`, not after the assertions. Ten thousand rows left
        # behind are not a tidiness problem: they are the tail of a shared
        # chain, so one failure here reappears as a break in every later test
        # that walks `analysis_runs`. That is what turned a single wrong
        # fixture into four red tests. Deleting exactly the rows this test
        # added restores the head it started from.
        if rows:
            async with factory() as db:
                await db.execute(
                    text("delete from analysis_runs where id = any(:ids)"),
                    {"ids": [row.id for row in rows]},
                )
                await db.commit()
            async with factory() as db:
                assert (await audit.head(db, "analysis_runs")) == start_head
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


# --- the alert path ----------------------------------------------------------
#
# These are pure tests on purpose. The previous version of `emit_chain_metric`
# built a record the sink could not read and raised KeyError on every call, and
# because nothing exercised it the defect survived a green CI: the alert for a
# broken audit chain had never fired once. A control nobody has run is a claim,
# not a control.


def _sink_stub(calls: list[dict[str, object]]) -> object:
    """A sink that reads exactly the keys the real LangfuseSink reads."""

    class Stub:
        def emit(self, record: dict[str, object]) -> None:
            # Mirror platform/obs/langfuse.py: any missing key is a KeyError
            # here for the same reason it would be there.
            _ = (
                f"{record['vendor']}.{record['capability']}",
                record["model"],
                record["units"],
                record["cost_usd"],
            )
            calls.append(dict(record))

    return Stub()


def test_the_metric_record_is_one_the_real_sink_can_read(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "indic_platform.obs.langfuse.default_sink", lambda: _sink_stub(calls), raising=True
    )
    summary = audit.summarise(
        [
            audit.ChainResult(table="analysis_runs", rows=3, ok=True),
            audit.ChainResult(table="flags", rows=1, ok=False, reason="row was edited"),
        ]
    )
    assert audit.emit_chain_metric(summary) is True
    (record,) = calls
    assert record["value"] == 1
    assert record["status"] == "broken"
    assert record["tables"] == {"analysis_runs": True, "flags": False}


def test_the_metric_never_carries_row_content_or_the_head_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A published head is what a tamperer needs in order to reproduce it."""
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        "indic_platform.obs.langfuse.default_sink", lambda: _sink_stub(calls), raising=True
    )
    result = audit.ChainResult(table="flags", rows=2, ok=True)
    result.head_hash = "a" * 64
    audit.emit_chain_metric(audit.summarise([result]))
    (record,) = calls
    assert "a" * 64 not in json.dumps(record, default=str)


def test_a_monitoring_outage_does_not_stop_the_verification_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken:
        def emit(self, record: dict[str, object]) -> None:
            raise RuntimeError("langfuse unreachable")

    monkeypatch.setattr("indic_platform.obs.langfuse.default_sink", lambda: Broken(), raising=True)
    summary = audit.summarise([audit.ChainResult(table="flags", rows=1, ok=True)])
    # Returns False rather than raising: the job still logs, which is the
    # channel that works when the metrics vendor does not.
    assert audit.emit_chain_metric(summary) is False


def test_the_anchor_table_is_declared_where_alembic_can_see_it() -> None:
    """Registered from `audit.py` alone, `alembic check` proposes to drop it."""
    from indic_platform.db.models import Base

    assert audit.ANCHOR_TABLE_NAME in Base.metadata.tables
    assert audit.ANCHORS is Base.metadata.tables[audit.ANCHOR_TABLE_NAME]
    assert audit.ANCHOR_TABLE_NAME not in audit.CHAINED


@pytest.mark.integration
async def test_a_truncated_tail_is_detected_although_the_chain_still_walks_clean() -> None:
    """The hole the walk cannot see on its own.

    Delete rows off the end and what remains is a perfectly valid chain: every
    `prev_hash` matches, every `row_hash` recomputes. Only the anchor knows the
    chain used to be longer. This is the realistic shape of an audit-trail
    attack -- nobody deletes the head, they delete what they did last night.

    Order matters, and getting it wrong is how this test first failed: the row
    that disappears has to be one the anchor had already counted. Appending a
    row *after* anchoring and then deleting it returns the chain to exactly the
    anchored state, and the anchor is right to call that clean.

    That is also this control's honest limit, worth stating where someone will
    read it: a row appended and deleted between two nightly runs leaves no
    trace in either the count or the head. Narrowing that window means
    anchoring more often, not anchoring differently.
    """
    from sqlalchemy import text

    _skip_without_db()
    engine, factory = await _engine()
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            kept = await audit.append(db, _run(call_id))
            await db.commit()
            kept_id = kept.id

        async with factory() as db:
            doomed = await audit.append(db, _run(call_id))
            await db.commit()
            doomed_id = doomed.id

        # Last night's verification, which counted both rows.
        async with factory() as db:
            before = await audit.verify_chain(db, "analysis_runs")
            assert before.ok, before.reason
            await audit.write_anchor(
                db, "analysis_runs", head_hash=before.head_hash, rows=before.rows
            )
            await db.commit()

        async with factory() as db:
            # Superuser truncation of an anchored row.
            await db.execute(text("delete from analysis_runs where id = :id"), {"id": doomed_id})
            await db.commit()

            # Without the anchor this reads as clean, which is the whole point.
            walk_only = await audit.verify_chain(db, "analysis_runs", check_anchor=False)
            assert walk_only.ok, walk_only.reason
            assert walk_only.rows == before.rows - 1

            # With it, the missing row is visible.
            anchored = await audit.verify_chain(db, "analysis_runs")
            assert not anchored.ok
            assert anchored.anchor_ok is False
            assert "removed" in anchored.reason or "rewritten" in anchored.reason

        # Leave the chain as this test found it, so the anchor and the rows
        # agree again for whatever runs next.
        async with factory() as db:
            await db.execute(text("delete from analysis_runs where id = :id"), {"id": kept_id})
            await db.execute(
                text("delete from audit_chain_anchors where table_name = 'analysis_runs'")
            )
            await db.commit()
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_the_nightly_job_only_moves_the_anchor_forward_on_a_clean_chain() -> None:
    """An anchor updated over a break would launder the break into the baseline."""
    from sqlalchemy import text

    _skip_without_db()
    engine, factory = await _engine()
    try:
        call_id = await _seed_call(factory)
        async with factory() as db:
            row = await audit.append(db, _run(call_id))
            await db.commit()
            row_id = row.id

        clean = await audit.run_chain_verify(lambda: factory())
        assert clean["ok"], clean
        assert "analysis_runs" in clean["anchors_updated"]

        async with factory() as db:
            anchor_before = await audit.read_anchor(db, "analysis_runs")
        assert anchor_before is not None

        async with factory() as db:
            await db.execute(
                text("update analysis_runs set model = 'tampered' where id = :id"),
                {"id": row_id},
            )
            await db.commit()

        broken = await audit.run_chain_verify(lambda: factory())
        assert not broken["ok"]
        assert "analysis_runs" not in broken["anchors_updated"]

        async with factory() as db:
            anchor_after = await audit.read_anchor(db, "analysis_runs")
        assert anchor_after is not None
        assert anchor_after.head_hash == anchor_before.head_hash

        # Put the row back so later tests in this session see a clean chain.
        async with factory() as db:
            await db.execute(text("delete from analysis_runs where id = :id"), {"id": row_id})
            await db.commit()
    finally:
        await engine.dispose()


def test_a_payload_built_by_hand_does_not_match_one_built_by_row_payload() -> None:
    """Fixtures must hash through `row_payload`, never a hand-written dict.

    Twice now a test has assembled "the columns that get hashed" itself, and
    twice the chain has correctly called the result tampering: first when it
    bound `{}` while hashing `{"flags": []}`, then when pinning the hashed
    columns added `_hash_schema` and the hand-built copy did not have it. Both
    cost a full integration run to find, because the disagreement only shows up
    against a real database.

    This is that failure made pure: if the payload ever grows a key a hand-built
    dict would miss, this goes red in milliseconds rather than in CI.
    """
    from indic_platform.db.models import AnalysisRun

    row = AnalysisRun(call_id=uuidlib.uuid4(), stage="triage", model="m")
    audit.materialise_defaults(row)

    derived = audit.row_payload(row)
    by_hand = {
        name: getattr(row, name)
        for name in AnalysisRun.__table__.columns.keys()
        if name not in audit.NOT_HASHED
    }

    assert "_hash_schema" in derived
    assert "_hash_schema" not in by_hand
    assert audit.row_hash("", derived) != audit.row_hash("", by_hand)
