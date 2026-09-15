"""uc1/P6's replay endpoint: who may read a session, and what they get.

The prompt's acceptance criterion is "replay endpoint returns 403 for a
non-governance role and full JSON for governance", and its execution notes say
the 403/200 behaviour is proven by API tests rather than by the UI. So this file
is the evidence for that criterion, and it is written to fail loudly if the
check is deleted rather than to pass because a fixture happened to be empty:

- the refusal tests assert that the database session was **never opened**, not
  merely that the status was 403. Deleting the dependency turns them into
  failures even if something downstream happened to error;
- the 403/404 matrix is asserted in both orders -- a missing session with no
  role, and a real session with no role -- because a 404/403 split is an oracle
  telling an unauthorised caller which session ids exist, and helpdesk session
  ids are written into Zammad tickets as `source_session_id`;
- the role tests pin that uc1's `governance` is not uc3's. Same word, opposite
  effect (`helpdesk_agent/roles.py`, ADR 0013), so a caller carrying uc3's
  content roles is refused here, and a caller carrying `governance` *and* a uc3
  content role is admitted here although uc3 would deny it.

The pure half -- the role predicate, the Langfuse URL, the payload shape from
fabricated rows -- needs no database and is a plain unit test, so it runs in
`make check`. The database half is marked `integration`: it proves the SQL
ordering, the join to `ticket_filings` and the real 200/404, which are
properties of Postgres rather than of Python.

**The database is shared.** Every fixture row this file writes is stamped
`trace_id == FIXTURE` on its turns and `employee_id == FIXTURE_EMPLOYEE` on its
sessions and ticket filings, every assertion is scoped to rows this file
created, and `_clear` runs before each case as well as after so an earlier
crashed run cannot poison this one. There is no assertion anywhere over an
absolute row count.
"""

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from helpdesk_agent import api, roles
from indic_platform.db.models import Session as SessionRow
from indic_platform.db.models import TicketFiling, Turn
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    BaseUser,
    SimpleUser,
)
from starlette.middleware.authentication import AuthenticationMiddleware

FIXTURE = "uc1-replay-test"
FIXTURE_EMPLOYEE = "uc1-replay-test-employee"

# A phone number, in a fixture utterance, on purpose. `redact` masks this
# spelling on its way to a vendor or a log; the replay returns it verbatim to an
# authorised reviewer, and `test_the_replay_returns_the_stored_text_unredacted`
# is what makes that a stated decision instead of an oversight.
PHONE_IN_AN_UTTERANCE = "call me back on 9876543210"

# 2021, far outside the range any other suite writes into.
BASE_AT = datetime(2021, 6, 15, 3, 30, tzinfo=UTC)

MISSING_SESSION = uuid.UUID("99999999-9999-4999-8999-999999999999")

# uc3's content roles, named here so the cross-application test does not have to
# import `comms_surveillance.auth` -- importing it would be the very coupling
# `roles.py` promises does not exist.
UC3_REVIEWER = "compliance_reviewer"
UC3_LEAD = "compliance_lead"


# --- a verified subject, without an identity provider --------------------------


class _Verified(SimpleUser):
    @property
    def identity(self) -> str:
        return self.username


def _backend(identity: str, scopes: tuple[str, ...]) -> AuthenticationBackend:
    class Backend(AuthenticationBackend):
        async def authenticate(self, conn: Any) -> tuple[AuthCredentials, BaseUser]:
            # Stand-in for server-side SSO validation. Never a request header:
            # the point of the contract is that a caller cannot name its own
            # roles.
            return AuthCredentials([roles.AUTHENTICATED, *scopes]), _Verified(identity)

    return Backend()


def _client(*scopes: str, identity: str = FIXTURE_EMPLOYEE) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=AuthenticationMiddleware(api.app, backend=_backend(identity, scopes))
        ),
        base_url="http://test",
    )


