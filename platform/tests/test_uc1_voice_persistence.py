"""Voice turns land in the session they were given, and their latencies land on them.

The defect these tests pin: `helpdesk_agent.graph.decide` is the *eval* entry point. It
takes `initial_state`'s defaults, which mint a fresh `session_id` and attribute the turn
to the literal employee ``"eval"``. Injected into the voice pipeline as the production
decision stage it therefore opened a brand new, unowned session row per utterance, with
`turn_index` permanently 0 -- so `run_session`'s `on_turn` hook had no row it could
address and P5's "record per-stage latencies into turns.latency_ms" had nowhere to write.
`session_decide` binds the stage to the real session and employee; `record_voice_latency`
merges the voice stages onto the row that turn wrote.

The pure tests pin the closure's counting and the merge arithmetic. The integration
tests are the ones that matter for the acceptance criterion, because "the latency is on
the right row" and "the graph's own keys survived" are properties of a real Postgres row,
not of Python. They are marked `integration`: CI provisions Postgres for that marker and
fails the job on any skip, so they must genuinely run there.
"""

import os
import uuid
from typing import Any

import pytest
from helpdesk_agent import graph, persistence
from helpdesk_agent.graph import Decision, TurnState, decide, session_decide
from helpdesk_agent.persistence import merge_latency, record_voice_latency
from indic_platform.adapters.vectorstore import Chunk

# --- a fake `run_turn`, so the closure is provable without a database -----------

CHUNK = Chunk(id="c1", article_id="KB-1", title="t", category="IT", text="x", aliases=["kb1"])


def _result(state: TurnState) -> TurnState:
    return {
        **state,
        "chunks": [CHUNK],
        "decision": Decision(action="clarify", reply_text="Please describe the problem."),
    }  # type: ignore[return-value]


class FakeRunTurn:
    """Records what each turn was asked to persist, and returns a decided state."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[tuple[TurnState, bool]] = []
        self.fail_first = fail_first

    async def __call__(self, state: TurnState, *, existing: bool = False) -> TurnState:
        self.calls.append((dict(state), existing))  # type: ignore[arg-type]
        if self.fail_first and len(self.calls) == 1:
            raise LookupError("Unknown helpdesk session")
        return _result(state)


# --- the closure ---------------------------------------------------------------


async def test_session_decide_binds_the_session_and_employee_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression itself, at the level where it is cheapest to see.

    `initial_state` still mints a uuid and says "eval" -- the eval runner depends on
    that and it is not changed. The bound stage overrides both fields afterwards, the
    way `api.chat_turn` already does.
    """
    fake = FakeRunTurn()
    monkeypatch.setattr(persistence, "run_turn", fake)
    session_id, employee_id = str(uuid.uuid4()), "emp-42@example.com"

    stage = session_decide(session_id, employee_id)
    await stage("printer is broken", "en-IN", [])

    state, existing = fake.calls[0]
    assert state["session_id"] == session_id
    assert state["employee_id"] == employee_id
    assert existing is False


async def test_session_decide_counts_its_own_turns_and_returns_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First turn opens the session, every later turn joins it; the index is 0-based."""
    fake = FakeRunTurn()
    monkeypatch.setattr(persistence, "run_turn", fake)
    session_id = str(uuid.uuid4())

    stage = session_decide(session_id, "emp-1")
    indices = [(await stage(text, "en-IN", []))["turn_index"] for text in ("one", "two", "three")]

    assert indices == [0, 1, 2]
    assert [existing for _, existing in fake.calls] == [False, True, True]
    assert {state["session_id"] for state, _ in fake.calls} == {session_id}


async def test_a_turn_that_failed_to_persist_does_not_advance_the_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised turn wrote nothing -- session row included -- so the next is still first.

    Counting before the await would leave the session permanently un-openable: every
    later turn would claim `existing=True` against a session row that was rolled back.
    """
    fake = FakeRunTurn(fail_first=True)
    monkeypatch.setattr(persistence, "run_turn", fake)

    stage = session_decide(str(uuid.uuid4()), "emp-1")
    with pytest.raises(LookupError):
        await stage("one", "en-IN", [])
    result = await stage("two", "en-IN", [])

    assert result["turn_index"] == 0
    assert [existing for _, existing in fake.calls] == [False, False]


