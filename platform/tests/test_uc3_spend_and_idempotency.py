"""Three uc3 controls that are only visible from outside the code that implements them.

**A retried disposition.** `dispositions` is append-only, hash-chained, and the
application's database role holds INSERT and SELECT on it and nothing else, so a
duplicate ruling caused by a page refresh or a proxy retry is permanent. The
`Idempotency-Key` header is the guard, and "it did not append" is asserted on the
call count of `audit.append` itself rather than inferred from a status code --
a handler that wrote a second row and then returned the first row's receipt
would pass the weaker check.

**The per-call spend scope.** `budget.session_scope` is a ContextVar, so the
question that matters is not "was the scope entered" but "what did the vendor
call see when it ran, and did two concurrent calls see different things". Both
fakes here record `budget.current_session_id()` from inside the call.

**`TeeSink`.** uc3's ingestion passed a bare `MemorySink` to `AdapterRuntime`,
which *replaces* the default sink rather than adding to it, so every Saaras call
uc3 ever made emitted no Langfuse span -- against CLAUDE.md's "every adapter call
emits a Langfuse span" -- while still being billed. The last test asserts the tee
is actually wired into the adapter the app builds, because that is the bug.

No database, no network, no vendor. `default_sink` is patched so no Langfuse
client is ever constructed.
"""

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from comms_surveillance import api, detector, ingest
from comms_surveillance.auth import AUTHENTICATED, ROLE_REVIEWER
from comms_surveillance.detector import DetectorSettings, Flags, Triage
from comms_surveillance.lexicon.matcher import Segment
from fastapi.testclient import TestClient
from indic_platform.adapters import budget
from indic_platform.adapters.base import TranscriptSegment as AdapterSegment
from indic_platform.db.models import Call, Disposition, Flag
from indic_platform.obs import langfuse
from indic_platform.obs.langfuse import MemorySink, TeeSink
from sqlalchemy.exc import IntegrityError
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser
from starlette.middleware.authentication import AuthenticationMiddleware

FLAG_ID = "11111111-1111-4111-8111-111111111111"
OTHER_FLAG_ID = "44444444-4444-4444-8444-444444444444"
CALL_ID = "22222222-2222-4222-8222-222222222222"
RUN_ID = "55555555-5555-4555-8555-555555555555"

BODY = {"disposition": "confirmed", "note": "spoke to the desk head"}
CHANGED_MIND = {"disposition": "false_positive", "note": "cleared with the desk"}


def url(flag_id: str) -> str:
    return f"/flags/{flag_id}/dispositions"


# --- a verified subject, without an identity provider --------------------------


class _Verified(SimpleUser):
    @property
    def identity(self) -> str:
        return self.username


def client(*scopes: str, identity: str = "asha@example.test") -> TestClient:
    class Backend(AuthenticationBackend):
        async def authenticate(self, conn: Any) -> tuple[AuthCredentials, SimpleUser]:
            return AuthCredentials([AUTHENTICATED, *scopes]), _Verified(identity)

    return TestClient(AuthenticationMiddleware(api.app, backend=Backend()))


def reviewer() -> TestClient:
    return client(ROLE_REVIEWER)


# --- a session that behaves like the dispositions table ------------------------


class _Scalars:
    def __init__(self, rows: list[Disposition]) -> None:
        self._rows = rows

    def first(self) -> Disposition | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Disposition]:
        return list(self._rows)


class FakeSession:
    """Enough AsyncSession for `create_disposition`, with a real row store.

    `scalars` resolves the handler's `(flag_id, idempotency_key)` lookup against
    the rows this session has actually accumulated, so a replay is found because
    the first write happened -- not because a mock was told to return one.

    `blind_lookups` makes the next N lookups miss. That is how the insert race is
    staged: the pre-check runs before the losing transaction has anything to find,
    which is exactly the window the database constraint exists to close.
    """

    def __init__(self, objects: dict[tuple[str, str], Any]) -> None:
        self.objects = objects
        self.rows: list[Disposition] = []
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.blind_lookups = 0

    async def get(self, model: Any, pk: Any) -> Any:
        return self.objects.get((model.__name__, str(pk)))

    async def scalars(self, statement: Any) -> _Scalars:
        params = statement.compile().params
        flag_id = str(params.get("flag_id_1"))
        key = params.get("idempotency_key_1")
        if self.blind_lookups > 0:
            self.blind_lookups -= 1
            return _Scalars([])
        return _Scalars(
            [row for row in self.rows if str(row.flag_id) == flag_id and row.idempotency_key == key]
        )

    def add(self, row: Any) -> None:  # must never be used for a chained table
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def a_flag(flag_id: str) -> Flag:
    return Flag(
        id=uuid.UUID(flag_id),
        call_id=uuid.UUID(CALL_ID),
        run_id=uuid.UUID(RUN_ID),
        category="guaranteed_returns",
        severity="high",
        speaker="agent",
        start_ms=4200,
        evidence_span="bara percent return guaranteed hai",
    )