def _anonymous() -> httpx.AsyncClient:
    """No authentication middleware at all -- a bare deployment."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test")


class _Unreachable:
    """A database dependency that fails the test if the route ever reaches it."""

    def __init__(self) -> None:
        self.opened = 0

    async def __call__(self) -> AsyncIterator[Any]:
        self.opened += 1
        raise AssertionError("the route opened a database session for a refused caller")
        yield  # pragma: no cover - unreachable, present so this is a generator


@asynccontextmanager
async def _refuses_database() -> AsyncIterator[_Unreachable]:
    sentinel = _Unreachable()
    api.app.dependency_overrides[api.db_session] = sentinel
    try:
        yield sentinel
    finally:
        api.app.dependency_overrides.pop(api.db_session, None)


# --- the role, and the fact that it is not uc3's role --------------------------


def test_uc1_recognises_exactly_one_role_and_it_grants_replay() -> None:
    assert roles.KNOWN_ROLES == frozenset({"governance"})
    assert roles.REPLAY_ROLES == frozenset({"governance"})
    assert roles.grants_replay({"governance"}) is True
    assert roles.grants_replay(set()) is False


def test_uc1_replay_does_not_consult_uc3s_role_sets() -> None:
    """The pin the contract asks for: same word, opposite effect, no coupling.

    Three separate claims, each of which would break if someone reused uc3's
    module here:

    1. uc3's content roles grant nothing in uc1 -- a compliance reviewer or
       lead is refused replay.
    2. uc3's *deny* does not apply in uc1 -- a caller holding `governance`
       alongside a uc3 content role replays, where uc3's `SEGREGATED_ROLES`
       check would have refused it.
    3. uc1's role module does not import uc3's at all, so no future edit to
       `comms_surveillance.auth` can change who reads a helpdesk transcript.
    """
    assert roles.grants_replay({UC3_REVIEWER}) is False
    assert roles.grants_replay({UC3_LEAD}) is False
    assert roles.grants_replay({UC3_REVIEWER, UC3_LEAD}) is False
    assert roles.grants_replay({"governance", UC3_LEAD}) is True

    import inspect

    imports = [
        line
        for line in inspect.getsource(roles).splitlines()
        if line.startswith(("import ", "from "))
    ]
    assert imports, "no import lines found; the check below would pass vacuously"
    assert not any("comms_surveillance" in line for line in imports), (
        "helpdesk_agent.roles must not import comms_surveillance"
    )
    assert "MUST map these to two different Entra groups" in (roles.__doc__ or ""), (
        "the deployment warning is the reason this module exists"
    )


def test_unknown_scopes_are_dropped_rather_than_carried() -> None:
    """`/me` must not be a reflection surface for arbitrary claim scopes."""
    assert roles.recognised_roles(["governance", "wheel", "admin", "sudo"]) == frozenset(
        {"governance"}
    )
    assert roles.recognised_roles([]) == frozenset()


# --- /me -----------------------------------------------------------------------


async def test_me_reports_the_verified_subject_and_its_roles() -> None:
    async with _client("governance", identity="E1") as client:
        response = await client.get("/me")
    assert response.status_code == 200
    assert response.json() == {"employee_id": "E1", "roles": ["governance"]}


async def test_me_answers_a_caller_holding_no_roles() -> None:
    """A caller with no grant has to be told so; refusing /me blanks the UI."""
    async with _client(identity="E2") as client:
        response = await client.get("/me")
    assert response.status_code == 200
    assert response.json() == {"employee_id": "E2", "roles": []}


async def test_me_does_not_echo_scopes_uc1_has_no_rule_for() -> None:
    async with _client("wheel", "compliance_lead", identity="E3") as client:
        response = await client.get("/me")
    assert response.status_code == 200
    assert response.json() == {"employee_id": "E3", "roles": []}


async def test_me_refuses_an_unauthenticated_caller() -> None:
    async with _anonymous() as client:
        response = await client.get("/me", headers={"X-Employee-ID": "E1"})
    assert response.status_code == 401


# --- the 403/404 matrix, without a database ------------------------------------


@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param((), id="no-roles"),
        pytest.param((UC3_REVIEWER,), id="uc3-compliance-reviewer"),
        pytest.param((UC3_LEAD,), id="uc3-compliance-lead"),
        pytest.param((UC3_REVIEWER, UC3_LEAD), id="uc3-reviewer-and-lead"),
        pytest.param(("wheel", "admin"), id="unknown-scopes"),
    ],
)
@pytest.mark.parametrize(
    "session_id",
    [
        pytest.param(str(uuid.uuid4()), id="session-that-does-not-exist"),
        pytest.param("not-a-uuid", id="session-id-that-is-not-even-a-uuid"),
    ],
)
async def test_a_caller_without_the_role_gets_403_whatever_the_session_id(
    scopes: tuple[str, ...], session_id: str
) -> None:
    """403 for every id, real, invented or malformed -- and no lookup happens.

    The malformed id is the sharper case: FastAPI answers 422 for an unparseable
    path parameter, so a 422 here would confirm to a refused caller that they
    had at least reached parameter validation, and a 404 would confirm which
    ids are real. Both are 403 because the dependency is solved before the path
    parameter is parsed.
    """
    async with _refuses_database() as database, _client(*scopes) as client:
        response = await client.get(f"/sessions/{session_id}/replay")
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == roles.REPLAY_REFUSED
    assert database.opened == 0


async def test_the_refusal_names_no_session_and_no_role() -> None:
    """The 403 body must not become the oracle the status code is not."""
    missing = str(MISSING_SESSION)
    async with _refuses_database(), _client() as client:
        response = await client.get(f"/sessions/{missing}/replay")
    body = response.text
    assert missing not in body
    assert "compliance" not in body


async def test_an_unauthenticated_caller_gets_401_not_403() -> None:
    """A bare deployment fails closed before the role question is asked."""
    async with _refuses_database() as database, _anonymous() as client:
        response = await client.get(
            f"/sessions/{MISSING_SESSION}/replay",
            headers={"X-Employee-ID": "E1", "Authorization": "Bearer unverified"},
        )
    assert response.status_code == 401
    assert database.opened == 0


# --- the payload shape, from fabricated rows -----------------------------------


def _turn(**overrides: Any) -> Turn:
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "utterance": "my vpn is down",
        "language": "en-IN",
        "decision_json": {"action": "answer", "_guard_errors": []},
        "retrieval_json": {"chunks": []},
        "latency_ms": {"retrieve": 11.0, "decide": 900.0, "voice.stt_ms": 310.0},
        "policy_version": "policy-1",
        "prompt_version": "prompt-1",
        "model": "claude-sonnet-5",
        "trace_id": "trace-1",
        "created_at": BASE_AT,
    }
    fields.update(overrides)
    return Turn(**fields)


def _session(session_id: uuid.UUID | None = None) -> SessionRow:
    return SessionRow(
        id=session_id or uuid.uuid4(),
        app="helpdesk_agent",
        metadata_json={"employee_id": FIXTURE_EMPLOYEE},
        created_at=BASE_AT,
    )


def test_the_payload_carries_every_field_the_contract_names() -> None:
    session = _session()
    filing = TicketFiling(
        session_id=session.id,
        turn_index=0,
        employee_id=FIXTURE_EMPLOYEE,
        source_key="k",
        status="filed",
        ticket_number="12345",
    )
    payload = api.replay_payload(session, [_turn(session_id=session.id)], {0: filing})

    assert payload["session_id"] == str(session.id)
    assert payload["employee_id"] == FIXTURE_EMPLOYEE
    assert payload["created_at"] == BASE_AT.isoformat()
    turns = payload["turns"]
    assert isinstance(turns, list)
    assert set(turns[0]) == {
        "turn_index",
        "utterance",
        "language",
        "decision_json",
        "retrieval_json",
        "latency_ms",
        "model",
        "prompt_version",
        "policy_version",
        "trace_id",
        "langfuse_url",
        "audio_url",
        "ticket",
    }
    assert turns[0]["ticket"] == {"status": "filed", "ticket_number": "12345"}


def test_audio_url_is_null_because_uc1_keeps_no_audio_at_rest() -> None:
    session = _session()
    payload = api.replay_payload(session, [_turn(session_id=session.id)], {})
    turns = payload["turns"]
    assert isinstance(turns, list)
    assert turns[0]["audio_url"] is None
    assert "no audio at rest" in (api.replay_payload.__doc__ or "")


def test_turn_index_is_positional_and_the_ticket_follows_the_position() -> None:
    """A ticket filed for turn 1 must not surface on turn 0, or vice versa."""
    session = _session()
    turns = [
        _turn(session_id=session.id, utterance="first", created_at=BASE_AT),
        _turn(session_id=session.id, utterance="second", created_at=BASE_AT + timedelta(minutes=1)),
        _turn(session_id=session.id, utterance="third", created_at=BASE_AT + timedelta(minutes=2)),
    ]
    filing = TicketFiling(
        session_id=session.id,
        turn_index=1,
        employee_id=FIXTURE_EMPLOYEE,
        source_key="k",
        status="filed",
        ticket_number="42",
    )
    payload = api.replay_payload(session, turns, {1: filing})
    rows = payload["turns"]
    assert isinstance(rows, list)
    assert [row["turn_index"] for row in rows] == [0, 1, 2]
    assert [row["utterance"] for row in rows] == ["first", "second", "third"]
    assert [row["ticket"] for row in rows] == [
        None,
        {"status": "filed", "ticket_number": "42"},
        None,
    ]


def test_a_pending_filing_reports_no_number_rather_than_inventing_one() -> None:
    session = _session()
    filing = TicketFiling(
        session_id=session.id,
        turn_index=0,
        employee_id=FIXTURE_EMPLOYEE,
        source_key="k",
        status="pending",
        ticket_number=None,
    )
    payload = api.replay_payload(session, [_turn(session_id=session.id)], {0: filing})
    rows = payload["turns"]
    assert isinstance(rows, list)
    assert rows[0]["ticket"] == {"status": "pending", "ticket_number": None}


def test_the_filing_row_does_not_ride_along_whole() -> None:
    """Named fields only: `payload`, `source_key` and `last_error` stay out."""
    session = _session()
    filing = TicketFiling(
        session_id=session.id,
        turn_index=0,
        employee_id=FIXTURE_EMPLOYEE,
        source_key="a-source-key-nobody-asked-for",
        status="filed",
        ticket_number="7",
        payload={"title": "a ticket body"},
        last_error="TicketingUnavailable",
    )
    payload = api.replay_payload(session, [_turn(session_id=session.id)], {0: filing})
    rows = payload["turns"]
    assert isinstance(rows, list)
    assert set(rows[0]["ticket"] or {}) == {"status", "ticket_number"}


# --- the Langfuse link ---------------------------------------------------------


def test_the_trace_link_is_null_when_the_project_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never a broken link: a host alone cannot address a Langfuse trace."""
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3002")
    monkeypatch.delenv("LANGFUSE_PROJECT_ID", raising=False)
    assert api.langfuse_trace_url("abc123") is None


