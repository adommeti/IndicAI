"""Ticketing: file once, never invent a number, and make every retry safe.

Zammad is an in-process fake behind `httpx.MockTransport` throughout; nothing
here opens a socket. What is being tested is not Zammad's API but the three
places this code can hurt someone: filing a second ticket for a turn that was
retried, telling an employee a ticket number that does not exist, and swallowing
a bad API token as though it were an outage.

The database is faked too, and deliberately faked *pessimistically*: its `flush`
reproduces Postgres blocking a duplicate INSERT until the key's holder commits,
because that wait is what lets the loser of a race read back a settled row. The
constraint itself is proved against a real Postgres in the `integration` test at
the bottom; the fake proves the code's ordering -- that the reservation happens
before Zammad is called, so the loser never reaches it.
"""

import asyncio
import os
import uuid
from typing import Any, cast

import httpx
import pytest
from helpdesk_agent import ticketing
from helpdesk_agent.graph import Ticket
from helpdesk_agent.ticketing import (
    FILED,
    PENDING,
    SOURCE_FIELD,
    TAGS,
    Filed,
    TicketingUnauthorized,
    create_ticket,
)
from indic_platform.db.models import TicketFiling
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

TICKET = Ticket(
    title="Laptop will not connect to the office VPN",
    description="The employee reports the VPN client fails with a timeout on every attempt.",
    category="IT",
    urgency="normal",
)
# A plausible Zammad number, used to assert that a pending filing contains none
# of it -- not the string, and not a single one of its digits.
NUMBER = "89001"


# --- fake Zammad -------------------------------------------------------------