def appender(session: FakeSession) -> AsyncMock:
    """`audit.append` as far as the handler can tell: it fills in the chain
    fields and makes the row visible to the next lookup. Wrapped in an AsyncMock
    so "did not append" is a call count rather than an inference."""
    counter = iter(range(1, 1_000))

    async def append(_session: Any, row: Disposition) -> Disposition:
        row.id = uuid.uuid4()
        row.seq = next(counter)
        row.row_hash = f"{row.seq:064d}"
        session.rows.append(row)
        return row

    return AsyncMock(side_effect=append)


@dataclass
class Wiring:
    session: FakeSession
    append: AsyncMock


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[Wiring]:
    session = FakeSession(
        {("Flag", FLAG_ID): a_flag(FLAG_ID), ("Flag", OTHER_FLAG_ID): a_flag(OTHER_FLAG_ID)}
    )
    append = appender(session)
    monkeypatch.setattr(api.audit, "append", append)
    api.app.dependency_overrides[api.db_session] = lambda: session
    yield Wiring(session=session, append=append)
    api.app.dependency_overrides.clear()


def duplicate_key_error() -> IntegrityError:
    return IntegrityError(
        "INSERT INTO dispositions ...",
        {},
        Exception(
            'duplicate key value violates unique constraint "uq_dispositions_flag_idempotency_key"'
        ),
    )


# --- part 1: the disposition's idempotency key ---------------------------------


def test_a_first_keyed_write_is_a_new_row_and_says_so(wired: Wiring) -> None:
    """Prevents a guard that is so eager it turns the first request into a replay.

    A handler that reported `replayed: true` on a write it had just performed
    would tell a client its decision was already recorded by somebody else.
    """
    response = reviewer().post(url(FLAG_ID), json=BODY, headers={"Idempotency-Key": "retry-1"})

    assert response.status_code == 201
    assert response.json()["replayed"] is False
    assert wired.append.await_count == 1
    assert len(wired.session.rows) == 1
    assert wired.session.rows[0].idempotency_key == "retry-1"
    assert wired.session.rows[0].disposition == "confirmed"
    assert wired.session.commits == 1
    # Never around the side of the chain.
    assert wired.session.added == []


def test_a_replayed_key_returns_the_original_receipt_and_appends_nothing(
    wired: Wiring,
) -> None:
    """Prevents the permanent duplicate ruling a page refresh used to cause.

    The second POST carries a *different* body, so a handler that appended and
    then returned the new row's receipt fails on the receipt fields, and one
    that appended before returning the original receipt fails on the append
    count. Only "did not write" passes both.
    """
    session = reviewer()
    first = session.post(url(FLAG_ID), json=BODY, headers={"Idempotency-Key": "retry-2"})
    second = session.post(url(FLAG_ID), json=CHANGED_MIND, headers={"Idempotency-Key": "retry-2"})

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    receipt = ("disposition_id", "seq", "row_hash")
    assert {k: second.json()[k] for k in receipt} == {k: first.json()[k] for k in receipt}

    assert wired.append.await_count == 1, "a replay must not reach the hash chain at all"
    assert len(wired.session.rows) == 1
    assert wired.session.rows[0].disposition == "confirmed", "the original ruling stands"
    assert wired.session.commits == 1, "there is nothing to commit on a replay"


def test_an_idempotency_key_is_scoped_to_its_flag(wired: Wiring) -> None:
    """Prevents a global key namespace silently discarding a second flag's ruling.

    Clients reuse request ids -- a browser tab id, a batch id. If the key were
    global rather than `(flag_id, key)`, a reviewer working through a queue with
    one key would have every ruling after the first vanish into a replay.
    """
    session = reviewer()
    first = session.post(url(FLAG_ID), json=BODY, headers={"Idempotency-Key": "shared"})
    second = session.post(url(OTHER_FLAG_ID), json=BODY, headers={"Idempotency-Key": "shared"})

    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["replayed"] is False and second.json()["replayed"] is False
    assert first.json()["disposition_id"] != second.json()["disposition_id"]
    assert wired.append.await_count == 2
    assert {str(row.flag_id) for row in wired.session.rows} == {FLAG_ID, OTHER_FLAG_ID}