def test_the_trace_link_is_null_when_the_host_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "indic-platform")
    assert api.langfuse_trace_url("abc123") is None


def test_the_trace_link_is_null_when_the_turn_has_no_trace_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3002")
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "indic-platform")
    assert api.langfuse_trace_url("") is None
    assert api.langfuse_trace_url("   ") is None


def test_the_trace_link_is_langfuses_own_route_when_both_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`{host}/project/{project}/traces/{trace}` -- the SDK's own shape."""
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3002/")
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "indic-platform")
    assert (
        api.langfuse_trace_url("abc123")
        == "http://localhost:3002/project/indic-platform/traces/abc123"
    )


def test_the_payload_carries_the_link_when_it_can_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3002")
    monkeypatch.setenv("LANGFUSE_PROJECT_ID", "indic-platform")
    session = _session()
    payload = api.replay_payload(session, [_turn(session_id=session.id, trace_id="t9")], {})
    rows = payload["turns"]
    assert isinstance(rows, list)
    assert rows[0]["langfuse_url"] == "http://localhost:3002/project/indic-platform/traces/t9"


# --- the database half ---------------------------------------------------------


def _database_url() -> str:
    """The stack's database, or a skip.

    CI's integration job provisions Postgres and fails on any skip, so with
    DATABASE_URL set an unreachable server is a failure, never a skip.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")
    return url


async def _clear(factory: async_sessionmaker[AsyncSession], made: Sequence[uuid.UUID]) -> None:
    """Remove this file's rows -- its own, and any a killed earlier run left.

    Scoped to `trace_id == FIXTURE` and `employee_id == FIXTURE_EMPLOYEE`, so it
    can never touch another suite's rows. The session ids are recovered from
    those rows rather than assumed, which is what makes it able to clean up
    after a run that died before it recorded what it had made.
    """
    async with factory() as db, db.begin():
        found = set(
            (await db.scalars(select(Turn.session_id).where(Turn.trace_id == FIXTURE))).all()
        )
        found |= set(
            (
                await db.scalars(
                    select(TicketFiling.session_id).where(
                        TicketFiling.employee_id == FIXTURE_EMPLOYEE
                    )
                )
            ).all()
        )
        found |= set(made)
        await db.execute(delete(Turn).where(Turn.trace_id == FIXTURE))
        await db.execute(delete(TicketFiling).where(TicketFiling.employee_id == FIXTURE_EMPLOYEE))
        if found:
            await db.execute(delete(SessionRow).where(SessionRow.id.in_(list(found))))


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


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    made: list[uuid.UUID],
    utterances: Sequence[str],
    *,
    app: str = "helpdesk_agent",
    ticket_on: int | None = None,
) -> uuid.UUID:
    """One session of the given app, one turn per utterance, a minute apart.

    Timestamps are written explicitly and in ascending order so the ordering
    assertion is about `order_by(Turn.created_at, Turn.id)` rather than about
    insertion luck.
    """
    session_id = uuid.uuid4()
    made.append(session_id)
    async with factory() as db, db.begin():
        db.add(
            SessionRow(
                id=session_id,
                app=app,
                metadata_json={"employee_id": FIXTURE_EMPLOYEE},
                created_at=BASE_AT,
            )
        )
        await db.flush()
        for index, utterance in enumerate(utterances):
            db.add(
                Turn(
                    session_id=session_id,
                    utterance=utterance,
                    language="en-IN",
                    decision_json={"action": "answer", "_guard_errors": [], "_grounding": {}},
                    retrieval_json={"chunks": [{"article_id": "VPN-001"}]},
                    latency_ms={"retrieve": 11.0, "decide": 900.0},
                    policy_version="policy-1",
                    prompt_version="prompt-1",
                    model="claude-sonnet-5",
                    trace_id=FIXTURE,
                    created_at=BASE_AT + timedelta(minutes=index),
                )
            )
        if ticket_on is not None:
            db.add(
                TicketFiling(
                    session_id=session_id,
                    turn_index=ticket_on,
                    employee_id=FIXTURE_EMPLOYEE,
                    source_key=f"{session_id}:{ticket_on}",
                    status="filed",
                    ticket_number="90210",
                )
            )
    return session_id


@pytest.mark.integration
async def test_governance_gets_the_full_chain_in_session_order() -> None:
    """The acceptance criterion's 200 half, against a real PostgreSQL."""
    async with _db() as (factory, made):
        session_id = await _seed(
            factory, made, ["first problem", "second problem", "third problem"], ticket_on=1
        )

        async with _client("governance") as client:
            response = await client.get(f"/sessions/{session_id}/replay")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["session_id"] == str(session_id)
        assert body["employee_id"] == FIXTURE_EMPLOYEE
        assert body["created_at"].startswith("2021-06-15T03:30")
        assert [turn["turn_index"] for turn in body["turns"]] == [0, 1, 2]
        assert [turn["utterance"] for turn in body["turns"]] == [
            "first problem",
            "second problem",
            "third problem",
        ]
        assert [turn["ticket"] for turn in body["turns"]] == [
            None,
            {"status": "filed", "ticket_number": "90210"},
            None,
        ]
        first = body["turns"][0]
        assert first["model"] == "claude-sonnet-5"
        assert first["prompt_version"] == "prompt-1"
        assert first["policy_version"] == "policy-1"
        assert first["trace_id"] == FIXTURE
        assert first["retrieval_json"] == {"chunks": [{"article_id": "VPN-001"}]}
        assert first["decision_json"]["action"] == "answer"
        assert first["latency_ms"] == {"retrieve": 11.0, "decide": 900.0}
        assert first["audio_url"] is None
        # Unset in this session's environment, so the link is null rather than
        # a URL that would render and 404.
        assert first["langfuse_url"] is None
        print(f"replay 200: {len(body['turns'])} turns for session {session_id}")