async def test_the_bound_stage_keeps_the_decide_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`voice_pipeline.DecideCallable` reads these keys; only `turn_index` is added.

    Asserting key parity against `decide` itself is the drift guard: the eval entry
    point and the voice entry point must answer with the same shape.
    """
    monkeypatch.setattr(persistence, "run_turn", FakeRunTurn())

    evaluated = await decide("printer is broken", "en-IN", ["earlier utterance"])
    bound = await session_decide(str(uuid.uuid4()), "emp-1")("printer is broken", "en-IN", [])

    assert set(bound) - set(evaluated) == {"turn_index"}
    assert set(evaluated) - set(bound) == set()
    assert bound["action"] == "clarify"
    assert bound["article_ids"] == ["KB-1"]
    assert bound["model"] == graph.MODEL and bound["prompt_version"] == graph.PROMPT_VERSION


async def test_history_entries_that_are_not_dicts_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The voice pipeline sends dicts, but `decide` accepts bare strings; so must this."""
    fake = FakeRunTurn()
    monkeypatch.setattr(persistence, "run_turn", fake)

    await session_decide(str(uuid.uuid4()), "emp-1")("now", "en-IN", ["before", {"x": 1}])

    assert fake.calls[0][0]["history"] == [{"utterance": "before"}, {"x": 1}]


# --- the merge arithmetic ------------------------------------------------------


def test_merge_keeps_the_graph_keys_and_adds_the_voice_ones() -> None:
    merged = merge_latency(
        {"retrieve": 12.0, "decide": 900.0, "total": 950.0},
        {"voice.decide_ms": 1010.0, "voice.time_to_first_audio_ms": 1400.0},
    )
    assert merged == {
        "retrieve": 12.0,
        "decide": 900.0,
        "total": 950.0,
        "voice.decide_ms": 1010.0,
        "voice.time_to_first_audio_ms": 1400.0,
    }


def test_merge_does_not_reprefix_and_leaves_unmeasured_stages_absent() -> None:
    """`StageLatency.merge_into` already namespaces; a stage that did not run is absent.

    Absent means unmeasured. A 0.0 here would read as "instant" on a dashboard and be
    indistinguishable from a real measurement -- the defect CLAUDE.md forbids.
    """
    merged = merge_latency({"total": 5.0}, {"voice.stt_ms": 300.0})
    assert merged == {"total": 5.0, "voice.stt_ms": 300.0}
    assert "voice.voice.stt_ms" not in merged
    assert "voice.tts_ms" not in merged


def test_merge_tolerates_an_empty_or_missing_payload() -> None:
    assert merge_latency(None, {"voice.vad_ms": 40.0}) == {"voice.vad_ms": 40.0}
    assert merge_latency({}, {}) == {}
    assert merge_latency({"total": 1.0}, {}) == {"total": 1.0}


def test_merge_writes_json_floats() -> None:
    """Postgres JSONB stores what it is given; ints from a caller become floats here."""
    merged = merge_latency({"total": 1.0}, {"voice.tts_ms": 200})
    assert isinstance(merged["voice.tts_ms"], float)


# --- against a real Postgres ---------------------------------------------------