def test_without_a_key_a_changed_mind_is_still_a_new_row(wired: Wiring) -> None:
    """Prevents the idempotency work from collapsing the append-only behaviour.

    `dispositions` exists so that a reviewer who changes their mind leaves both
    rulings behind. A key derived from the body, or a default key, would turn
    the second ruling into a replay of the first and destroy the audit trail.
    """
    session = reviewer()
    first = session.post(url(FLAG_ID), json=BODY)
    second = session.post(url(FLAG_ID), json=CHANGED_MIND)

    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["disposition_id"] != second.json()["disposition_id"]
    assert wired.append.await_count == 2
    assert [row.disposition for row in wired.session.rows] == ["confirmed", "false_positive"]
    assert [row.idempotency_key for row in wired.session.rows] == [None, None]


def test_a_racing_duplicate_resolves_to_the_winners_receipt(wired: Wiring) -> None:
    """Prevents a retry that loses a race from surfacing as a 500.

    Two retries can both pass the pre-check before either commits; the database
    constraint catches the loser. The loser must roll back, re-read and answer
    with the winner's receipt -- both callers asked for one ruling and there is
    one ruling -- rather than returning an error for a decision that *was*
    recorded.
    """
    winner = Disposition(
        id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        flag_id=uuid.UUID(FLAG_ID),
        disposition="confirmed",
        note="first one home",
        reviewer_id="asha@example.test",
        idempotency_key="race-1",
        seq=41,
        row_hash="f" * 64,
    )
    wired.session.rows.append(winner)
    # The pre-check runs inside the window where the winner is not yet visible.
    wired.session.blind_lookups = 1
    wired.append.side_effect = duplicate_key_error()

    response = reviewer().post(url(FLAG_ID), json=BODY, headers={"Idempotency-Key": "race-1"})

    assert response.status_code == 200
    assert response.json() == {
        "disposition_id": str(winner.id),
        "seq": 41,
        "row_hash": "f" * 64,
        "replayed": True,
    }
    assert wired.session.rollbacks == 1, "the session is unusable until the failure is rolled back"
    assert wired.session.commits == 0


def test_an_integrity_error_with_no_key_is_not_disguised_as_a_replay(wired: Wiring) -> None:
    """Prevents a genuine constraint failure being reported as a successful write.

    Without a key there is no duplicate to resolve to, so the only honest answer
    is the error. Swallowing it would mean a foreign-key or check-constraint
    violation returned a receipt for a row that does not exist.
    """
    wired.append.side_effect = duplicate_key_error()

    with pytest.raises(IntegrityError):
        reviewer().post(url(FLAG_ID), json=BODY)

    assert wired.session.rollbacks == 1
    assert wired.session.commits == 0


def test_an_integrity_error_with_a_key_but_no_winner_is_re_raised(wired: Wiring) -> None:
    """Prevents `uq_dispositions_flag_idempotency_key` being assumed to be the
    constraint that fired. A key in the request does not make every integrity
    failure a duplicate: if the re-read finds no row under that key, something
    else rejected the insert and the caller must hear about it."""
    wired.append.side_effect = duplicate_key_error()
    # Pre-check misses, and the re-read after the rollback finds nothing either.
    wired.session.blind_lookups = 2

    with pytest.raises(IntegrityError):
        reviewer().post(url(FLAG_ID), json=BODY, headers={"Idempotency-Key": "orphan"})

    assert wired.session.rollbacks == 1
    assert wired.session.rows == []


def test_an_over_long_key_is_refused_before_the_handler_runs(wired: Wiring) -> None:
    """Prevents a 128-character column being overrun by a caller-supplied header.

    Truncating to fit would be worse than refusing: two different long keys
    would share a prefix and collapse into each other's replays.
    """
    at_limit = reviewer().post(url(FLAG_ID), json=BODY, headers={"Idempotency-Key": "k" * 128})
    assert at_limit.status_code == 201

    too_long = reviewer().post(
        url(OTHER_FLAG_ID), json=BODY, headers={"Idempotency-Key": "k" * 129}
    )
    assert too_long.status_code == 422
    assert wired.append.await_count == 1, "the rejected request never reached the chain"


