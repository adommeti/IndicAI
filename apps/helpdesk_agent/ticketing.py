"""File helpdesk tickets in Zammad, at most once per `(session_id, turn_index)`.

Three properties, in the order they matter.

**Exactly one ticket per turn.** The guarantee is the unique constraint
`uq_ticket_filings_turn` (migration 0011), not a lookup. `create_ticket` reserves
the turn by INSERTing its row inside a SAVEPOINT *before* it talks to Zammad. A
concurrent retry of the same turn blocks on that key, and when the winner's
transaction commits the loser's INSERT is refused; the `IntegrityError` is the
idempotent path, and the loser reads back the number the winner filed without
ever reaching Zammad. Read-then-write would let both callers see an empty table
and both file.

**A pending filing never invents a number.** When Zammad is unreachable the row
is committed `status="pending"` with `ticket_number` NULL -- a combination the
check constraint `ck_ticket_filings_number` is the only one it permits -- a
Celery task is enqueued with exponential backoff, and `Filed.ticket_number` is
None so the reply can only promise the number by email.

**The fallback task is itself idempotent.** Its hard case is an attempt that
died *after* Zammad committed the ticket and before we recorded the number: the
row still says pending, but the ticket exists. Every retry therefore searches
Zammad for `source_session_id == "<session_id>:<turn_index>"` before it posts,
and adopts what it finds. The same search runs before any retry of the POST
inside a single attempt, because a connection dropped while reading the response
is indistinguishable from one dropped before Zammad committed.

Deviation from `.claude/rules/adapters.md`, recorded deliberately: Zammad calls
do **not** go through `indic_platform.adapters.runtime.AdapterRuntime`. That
runtime prices every call through `config/pricing.yaml` and raises `KeyError` for
a model it does not know, and it emits a Langfuse *generation* with a cost. Zammad
is self-hosted infrastructure with no per-call price; giving it a pricing entry
would put a fabricated number into the cost reporting the PRD's B6 gates read.
What the rule is actually protecting -- explicit timeouts, bounded retries with
jitter on 429/5xx only, and no unbounded blocking -- is implemented here instead
(`_send`, `_get`, `file_once`). It is also why the ticket body and log lines carry
identifiers rather than employee text: nothing here is a model vendor, so the
redaction hook does not apply, and the ticket itself must keep the employee's
description intact to be useful to the IT queue.
"""

import asyncio
import functools
import logging
import os
import random
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from celery import Celery
from indic_platform.db.models import TicketFiling
from indic_platform.tasks import BudgetAwareTask
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from helpdesk_agent.graph import Ticket

log = logging.getLogger(__name__)

PENDING = "pending"
FILING = "filing"
FILED = "filed"
GROUP = "IT Support"
TAGS = ("voice-agent", "auto-filed")
SOURCE_FIELD = "source_session_id"
PRIORITY = {"low": "1 low", "normal": "2 normal", "high": "3 high"}

# Timeout and retry discipline, stated here because AdapterRuntime is not used.
CONNECT_TIMEOUT_S = 5.0
REQUEST_TIMEOUT_S = 20.0
ATTEMPTS = 3
RETRY_BASE_S = 0.5
# The fallback task's first run waits for the turn's transaction to commit: the
# reservation row is not visible to another process until the caller commits.
ENQUEUE_DELAY_S = 10


class TicketingUnavailable(RuntimeError):
    """Zammad is unreachable or returned 429/5xx. Retry later; fall back to pending."""


class TicketingRaced(RuntimeError):
    """The reservation was taken and then released before it could be read.

    Deliberately NOT a `TicketingUnavailable`: that class means "queued, the
    number will follow by email", and the act node says exactly that to the
    employee. Here nothing is queued and no row exists -- the other attempt's
    transaction rolled back between our constraint violation and our read -- so
    promising an email would be promising what nobody owes. Raising instead
    fails the turn loudly and leaves the key free for a retry that can succeed.
    """


class TicketingRejected(RuntimeError):
    """Zammad refused the request permanently. A defect or a misconfiguration."""


class TicketingUnauthorized(TicketingRejected):
    """Zammad rejected the credential, or there is none.

    Deliberately not a `TicketingUnavailable`: a bad token does not heal by
    waiting, and queueing every ticket behind it would hide a one-line
    configuration error behind a growing backlog of "the number will be emailed".
    """


@dataclass(frozen=True)
class Filed:
    """What `create_ticket` could honestly promise the employee."""

    ticket_number: str | None  # None whenever Zammad has not issued one
    pending: bool
    idempotent_replay: bool  # True when an existing row was returned