@pytest.mark.integration
async def test_the_replay_is_not_cached_by_the_browser_or_a_proxy() -> None:
    """The densest content in the app should not outlive the tab."""
    async with _db() as (factory, made):
        session_id = await _seed(factory, made, ["my vpn is down"])
        async with _client("governance") as client:
            response = await client.get(f"/sessions/{session_id}/replay")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.integration
async def test_the_replay_returns_the_stored_text_unredacted() -> None:
    """The documented decision, pinned.

    `redact` is outbound-to-vendor-and-to-logs, so `turns.utterance` holds what
    the employee actually said. A replay exists so a governance reviewer can
    check that against what the agent did, and `[PHONE]` cannot settle that
    question -- so the endpoint returns the text verbatim and the role gate is
    the control. If this ever has to change, it is a policy decision with an ADR
    behind it, not a quiet edit, and this test is the thing that will notice.
    """
    async with _db() as (factory, made):
        session_id = await _seed(factory, made, [PHONE_IN_AN_UTTERANCE])
        async with _client("governance") as client:
            response = await client.get(f"/sessions/{session_id}/replay")
        assert response.status_code == 200
        assert response.json()["turns"][0]["utterance"] == PHONE_IN_AN_UTTERANCE
        assert "[PHONE]" not in response.text