def test_a_whitespace_only_key_is_treated_as_absent(wired: Wiring) -> None:
    """Prevents a proxy's blank header from becoming a de-duplication token.

    "Absent" has to hold in the database as well as in the pre-check. The column
    is nullable precisely because PostgreSQL treats NULLs as distinct, so unkeyed
    writes stay unconstrained; the empty string is not distinct, and it is the
    one value that makes `uq_dispositions_flag_idempotency_key` bite. Persisting
    `""` therefore means a second ruling on the same flag from a client that
    sends a whitespace header hits the constraint, takes the `if not key: raise`
    arm, and comes back a 500 with the changed mind lost -- on an append-only
    table this service cannot repair.
    """
    session = reviewer()
    first = session.post(url(FLAG_ID), json=BODY, headers={"Idempotency-Key": "   "})
    second = session.post(url(FLAG_ID), json=CHANGED_MIND, headers={"Idempotency-Key": "   "})

    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["replayed"] is False and second.json()["replayed"] is False
    assert wired.append.await_count == 2
    assert [row.idempotency_key for row in wired.session.rows] == [None, None], (
        "a blank key must reach the column as NULL, not as the empty string: "
        "`key = idempotency_key.strip() if idempotency_key else None` yields '' for "
        "a whitespace-only header, which the unique constraint does not treat as absent"
    )


# --- part 2: the per-call spend scope ------------------------------------------


SETTINGS = DetectorSettings(theta=60, qa_sample_rate=0.0, canary="CANARY_scope_only")
# Nothing in the lexicon, so no Stage 0 floor flag and no English rendering call:
# the vendor calls the fakes below see are exactly triage and deep analysis.
BENIGN = [Segment(text="Thanks, talk tomorrow.", speaker="SPEAKER_00")]


class ScopeRecordingClaude:
    """Records the spend scope at the moment of the vendor call, not around it."""

    def __init__(
        self, *replies: Any, barrier: asyncio.Barrier | None = None, fail: bool = False
    ) -> None:
        self.replies = list(replies)
        self.sessions: list[str | None] = []
        self.barrier = barrier
        self.fail = fail

    async def structured(self, *, system: str, user: str, schema: Any, model: str) -> Any:
        if self.barrier is not None and schema is Triage:
            # Both calls are suspended inside their own scope at the same instant.
            await self.barrier.wait()
        self.sessions.append(budget.current_session_id())
        if self.fail:
            raise RuntimeError("vendor unavailable")
        return self.replies.pop(0)


async def test_analysis_charges_every_vendor_call_to_the_call_id() -> None:
    """Prevents uc3's Claude legs charging to no session at all.

    Without a scope `AdapterRuntime.session()` returns None, the session cap is
    simply absent from the scope list, and one pathological transcript is bounded
    only by the whole day's budget.
    """
    claude = ScopeRecordingClaude(Triage(risk_score=99), Flags(flags=[]))
    out = await detector.analyse("call-scope-1", BENIGN, client=claude, settings=SETTINGS)

    assert out.escalation.escalate, "both vendor legs must have run for this to prove anything"
    assert claude.sessions == ["call-scope-1", "call-scope-1"]
    assert budget.current_session_id() is None, "the scope must not outlive the call"


async def test_the_analysis_spend_scope_is_reset_when_the_vendor_raises() -> None:
    """Prevents a failed call leaving its id charged to everything that follows.

    A scope reset only on the happy path would make the next call in the same
    worker task charge to the previous call's session counter -- and be refused
    by a cap it never contributed to.
    """
    claude = ScopeRecordingClaude(fail=True)

    with pytest.raises(RuntimeError):
        await detector.analyse("call-scope-2", BENIGN, client=claude, settings=SETTINGS)

    assert claude.sessions == ["call-scope-2"]
    assert budget.current_session_id() is None


async def test_concurrent_analyses_do_not_share_a_spend_scope() -> None:
    """Prevents one call's spend being charged to another's cap.

    This is the property the ContextVar exists for. Both calls are held at a
    barrier inside their own scope, so a module-global or a plain attribute
    would show the same id to both fakes.
    """
    barrier = asyncio.Barrier(2)
    clients = {
        call_id: ScopeRecordingClaude(Triage(risk_score=1), barrier=barrier)
        for call_id in ("call-a", "call-b")
    }

    async with asyncio.timeout(10):
        await asyncio.gather(
            *(
                detector.analyse(call_id, BENIGN, client=claude, settings=SETTINGS)
                for call_id, claude in clients.items()
            )
        )

    for call_id, claude in clients.items():
        assert claude.sessions == [call_id]
    assert budget.current_session_id() is None