class FakeZammad:
    """Counts what was actually created, and can be told to fail."""

    def __init__(
        self,
        *,
        create_status: int = 201,
        search_status: int = 200,
        connection_error: bool = False,
        existing: list[dict[str, Any]] | None = None,
    ) -> None:
        self.create_status = create_status
        self.search_status = search_status
        self.connection_error = connection_error
        self.tickets: list[dict[str, Any]] = list(existing or [])
        self.created: list[dict[str, Any]] = []
        self.searches: list[str] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        # A real HTTP round trip suspends. The fake must too, or two gathered
        # `create_ticket` calls run one after the other and the race this file
        # exists to test never happens.
        await asyncio.sleep(0)
        if self.connection_error:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/api/v1/tickets/search":
            self.searches.append(request.url.params.get("query", ""))
            if self.search_status != 200:
                return httpx.Response(self.search_status, json={"error": "nope"})
            return httpx.Response(200, json=self.tickets)
        if request.url.path == "/api/v1/tickets" and request.method == "POST":
            import json as jsonlib

            body = cast(dict[str, Any], jsonlib.loads(request.content))
            if self.create_status >= 400:
                # Record nothing: a refused create made no ticket.
                return httpx.Response(self.create_status, json={"error": "nope"})
            self.created.append(body)
            ticket = {
                "id": 100 + len(self.created),
                "number": str(int(NUMBER) + len(self.created) - 1),
                SOURCE_FIELD: body.get(SOURCE_FIELD, ""),
            }
            self.tickets.append(ticket)
            return httpx.Response(self.create_status, json=ticket)
        return httpx.Response(404, json={"error": "unknown"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://zammad.test",
            headers={"Authorization": "Token token=test"},
            transport=httpx.MockTransport(self.handle),
        )


# --- fake database -----------------------------------------------------------


class _Held:
    """An uncommitted claim on `(session_id, turn_index)`, as a row lock."""

    def __init__(self, owner: "FakeSession", row: TicketFiling) -> None:
        self.owner = owner
        self.row = row
        self.event = asyncio.Event()


Key = tuple[uuid.UUID, int]


class FakeDb:
    def __init__(self) -> None:
        self.rows: dict[Key, TicketFiling] = {}
        self.held: dict[Key, _Held] = {}


class FakeSavepoint:
    def __init__(self, session: "FakeSession") -> None:
        self.session = session

    async def rollback(self) -> None:
        self.session.release(insert=False)

    async def commit(self) -> None:
        """Releasing a SAVEPOINT publishes nothing; the outer commit does that."""


class FakeSession:
    """Enough of `AsyncSession` to exercise a unique constraint honestly.

    The important detail is in `flush`: a duplicate INSERT does not fail
    immediately, it waits for whoever holds the key to commit or roll back, and
    only then is refused. Failing fast instead would let the loser read a row
    that is still mid-flight and report a number that had not been issued.
    """

    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.staged: list[TicketFiling] = []

    def add(self, row: TicketFiling) -> None:
        self.staged.append(row)

    async def flush(self) -> None:
        for row in self.staged:
            key: Key = (row.session_id, row.turn_index)
            held = self.db.held.get(key)
            if held is not None and held.owner is not self:
                await held.event.wait()
            if key in self.db.rows:
                self.staged.clear()
                raise IntegrityError(
                    "INSERT INTO ticket_filings", {}, Exception("uq_ticket_filings_turn")
                )
            self.db.held[key] = _Held(self, row)
        self.staged.clear()

    async def begin_nested(self) -> FakeSavepoint:
        return FakeSavepoint(self)

    async def scalar(self, statement: Any) -> TicketFiling | None:
        params = statement.compile().params
        key: Key = (params["session_id_1"], params["turn_index_1"])
        held = self.db.held.get(key)
        if held is not None and held.owner is self:
            return held.row
        return self.db.rows.get(key)

    async def commit(self) -> None:
        self.release(insert=True)

    async def rollback(self) -> None:
        self.release(insert=False)

    def release(self, *, insert: bool) -> None:
        self.staged.clear()
        for key, held in list(self.db.held.items()):
            if held.owner is not self:
                continue
            if insert:
                self.db.rows[key] = held.row
            del self.db.held[key]
            held.event.set()


def session_of(db: FakeDb) -> AsyncSession:
    return cast(AsyncSession, FakeSession(db))


class Enqueued:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def apply_async(self, args: tuple[Any, ...] = (), **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.fixture
def zammad(monkeypatch: pytest.MonkeyPatch) -> FakeZammad:
    fake = FakeZammad()
    monkeypatch.setattr(ticketing, "client", fake.client)
    return fake


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> Enqueued:
    recorder = Enqueued()
    monkeypatch.setattr(ticketing.file_pending_ticket, "apply_async", recorder.apply_async)
    return recorder


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry ladder's shape, not its wall clock."""

    async def instant(attempt: int) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(ticketing, "_backoff", instant)


# --- the happy path ----------------------------------------------------------


async def test_a_created_ticket_carries_the_tags_and_the_source_field(
    zammad: FakeZammad, queue: Enqueued
) -> None:
    """Tags and `source_session_id` are not decoration: the field is what a
    retry searches on, so it has to be on the wire, not just in our own row."""
    session_id, db = uuid.uuid4(), FakeDb()
    filed = await create_ticket(
        session_of(db),
        TICKET,
        session_id=session_id,
        turn_index=0,
        employee_id="emp-1@example.com",
    )

    assert filed == Filed(NUMBER, False, False)
    assert len(zammad.created) == 1
    body = zammad.created[0]
    assert body["group"] == ticketing.GROUP
    assert sorted(body["tags"].split(",")) == sorted(TAGS)
    assert body[SOURCE_FIELD] == f"{session_id}:0"
    assert body["title"] == TICKET.title
    assert body["priority"] == "2 normal"
    # The key is repeated in the article because a Zammad without Elasticsearch
    # searches article bodies but not custom attributes.
    assert f"{SOURCE_FIELD}: {session_id}:0" in body["article"]["body"]
    assert queue.calls == []


async def test_the_row_records_what_was_filed(zammad: FakeZammad, queue: Enqueued) -> None:
    session_id, db = uuid.uuid4(), FakeDb()
    session = session_of(db)
    await create_ticket(session, TICKET, session_id=session_id, turn_index=2, employee_id="emp-1")
    await session.commit()

    (row,) = db.rows.values()
    assert (row.status, row.ticket_number, row.attempts) == (FILED, NUMBER, 1)
    assert row.source_key == f"{session_id}:2"
    assert row.payload == TICKET.model_dump()


# --- a pending filing invents nothing ----------------------------------------


@pytest.mark.parametrize(
    "fake",
    [
        FakeZammad(create_status=503),
        FakeZammad(connection_error=True),
    ],
    ids=["5xx", "connection-error"],
)
async def test_an_outage_files_nothing_and_promises_no_number(
    fake: FakeZammad, queue: Enqueued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion, stated as strictly as it can be stated: not one
    digit of a plausible ticket number appears anywhere in what comes back."""
    monkeypatch.setattr(ticketing, "client", fake.client)
    session_id, db = uuid.uuid4(), FakeDb()
    session = session_of(db)

    filed = await create_ticket(
        session, TICKET, session_id=session_id, turn_index=0, employee_id="emp-1"
    )
    await session.commit()

    assert filed == Filed(None, True, False)
    assert filed.ticket_number is None
    rendered = repr(filed) + repr(filed.ticket_number)
    assert NUMBER not in rendered
    assert not any(character.isdigit() for character in rendered)

    (row,) = db.rows.values()
    assert row.status == PENDING
    assert row.ticket_number is None
    assert row.last_error == "TicketingUnavailable"
    assert fake.created == []

    args, kwargs = queue.calls[0]
    assert args == (str(session_id), 0)
    assert kwargs["countdown"] == ticketing.ENQUEUE_DELAY_S


async def test_a_broker_outage_does_not_lose_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pending row is the record of what is owed; enqueue failing is not."""
    fake = FakeZammad(create_status=503)
    monkeypatch.setattr(ticketing, "client", fake.client)

    def explode(args: tuple[Any, ...] = (), **kwargs: Any) -> None:
        raise OSError("broker down")

    monkeypatch.setattr(ticketing.file_pending_ticket, "apply_async", explode)
    db = FakeDb()
    session = session_of(db)
    filed = await create_ticket(
        session, TICKET, session_id=uuid.uuid4(), turn_index=0, employee_id="emp-1"
    )
    await session.commit()

    assert filed.pending and filed.ticket_number is None
    assert next(iter(db.rows.values())).status == PENDING


# --- a bad token is not an outage --------------------------------------------


async def test_a_401_is_raised_loudly_and_queues_nothing(
    queue: Enqueued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected credential does not heal by waiting. Queueing it would hide a
    one-line configuration error behind a growing backlog of pending tickets."""
    fake = FakeZammad(create_status=401)
    monkeypatch.setattr(ticketing, "client", fake.client)
    db = FakeDb()
    session = session_of(db)

    with pytest.raises(TicketingUnauthorized):
        await create_ticket(
            session, TICKET, session_id=uuid.uuid4(), turn_index=0, employee_id="emp-1"
        )
    await session.commit()

    assert queue.calls == []
    # The reservation is released, so filing can be retried once the token is
    # fixed rather than replaying a dead row forever.
    assert db.rows == {}
    assert fake.created == []


async def test_a_403_is_also_permanent(queue: Enqueued, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeZammad(create_status=403)
    monkeypatch.setattr(ticketing, "client", fake.client)
    with pytest.raises(TicketingUnauthorized):
        await create_ticket(
            session_of(FakeDb()),
            TICKET,
            session_id=uuid.uuid4(),
            turn_index=0,
            employee_id="emp-1",
        )
    assert queue.calls == []


async def test_a_422_is_a_defect_not_an_outage(
    queue: Enqueued, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeZammad(create_status=422)
    monkeypatch.setattr(ticketing, "client", fake.client)
    with pytest.raises(ticketing.TicketingRejected):
        await create_ticket(
            session_of(FakeDb()),
            TICKET,
            session_id=uuid.uuid4(),
            turn_index=0,
            employee_id="emp-1",
        )
    assert queue.calls == []


async def test_a_missing_token_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZAMMAD_TOKEN", raising=False)
    with pytest.raises(TicketingUnauthorized):
        ticketing.client()


# --- the race --------------------------------------------------------------


async def test_two_concurrent_turns_file_exactly_one_ticket(
    zammad: FakeZammad, queue: Enqueued
) -> None:
    """The retry a real deployment produces: the same turn, twice, at once.

    The assertion that matters is `len(zammad.created) == 1` -- one ticket in
    the IT queue -- not merely one row in our table. A read-then-write guard
    passes the row assertion and fails this one.
    """
    session_id, db = uuid.uuid4(), FakeDb()

    async def attempt() -> Filed:
        session = session_of(db)
        try:
            return await create_ticket(
                session, TICKET, session_id=session_id, turn_index=7, employee_id="emp-1"
            )
        finally:
            await session.commit()

    first, second = await asyncio.gather(attempt(), attempt())

    assert len(zammad.created) == 1, "a retried turn must not open a second ticket"
    assert len(db.rows) == 1
    winner, loser = sorted([first, second], key=lambda f: f.idempotent_replay)
    assert winner == Filed(NUMBER, False, False)
    assert loser == Filed(NUMBER, False, True), "the loser reads back the filed number"
    assert queue.calls == []


async def test_a_replay_after_a_pending_filing_still_promises_no_number(
    queue: Enqueued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second attempt at a turn Zammad could not take must not file either,
    and must not manufacture a number to fill the gap."""
    fake = FakeZammad(create_status=503)
    monkeypatch.setattr(ticketing, "client", fake.client)
    session_id, db = uuid.uuid4(), FakeDb()

    first = session_of(db)
    await create_ticket(first, TICKET, session_id=session_id, turn_index=0, employee_id="emp-1")
    await first.commit()

    replay = await create_ticket(
        session_of(db), TICKET, session_id=session_id, turn_index=0, employee_id="emp-1"
    )
    assert replay == Filed(None, True, True)
    assert fake.created == []
    assert len(db.rows) == 1


# --- the fallback task is idempotent -----------------------------------------


async def pending_row(db: FakeDb, session_id: uuid.UUID, turn_index: int = 0) -> TicketFiling:
    row = TicketFiling(
        id=uuid.uuid4(),
        session_id=session_id,
        turn_index=turn_index,
        employee_id="emp-1",
        source_key=ticketing.source_key(session_id, turn_index),
        status=PENDING,
        ticket_number=None,
        payload=TICKET.model_dump(),
        attempts=1,
        last_error="TicketingUnavailable",
    )
    db.rows[(session_id, turn_index)] = row
    return row


async def test_the_task_adopts_a_ticket_an_earlier_attempt_already_created(
    zammad: FakeZammad,
) -> None:
    """The failure this exists for: the previous attempt reached Zammad, Zammad
    committed the ticket, and the connection died before the number came back.
    The row still says pending. Posting again would open a second ticket, so the
    task searches on `source_session_id` first and adopts what it finds.
    """
    session_id, db = uuid.uuid4(), FakeDb()
    await pending_row(db, session_id)
    zammad.tickets.append(
        {"id": 55, "number": "70004", SOURCE_FIELD: ticketing.source_key(session_id, 0)}
    )

    result = await ticketing.file_pending_with(session_of(db), session_id, 0)

    assert result == {"status": FILED, "ticket_number": "70004", "adopted": True}
    assert zammad.created == [], "the ticket already existed; filing again would duplicate it"
    row = db.rows[(session_id, 0)]
    assert (row.status, row.ticket_number, row.ticket_id) == (FILED, "70004", 55)
    assert row.attempts == 2


async def test_the_task_ignores_another_turns_ticket(zammad: FakeZammad) -> None:
    """Adoption matches the custom field, not a free-text search hit. Matching
    loosely would attach this turn to a stranger's ticket."""
    session_id, db = uuid.uuid4(), FakeDb()
    await pending_row(db, session_id)
    zammad.tickets.append({"id": 55, "number": "70004", SOURCE_FIELD: "someone-else:0"})

    result = await ticketing.file_pending_with(session_of(db), session_id, 0)

    assert len(zammad.created) == 1
    assert result["ticket_number"] == NUMBER
    assert result["adopted"] is False


async def test_the_task_files_when_zammad_holds_nothing(zammad: FakeZammad) -> None:
    session_id, db = uuid.uuid4(), FakeDb()
    await pending_row(db, session_id)

    result = await ticketing.file_pending_with(session_of(db), session_id, 0)

    assert result == {"status": FILED, "ticket_number": NUMBER, "adopted": False}
    assert len(zammad.created) == 1
    assert zammad.created[0][SOURCE_FIELD] == ticketing.source_key(session_id, 0)


async def test_a_redelivered_task_files_nothing(zammad: FakeZammad) -> None:
    """Celery's at-least-once delivery, run twice end to end."""
    session_id, db = uuid.uuid4(), FakeDb()
    await pending_row(db, session_id)

    first = await ticketing.file_pending_with(session_of(db), session_id, 0)
    second = await ticketing.file_pending_with(session_of(db), session_id, 0)

    assert len(zammad.created) == 1
    assert first["ticket_number"] == second["ticket_number"] == NUMBER
    assert second["replayed"] is True
    assert db.rows[(session_id, 0)].attempts == 2, "a settled row is not re-attempted"


async def test_a_missing_row_is_retried_rather_than_filed(zammad: FakeZammad) -> None:
    """The task can outrun the turn's commit. Filing on a row we cannot see
    would be filing a ticket nobody asked for."""
    with pytest.raises(ticketing.TicketingUnavailable):
        await ticketing.file_pending_with(session_of(FakeDb()), uuid.uuid4(), 0)
    assert zammad.created == []


async def test_the_task_retries_with_exponential_backoff() -> None:
    options = ticketing.file_pending_ticket
    assert ticketing.TicketingUnavailable in ticketing.RETRY_ON
    assert ticketing.TicketingRejected not in ticketing.RETRY_ON
    assert options.retry_backoff is True
    assert options.retry_jitter is True
    assert options.max_retries == 5
    assert options.name == "uc1.file_ticket"


# --- a POST retried inside one attempt ---------------------------------------


class FlakyZammad(FakeZammad):
    """Commits the ticket, then drops the connection before answering."""

    def __init__(self) -> None:
        super().__init__()
        self.posts = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/tickets" and request.method == "POST":
            self.posts += 1
            if self.posts == 1:
                await super().handle(request)  # Zammad commits...
                raise httpx.ReadError("connection reset", request=request)  # ...we never hear.
        return await super().handle(request)


async def test_a_dropped_response_does_not_open_a_second_ticket(
    queue: Enqueued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A POST whose response never arrived may or may not have committed. The
    retry searches before it reposts, which is the only way to tell."""
    fake = FlakyZammad()
    monkeypatch.setattr(ticketing, "client", fake.client)
    session_id, db = uuid.uuid4(), FakeDb()

    filed = await create_ticket(
        session_of(db), TICKET, session_id=session_id, turn_index=0, employee_id="emp-1"
    )

    key = ticketing.source_key(session_id, 0)
    assert len(fake.created) == 1, "the second attempt must adopt, not create"
    assert filed == Filed(NUMBER, False, False)
    # Two searches, not one: the lookup runs before EVERY post, the first
    # included. Searching only on retries left a real hole -- the reservation
    # row lives in the turn's transaction, so a rollback after Zammad committed
    # deletes the row, frees the key, and the retry arrives at attempt 0 with no
    # memory of the ticket it already created. This assertion is what keeps the
    # first-post lookup from being optimised away again.
    assert fake.searches == [key, key]


# --- against a real Postgres --------------------------------------------------


def _skip_without_db() -> None:
    import socket
    from urllib.parse import urlparse

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set")
    parsed = urlparse(url.replace("postgresql+psycopg", "postgresql"))
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), 1):
            pass
    except OSError:
        pytest.skip(f"Postgres at {parsed.hostname}:{parsed.port} is not reachable")


@pytest.mark.integration
async def test_the_constraint_itself_refuses_the_second_filing(zammad: FakeZammad) -> None:
    """Postgres only. The fake above proves the ordering; this proves the rule.

    Two `create_ticket` calls on two real sessions, concurrently, against the
    real `uq_ticket_filings_turn`: one ticket at Zammad, one row, and the loser
    reads the winner's number back.
    """
    _skip_without_db()
    from indic_platform.db.models import Session as SessionRow
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    session_id = uuid.uuid4()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as setup, setup.begin():
            setup.add(
                SessionRow(
                    id=session_id, app="helpdesk_agent", metadata_json={"employee_id": "emp-1"}
                )
            )

        async def attempt() -> Filed:
            async with factory() as db:
                try:
                    return await create_ticket(
                        db, TICKET, session_id=session_id, turn_index=0, employee_id="emp-1"
                    )
                finally:
                    await db.commit()

        first, second = await asyncio.gather(attempt(), attempt())
        assert len(zammad.created) == 1, "a retried turn must not open a second ticket"
        winner, loser = sorted([first, second], key=lambda f: f.idempotent_replay)
        assert winner == Filed(NUMBER, False, False)
        assert loser == Filed(NUMBER, False, True)

        async with factory() as db:
            rows = (
                await db.execute(
                    select(func.count())
                    .select_from(TicketFiling)
                    .where(TicketFiling.session_id == session_id)
                )
            ).scalar_one()
            assert rows == 1
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_a_pending_row_cannot_hold_a_ticket_number() -> None:
    """`ck_ticket_filings_number`: the database refuses an invented number."""
    _skip_without_db()
    from indic_platform.db.models import Session as SessionRow
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    session_id = uuid.uuid4()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as setup, setup.begin():
            setup.add(
                SessionRow(
                    id=session_id, app="helpdesk_agent", metadata_json={"employee_id": "emp-1"}
                )
            )
        async with factory() as db:
            db.add(
                TicketFiling(
                    session_id=session_id,
                    turn_index=0,
                    employee_id="emp-1",
                    source_key=ticketing.source_key(session_id, 0),
                    status=PENDING,
                    ticket_number=NUMBER,
                    payload=TICKET.model_dump(),
                    attempts=1,
                    last_error="",
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()


# --- against a real Zammad ----------------------------------------------------


def _skip_without_zammad() -> None:
    import socket
    from urllib.parse import urlparse

    url = os.environ.get("ZAMMAD_URL")
    if not url or not os.environ.get("ZAMMAD_TOKEN"):
        pytest.skip("ZAMMAD_URL/ZAMMAD_TOKEN are not set; run the `ticketing` compose profile")
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 80), 2):
            pass
    except OSError:
        pytest.skip(f"Zammad at {url} is not reachable")


@pytest.mark.ticketing
async def test_a_real_zammad_accepts_the_payload_and_finds_it_again() -> None:
    """The one thing the fake cannot prove: that Zammad accepts this body, and
    that a search on `source_session_id` finds the ticket back.

    Marked `ticketing`, not `integration`: Zammad runs under its own compose
    profile and CI does not provision it, and CI's integration job fails on a
    skip because a skip there means a provisioned service was unreachable.
    """
    _skip_without_zammad()
    session_id = uuid.uuid4()
    key = ticketing.source_key(session_id, 0)
    async with ticketing.client() as http:
        number, ticket_id = await ticketing.file_once(
            http, TICKET, key=key, employee_id="emp-1@example.com"
        )
        assert number and ticket_id
        # Zammad indexes asynchronously; give the search a moment to catch up.
        for _ in range(10):
            found = await ticketing.find_filed(http, key)
            if found is not None:
                break
            await asyncio.sleep(1)
        assert found == (number, ticket_id)


async def test_a_rolled_back_turn_does_not_file_a_second_ticket(
    queue: Enqueued, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The duplicate a pre-ship review demonstrated: two tickets, no searches.

    The reservation row lives in the *turn's* transaction. If that transaction
    aborts after Zammad has committed -- a voice client disconnecting mid-turn,
    a failure persisting the `Turn` row, a COMMIT that fails -- the row is gone
    while the ticket is not. The retry recomputes the same `turn_index`, because
    the `Turn` insert rolled back too and the count is unchanged, so it takes a
    fresh reservation and reaches `file_once` at `attempt == 0`.

    Nothing in the database can catch this: the key was released on purpose, so
    a corrected configuration can refile. Only Zammad knows, and only if it is
    asked before the first post.
    """
    fake = FakeZammad()
    monkeypatch.setattr(ticketing, "client", fake.client)
    session_id = uuid.uuid4()

    first = await create_ticket(
        session_of(FakeDb()), TICKET, session_id=session_id, turn_index=0, employee_id="emp-1"
    )
    assert first.ticket_number is not None
    assert len(fake.created) == 1

    # The turn rolls back: a brand-new database with no reservation row, exactly
    # what the retry sees. Same session, same turn_index.
    second = await create_ticket(
        session_of(FakeDb()), TICKET, session_id=session_id, turn_index=0, employee_id="emp-1"
    )

    assert len(fake.created) == 1, "the retry filed a second ticket for one request"
    assert second.ticket_number == first.ticket_number
    assert not second.pending


async def test_a_lost_race_does_not_promise_an_email_nobody_owes() -> None:
    """`TicketingRaced`, not `TicketingUnavailable`.

    If the other attempt's transaction rolls back between our constraint
    violation and our read, there is no row and nothing was enqueued. Raising
    `TicketingUnavailable` here would reach the act node's handler and tell the
    employee their ticket number will be emailed -- a promise with no record
    behind it and no worker that will ever settle it.
    """
    assert not issubclass(ticketing.TicketingRaced, ticketing.TicketingUnavailable)
    assert not issubclass(ticketing.TicketingUnavailable, ticketing.TicketingRaced)
    # And the act node must let it through rather than absorbing it.
    assert ticketing.TicketingRaced not in ticketing.RETRY_ON