@pytest.mark.integration
async def test_an_empty_session_replays_as_an_empty_chain_not_a_404() -> None:
    async with _db() as (factory, made):
        session_id = await _seed(factory, made, [])
        async with _client("governance") as client:
            response = await client.get(f"/sessions/{session_id}/replay")
        assert response.status_code == 200
        assert response.json()["turns"] == []


@pytest.mark.integration
async def test_governance_gets_404_for_a_session_that_does_not_exist() -> None:
    async with _db() as (factory, made):
        # A real session exists throughout, so the 404 is about *this* id and
        # not about an empty table.
        await _seed(factory, made, ["my vpn is down"])
        async with _client("governance") as client:
            response = await client.get(f"/sessions/{MISSING_SESSION}/replay")
        assert response.status_code == 404


@pytest.mark.integration
async def test_uc1_replay_has_no_authority_over_another_applications_session() -> None:
    """`sessions` is shared. A uc2 session is 404 here, not a transcript."""
    async with _db() as (factory, made):
        session_id = await _seed(
            factory, made, ["a module needs localizing"], app="training_localizer"
        )
        async with _client("governance") as client:
            response = await client.get(f"/sessions/{session_id}/replay")
        assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize(
    "scopes",
    [
        pytest.param((), id="no-roles"),
        pytest.param((UC3_REVIEWER, UC3_LEAD), id="uc3-content-roles"),
    ],
)
async def test_a_real_session_and_a_missing_one_are_both_403_without_the_role(
    scopes: tuple[str, ...],
) -> None:
    """Both orders, against the real database: the 403 must not be an oracle.

    The same caller asks for a session that exists and one that does not, and
    gets byte-identical answers. This is the half a fabricated-row test cannot
    prove: here the real row is genuinely there and genuinely readable by the
    other role, so a 404/403 split would be a real leak rather than a fixture
    artefact.
    """
    async with _db() as (factory, made):
        session_id = await _seed(factory, made, ["my vpn is down"])

        async with _client(*scopes) as client:
            real = await client.get(f"/sessions/{session_id}/replay")
            missing = await client.get(f"/sessions/{MISSING_SESSION}/replay")

        assert real.status_code == 403, real.text
        assert missing.status_code == 403, missing.text
        assert real.json() == missing.json()
        assert "my vpn is down" not in real.text

        # ...and the same session is a 200 for governance, so the 403 above is
        # the role refusing, not the row missing.
        async with _client("governance") as client:
            allowed = await client.get(f"/sessions/{session_id}/replay")
        assert allowed.status_code == 200
        assert allowed.json()["turns"][0]["utterance"] == "my vpn is down"
        print(f"replay 403/200 matrix proven for scopes={scopes or ('<none>',)}")


@pytest.mark.integration
async def test_uc3_governance_semantics_are_not_applied_to_uc1_replay() -> None:
    """A caller holding `governance` *and* a uc3 content role replays here.

    uc3 would refuse it: `SEGREGATED_ROLES` runs before the allow-list, so
    `governance` denies transcripts even alongside `compliance_lead`. uc1 has no
    deny list, and this is the end-to-end proof that uc1's check is its own.
    """
    async with _db() as (factory, made):
        session_id = await _seed(factory, made, ["my vpn is down"])
        async with _client("governance", UC3_LEAD) as client:
            response = await client.get(f"/sessions/{session_id}/replay")
        assert response.status_code == 200
        assert response.json()["turns"][0]["utterance"] == "my vpn is down"