class ScopeRecordingSTT:
    """A Saaras stand-in that reports the spend scope its call ran under."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sessions: list[str | None] = []
        self.fail = fail
        self.segments = [
            AdapterSegment(
                start_ms=0, end_ms=2000, speaker="SPEAKER_00", text="नमस्ते", language="hi-IN"
            )
        ]

    async def batch(
        self, uri: str, *, language: str = "auto", diarize: bool = False
    ) -> list[AdapterSegment]:
        self.sessions.append(budget.current_session_id())
        if self.fail:
            raise OSError("object unreadable")
        return list(self.segments)


class FakeCallSession:
    """Enough AsyncSession for `transcribe_call`: one call row, no database."""

    def __init__(self, call: Call) -> None:
        self.call = call
        self.added: list[Any] = []
        self.executed: list[Any] = []

    async def get(self, model: Any, pk: Any) -> Any:
        return self.call

    async def execute(self, statement: Any, params: Any = None) -> None:
        self.executed.append(statement)

    def add(self, row: Any) -> None:
        self.added.append(row)


def a_call(call_id: uuid.UUID) -> Call:
    return Call(
        id=call_id,
        source_uri="file:///recordings/x.wav",
        source_key="recordings/x.wav",
        status="pending",
        stt_cost_inr=Decimal("0"),
    )


async def test_transcription_charges_the_stt_call_to_the_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents a nightly batch's Saaras spend charging to no session at all.

    One recording is uc3's unit of work; without the scope, a ten-hour file or a
    retry storm is bounded only by the day cap.
    """
    monkeypatch.delenv("SARVAM_TRANSLITERATE", raising=False)
    call_id = uuid.uuid4()
    stt = ScopeRecordingSTT()
    session = FakeCallSession(a_call(call_id))

    turns = await ingest.transcribe_call(session, call_id, "/recordings/x.wav", stt=stt)  # type: ignore[arg-type]

    assert turns == 1
    assert stt.sessions == [str(call_id)]
    assert budget.current_session_id() is None


async def test_the_transcription_spend_scope_is_reset_when_stt_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents a failed recording leaking its id onto the next one in the sweep.

    `ingest_prefix` catches the failure and moves on in the same task, so a scope
    that survived the exception would charge the following recording's Saaras
    minutes to the file that could not be read.
    """
    monkeypatch.delenv("SARVAM_TRANSLITERATE", raising=False)
    call_id = uuid.uuid4()
    stt = ScopeRecordingSTT(fail=True)
    session = FakeCallSession(a_call(call_id))

    with pytest.raises(OSError):
        await ingest.transcribe_call(session, call_id, "/recordings/x.wav", stt=stt)  # type: ignore[arg-type]

    assert stt.sessions == [str(call_id)]
    assert budget.current_session_id() is None


# --- part 3: TeeSink ------------------------------------------------------------


RECORD = {
    "vendor": "sarvam",
    "capability": "stt",
    "model": ingest.DIARIZE_MODEL,
    "units": {"seconds": 60},
    "cost_inr": 0.75,
    "cost_usd": 0.008,
}


def test_a_record_reaches_every_sink_of_a_tee() -> None:
    """Prevents a fan-out that quietly writes to only the first sink -- which is
    the shape of the original bug, where cost accounting worked and the span
    never appeared."""
    accounting, observability = MemorySink(), MemorySink()

    TeeSink(accounting, observability).emit(RECORD)

    assert accounting.records == [RECORD]
    assert observability.records == [RECORD]


def test_a_failing_sink_neither_stops_the_others_nor_the_call() -> None:
    """Prevents observability sitting in the critical path of a billed call.

    A Langfuse outage must not raise out of an adapter that already spent money
    with the vendor, and must not cost the local cost record either. The broken
    sink is deliberately first, so ordering cannot hide the defect.
    """

    class Broken:
        def emit(self, record: dict[str, Any]) -> None:
            raise RuntimeError("langfuse is unreachable")

    accounting = MemorySink()

    TeeSink(Broken(), accounting).emit(RECORD)  # must not raise

    assert accounting.records == [RECORD]


def test_uc3_stt_tees_its_records_into_the_default_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents the regression this tee was introduced for.

    `stt_with_sink` used to hand `AdapterRuntime` a bare `MemorySink`, which
    *replaces* the default sink rather than adding to it, so every Saaras call
    uc3 made emitted no Langfuse span while still being billed. Asserting the
    returned adapter's sink is a tee containing both the returned memory sink and
    the default one is the only way to see that from outside.
    """
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")
    observability = MemorySink()
    monkeypatch.setattr(langfuse, "default_sink", lambda: observability)

    stt, accounting = ingest.stt_with_sink()

    tee = stt.runtime.sink
    assert isinstance(tee, TeeSink), "a private sink must be added to the default, not replace it"
    assert accounting in tee.sinks, "per-call cost attribution needs its own slice"
    assert observability in tee.sinks, "every adapter call emits a Langfuse span"

    tee.emit(RECORD)
    assert accounting.records == [RECORD]
    assert observability.records == [RECORD]