def _database_url() -> str:
    """Skip only when there is no database configured at all.

    Deliberately *not* the `_skip_without_db` reachability probe the older suites use:
    CI's integration job provisions Postgres and fails on any skip, so a skip there would
    hide an unreachable service. With DATABASE_URL set, an unreachable server is a
    failure, not a skip.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")
    return url


def _factory() -> tuple[Any, Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(_database_url())
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _session_row(factory: Any, session_id: uuid.UUID, employee_id: str) -> None:
    from indic_platform.db.models import Session as SessionRow

    async with factory() as db, db.begin():
        db.add(
            SessionRow(
                id=session_id,
                app="helpdesk_agent",
                metadata_json={"employee_id": employee_id},
            )
        )


async def _turn_row(factory: Any, session_id: uuid.UUID, utterance: str) -> uuid.UUID:
    """One turn, in its own transaction -- as `run_turn` writes it.

    Per-transaction matters for more than realism: `created_at` defaults to Postgres
    `now()`, which is the *transaction* timestamp, so turns written inside one
    transaction would all share it and `order_by(created_at, id)` would fall back to the
    random uuid tie-break. Real turns are separate transactions and order by time.
    """
    from indic_platform.db.models import Turn

    row = Turn(
        session_id=session_id,
        utterance=utterance,
        language="en-IN",
        decision_json={"action": "clarify", "reply_text": "..."},
        retrieval_json={"chunks": []},
        latency_ms={"retrieve": 11.0, "decide": 900.0, "total": 950.0},
        policy_version="p1",
        prompt_version="pr1",
        model="claude-sonnet-5",
        trace_id="t1",
    )
    async with factory() as db, db.begin():
        db.add(row)
    return row.id


async def _latency(factory: Any, turn_id: uuid.UUID) -> dict[str, Any]:
    from indic_platform.db.models import Turn

    async with factory() as db:
        row = await db.get(Turn, turn_id)
        assert row is not None
        return dict(row.latency_ms)


@pytest.mark.integration
async def test_a_voice_latency_merges_without_destroying_the_graphs_own_keys() -> None:
    """The acceptance criterion: the voice stages land on the turn, the graph's stay."""
    engine, factory = _factory()
    session_id = uuid.uuid4()
    try:
        await _session_row(factory, session_id, "emp-merge")
        turn_id = await _turn_row(factory, session_id, "printer broken")

        recorded = await record_voice_latency(
            str(session_id),
            0,
            {"voice.vad_ms": 40.0, "voice.stt_ms": 310.0, "voice.time_to_first_audio_ms": 1400.0},
        )

        assert recorded is True
        latency = await _latency(factory, turn_id)
        assert latency == {
            "retrieve": 11.0,
            "decide": 900.0,
            "total": 950.0,
            "voice.vad_ms": 40.0,
            "voice.stt_ms": 310.0,
            "voice.time_to_first_audio_ms": 1400.0,
        }
        print(f"voice latency merged onto turn 0: {latency}")
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_turn_index_addresses_the_right_row_among_several() -> None:
    """Three turns, the middle one updated: the index is a position, not a guess.

    The ordering is `order_by(Turn.created_at, Turn.id)` -- literally the one `run_turn`
    uses to rebuild history and to key ticket idempotency. If the two ever diverged,
    index *n* would mean a different row in each place and a latency would be filed
    against someone else's turn.
    """
    engine, factory = _factory()
    session_id = uuid.uuid4()
    try:
        await _session_row(factory, session_id, "emp-index")
        ids = [await _turn_row(factory, session_id, f"turn {i}") for i in range(3)]

        assert await record_voice_latency(str(session_id), 1, {"voice.tts_ms": 222.0}) is True

        first, middle, last = [await _latency(factory, turn_id) for turn_id in ids]
        assert middle["voice.tts_ms"] == 222.0
        assert "voice.tts_ms" not in first and "voice.tts_ms" not in last
        assert first["total"] == middle["total"] == last["total"] == 950.0
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_an_absent_row_returns_false_and_does_not_raise() -> None:
    """The eval path holds an index that was never persisted; that is legitimate.

    Both shapes of absence: a session with no turns at all, and an index past the end.
    A latency write must never be able to fail the turn it describes.
    """
    engine, factory = _factory()
    session_id = uuid.uuid4()
    try:
        assert await record_voice_latency(str(uuid.uuid4()), 0, {"voice.tts_ms": 1.0}) is False

        await _session_row(factory, session_id, "emp-absent")
        await _turn_row(factory, session_id, "only turn")
        assert await record_voice_latency(str(session_id), 5, {"voice.tts_ms": 1.0}) is False
        assert await record_voice_latency(str(session_id), -1, {"voice.tts_ms": 1.0}) is False
        assert await record_voice_latency("not-a-uuid", 0, {"voice.tts_ms": 1.0}) is False
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_session_decide_writes_its_turns_into_the_session_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression, against the database: no fresh uuid, no employee "eval".

    The vendors are the only fakes. Claude and Langfuse are not provisioned for the
    integration job (no key, no Langfuse service), and the property under test is about
    rows, not about what the model said -- so retrieval, the model and the span sink are
    deterministic stand-ins while Postgres is real.
    """
    from indic_platform.db.models import Session as SessionRow
    from indic_platform.db.models import Turn
    from sqlalchemy import select

    _fake_vendors(monkeypatch)
    engine, factory = _factory()
    session_id, employee_id = uuid.uuid4(), "emp-voice@example.com"
    try:
        stage = session_decide(str(session_id), employee_id)
        first = await stage("printer is broken", "en-IN", [])
        second = await stage("it is still broken", "en-IN", [])

        assert first["turn_index"] == 0
        assert second["turn_index"] == 1

        async with factory() as db:
            session_row = await db.get(SessionRow, session_id)
            assert session_row is not None
            assert session_row.metadata_json["employee_id"] == employee_id
            assert session_row.app == "helpdesk_agent"
            turns = list(
                (
                    await db.scalars(
                        select(Turn)
                        .where(Turn.session_id == session_id)
                        .order_by(Turn.created_at, Turn.id)
                    )
                ).all()
            )
        assert [t.utterance for t in turns] == ["printer is broken", "it is still broken"]

        # ... and the latency the pipeline learns later addresses the second turn.
        assert (
            await record_voice_latency(
                str(session_id), second["turn_index"], {"voice.time_to_first_audio_ms": 1234.0}
            )
            is True
        )
        latency = await _latency(factory, turns[1].id)
        assert latency["voice.time_to_first_audio_ms"] == 1234.0
        assert latency["total"] > 0, "the graph's own timings survived the merge"
        print(
            f"session {session_id} employee={employee_id} turns={len(turns)} "
            f"turn 1 latency_ms={latency}"
        )
    finally:
        await engine.dispose()


def _fake_vendors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic model, retrieval and span sink; Postgres stays real."""
    from helpdesk_agent.retriever import RetrievalResult
    from indic_platform.obs.langfuse import LangfuseSink

    class FakeSpan:
        trace_id = "trace-voice-persistence"

        def __enter__(self) -> "FakeSpan":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def update(self, **_: Any) -> None:
            return None

    class FakeLangfuse:
        def start_as_current_observation(self, **_: Any) -> FakeSpan:
            return FakeSpan()

        def flush(self) -> None:
            return None

    class FakeClaude:
        def __init__(self, **_: Any) -> None:
            self.client = self

        async def structured(self, **_: Any) -> Decision:
            return Decision(
                action="clarify", reply_text="Please describe the problem and what help you need."
            )

        async def close(self) -> None:
            return None

    async def retrieve(*_: Any, **__: Any) -> RetrievalResult:
        return RetrievalResult(
            chunks=[], translate_status="disabled", latency_s=0, original_latency_s=0
        )

    sink = object.__new__(LangfuseSink)
    sink.client = FakeLangfuse()  # type: ignore[assignment]
    monkeypatch.setattr(persistence, "default_sink", lambda: sink)
    monkeypatch.setattr(persistence, "Claude", FakeClaude)
    monkeypatch.setattr(persistence.Retriever, "retrieve", retrieve)