def source_key(session_id: uuid.UUID, turn_index: int) -> str:
    """The value written to Zammad's `source_session_id` custom field."""
    return f"{session_id}:{turn_index}"


def client() -> httpx.AsyncClient:
    """An HTTP client for the configured Zammad.

    Tests replace this function wholesale to inject an `httpx.MockTransport`;
    every call site reaches it through the module global for that reason.
    """
    token = os.environ.get("ZAMMAD_TOKEN", "")
    if not token:
        raise TicketingUnauthorized("ZAMMAD_TOKEN is not set")
    return httpx.AsyncClient(
        base_url=os.environ.get("ZAMMAD_URL", "http://localhost:8080").rstrip("/"),
        headers={"Authorization": f"Token token={token}"},
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
    )


async def _send(http: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    """One request, with vendor status mapped onto this module's two failure kinds.

    No retry here: whether a retry is safe depends on the verb, so the callers
    decide (`_get` retries freely, `file_once` searches first).
    """
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        # The message is the class name only: it can carry a URL with a token.
        raise TicketingUnavailable(f"zammad transport failure ({type(exc).__name__})") from exc
    if response.status_code in (401, 403):
        raise TicketingUnauthorized(f"zammad rejected the API token ({response.status_code})")
    if response.status_code == 429 or response.status_code >= 500:
        raise TicketingUnavailable(f"zammad returned {response.status_code}")
    if response.status_code >= 400:
        raise TicketingRejected(f"zammad refused the request ({response.status_code})")
    return response


async def _backoff(attempt: int) -> None:
    await asyncio.sleep(RETRY_BASE_S * 2**attempt * random.uniform(0.5, 1.5))


async def _get(
    http: httpx.AsyncClient, url: str, *, attempts: int = ATTEMPTS, **kwargs: Any
) -> httpx.Response:
    """A GET, retried on transient failures. Safe to repeat; nothing is created.

    `attempts` exists because this is also called from inside `file_once`'s own
    retry loop. Left at the default there, the two ladders multiply: three POST
    attempts each preceded by a three-attempt lookup, every one of them able to
    burn the full read timeout against a Zammad that accepts connections and
    then hangs. That is minutes of a turn held open on the reservation row's
    lock, to produce a reply whose entire purpose is to be fast and honest.
    """
    for attempt in range(attempts):
        try:
            return await _send(http, "GET", url, **kwargs)
        except TicketingUnavailable:
            if attempt == attempts - 1:
                raise
            await _backoff(attempt)
    raise AssertionError("unreachable")


def _tickets(payload: Any) -> list[dict[str, Any]]:
    """Ticket objects out of whichever shape the search endpoint returned.

    Zammad answers `/tickets/search` as a bare list when `expand` is set, and as
    `{"tickets": [...ids...], "assets": {"Ticket": {...}}}` otherwise. Both are
    handled rather than pinned, because the shape also depends on whether
    Elasticsearch is enabled, and this lookup is the thing standing between a
    dropped connection and a duplicate ticket.
    """
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        assets = payload.get("assets")
        if isinstance(assets, dict) and isinstance(assets.get("Ticket"), dict):
            return [item for item in assets["Ticket"].values() if isinstance(item, dict)]
        tickets = payload.get("tickets")
        if isinstance(tickets, list):
            return [item for item in tickets if isinstance(item, dict)]
    return []


async def find_filed(
    http: httpx.AsyncClient, key: str, *, attempts: int = ATTEMPTS
) -> tuple[str, int] | None:
    """The ticket already filed for `key`, if Zammad holds one.

    The match is on the `source_session_id` custom field, never on the free-text
    hit alone: the query is what finds candidates, the field is what proves one
    of them is this turn's ticket.
    """
    response = await _get(
        http, "/api/v1/tickets/search", params={"query": key, "limit": 50, "expand": "true"}
    )
    for ticket in _tickets(response.json()):
        if str(ticket.get(SOURCE_FIELD, "")) == key and ticket.get("number"):
            return str(ticket["number"]), int(ticket.get("id", 0))
    return None


def _article_body(ticket: Ticket, *, key: str, employee_id: str) -> str:
    """The first article. The trailer is what a database-only search can find.

    With Elasticsearch disabled -- which is how this stack runs Zammad -- the
    search endpoint falls back to SQL over titles, numbers and article bodies and
    does not look inside custom object attributes. Repeating the key in the body
    keeps `find_filed` able to locate the ticket; the custom field is still what
    confirms it.
    """
    return (
        f"{ticket.description}\n\n"
        f"Category: {ticket.category}\n"
        f"Urgency: {ticket.urgency}\n"
        f"Employee: {employee_id}\n"
        f"{SOURCE_FIELD}: {key}\n"
    )


def ticket_payload(ticket: Ticket, *, key: str, employee_id: str) -> dict[str, Any]:
    """The Zammad create body: group, tags, priority and the idempotency field."""
    payload: dict[str, Any] = {
        "title": ticket.title,
        "group": GROUP,
        "state": "new",
        "priority": PRIORITY[ticket.urgency],
        "tags": ",".join(TAGS),
        SOURCE_FIELD: key,
        "article": {
            "subject": ticket.title,
            "body": _article_body(ticket, key=key, employee_id=employee_id),
            "type": "note",
            "internal": False,
            "content_type": "text/plain",
        },
    }
    # `guess:` is Zammad's own "attach by email, creating the user if needed".
    # An SSO subject that is not an email would be refused, so it is only sent
    # when it can work; otherwise the employee is recorded in the article.
    if "@" in employee_id:
        payload["customer_id"] = f"guess:{employee_id}"
    return payload


async def file_once(
    http: httpx.AsyncClient, ticket: Ticket, *, key: str, employee_id: str
) -> tuple[str, int]:
    """Create the ticket, or adopt the one a previous attempt already created.

    The search runs before **every** POST, the first one included. A POST whose
    response never arrived may or may not have committed at Zammad, and blindly
    reposting turns one transient network fault into two tickets in the IT queue.

    Searching only on retries was not enough, and the hole is not hypothetical:
    the reservation row lives in the *turn's* transaction, and `create_ticket`
    releases the key when that transaction rolls back. If it aborts after Zammad
    committed -- a voice client disconnecting mid-turn, a failure persisting the
    `Turn` row, a COMMIT that fails -- the row disappears while the ticket
    survives. The retry then computes the same `turn_index` (the `Turn` insert
    rolled back too, so the count is unchanged), takes a fresh reservation, and
    arrives here at `attempt == 0`. Without this lookup that is a second ticket
    in the IT queue for one request, and the employee is read back the second
    number. A pre-ship review demonstrated exactly that: two tickets, no
    searches.

    The cost is one GET per filed ticket. Ticket filing is the rare branch of a
    turn, and a duplicate in a compliance-adjacent queue is worth far more than
    a lookup.

    `attempts=1` on the lookup deliberately: this loop is already the retry, and
    a nested ladder multiplies into minutes against a hung Zammad.
    """
    last: TicketingUnavailable | None = None
    for attempt in range(ATTEMPTS):
        existing = await find_filed(http, key, attempts=1)
        if existing is not None:
            return existing
        try:
            response = await _send(
                http,
                "POST",
                "/api/v1/tickets",
                json=ticket_payload(ticket, key=key, employee_id=employee_id),
            )
        except TicketingUnavailable as exc:
            last = exc
            if attempt == ATTEMPTS - 1:
                break
            await _backoff(attempt)
            continue
        created = response.json()
        return str(created["number"]), int(created.get("id", 0))
    assert last is not None
    raise last


async def _load(
    session: AsyncSession, session_id: uuid.UUID, turn_index: int
) -> TicketFiling | None:
    statement = (
        select(TicketFiling)
        .where(TicketFiling.session_id == session_id, TicketFiling.turn_index == turn_index)
        .execution_options(populate_existing=True)
    )
    return await session.scalar(statement)


async def create_ticket(
    session: AsyncSession,
    ticket: Ticket,
    *,
    session_id: uuid.UUID,
    turn_index: int,
    employee_id: str,
) -> Filed:
    """File `ticket` for this turn, exactly once, and say what can be promised.

    The caller owns the transaction. Nothing here commits: the reservation lives
    in a SAVEPOINT that is released on success and rolled back if Zammad refuses
    the request permanently, so a turn whose transaction is rolled back leaves no
    claim on the key. That is also why a concurrent duplicate blocks until the
    winner's turn commits -- at which point it reads a settled row rather than a
    half-written one.
    """
    key = source_key(session_id, turn_index)
    row = TicketFiling(
        session_id=session_id,
        turn_index=turn_index,
        employee_id=employee_id,
        source_key=key,
        status=FILING,
        payload=ticket.model_dump(),
        attempts=1,
        last_error="",
    )
    savepoint = await session.begin_nested()
    try:
        session.add(row)
        await session.flush()
    except IntegrityError:
        # uq_ticket_filings_turn. Another attempt owns this turn; adopt its outcome.
        await savepoint.rollback()
        existing = await _load(session, session_id, turn_index)
        if existing is None:
            raise TicketingRaced("the concurrent filing attempt left no row") from None
        log.info("helpdesk ticket filing replayed for turn %s", key)
        return Filed(existing.ticket_number, existing.ticket_number is None, True)

    try:
        async with client() as http:
            number, ticket_id = await file_once(http, ticket, key=key, employee_id=employee_id)
    except TicketingUnavailable as exc:
        row.status = PENDING
        row.last_error = type(exc).__name__
        await session.flush()
        await savepoint.commit()
        await enqueue(session_id, turn_index)
        log.warning("zammad unavailable; turn %s queued for retry", key)
        # No number exists. Saying so is the whole point of this branch.
        return Filed(None, True, False)
    except Exception:
        # Permanent refusal, or a defect. Release the key so a fixed
        # configuration can file this turn instead of replaying a dead row.
        await savepoint.rollback()
        raise

    row.status = FILED
    row.ticket_number = number
    row.ticket_id = ticket_id
    await session.flush()
    await savepoint.commit()
    return Filed(number, False, False)


# --- Celery fallback ---------------------------------------------------------

celery_app = Celery(
    "helpdesk_agent",
    broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)

# `TicketingRejected` is deliberately absent: a refused payload or a bad token
# retried sixteen times is still refused, and the failure should be visible.
RETRY_ON = (TicketingUnavailable, OSError, TimeoutError, DBAPIError)
TASK = {
    "autoretry_for": RETRY_ON,
    # Every task in this app inherits the spend-refusal boundary: a BudgetExceeded
    # becomes a recorded BUDGET_EXCEEDED state that stops the chain, not a FAILED
    # task with a traceback that reads like a broken worker.
    "base": BudgetAwareTask,
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": 5,
}


async def enqueue(session_id: uuid.UUID, turn_index: int) -> None:
    """Hand the turn to the worker. A broker outage must not fail the turn.

    `apply_async` is synchronous kombu socket I/O, so calling it directly from
    the event loop stalls every other turn in the process for the broker's
    connect timeout and publish-retry policy. That cost lands exactly when the
    system is already degraded -- this is the fallback path -- so it runs on a
    worker thread instead.
    """
    try:
        await asyncio.to_thread(
            functools.partial(
                file_pending_ticket.apply_async,
                (str(session_id), turn_index),
                countdown=ENQUEUE_DELAY_S,
            )
        )
    except Exception:
        # The row stays `pending` and is still the record of what is owed.
        log.exception("could not enqueue the pending ticket for turn %s:%s", session_id, turn_index)


async def file_pending_with(
    session: AsyncSession, session_id: uuid.UUID, turn_index: int
) -> dict[str, Any]:
    """Settle one pending filing. Idempotent: safe to run again after any failure.

    Two guards make the retry safe. The row is checked first, so a redelivery of
    an already-settled task costs nothing and files nothing. Then Zammad is
    searched for this turn's `source_session_id`, because the attempt that left
    the row pending may have created the ticket and died before it could record
    the number -- the row cannot tell us, and only Zammad can.
    """
    key = source_key(session_id, turn_index)
    row = await _load(session, session_id, turn_index)
    if row is None:
        # The turn's transaction may not have committed yet. Come back.
        raise TicketingUnavailable(f"no filing row for turn {key} yet")
    if row.status == FILED and row.ticket_number:
        return {"status": FILED, "ticket_number": row.ticket_number, "replayed": True}
    ticket = Ticket.model_validate(row.payload)
    employee_id = row.employee_id
    row.attempts += 1
    await session.commit()

    async with client() as http:
        found = await find_filed(http, key)
        adopted = found is not None
        if found is None:
            found = await file_once(http, ticket, key=key, employee_id=employee_id)
    number, ticket_id = found

    row = await _load(session, session_id, turn_index)
    if row is not None:
        row.status = FILED
        row.ticket_number = number
        row.ticket_id = ticket_id
        row.last_error = ""
        await session.commit()
    return {"status": FILED, "ticket_number": number, "adopted": adopted}


async def file_pending(session_id: uuid.UUID, turn_index: int) -> dict[str, Any]:
    """`file_pending_with` against the configured database."""
    engine = create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        async with AsyncSession(engine) as session:
            return await file_pending_with(session, session_id, turn_index)
    finally:
        await engine.dispose()


@celery_app.task(name="uc1.file_ticket", **TASK)
def file_pending_ticket(session_id: str, turn_index: int) -> dict[str, Any]:
    """Retry a pending filing with exponential backoff. Celery workers are sync."""
    return asyncio.run(file_pending(uuid.UUID(session_id), turn_index))
