"""The per-session spend cap is only a control if something opens the scope.

`indic_platform.adapters.budget` implements session, day and month caps, and
`platform/tests/test_spend_caps.py` proves the ledger's arithmetic. None of that binds a
single real adapter call to a real session: the session scope is a contextvar, and a cap
keyed on a scope nobody enters is a cap on nothing. That gap is easy to reintroduce --
deleting one `with` line in `persistence.run_turn` leaves every spend-cap test passing --
so this file pins the wiring itself.

The test does not need Postgres, Qdrant, TEI or a vendor key. `run_turn` opens the scope
around the block that does the work, so replacing the database session with a double that
reads `budget.current_session_id()` on entry and then bails out is enough to observe it,
and it observes the real `run_turn`, not a stand-in for it.
"""

import uuid
from typing import Any

import pytest
from helpdesk_agent import persistence
from helpdesk_agent.graph import initial_state
from indic_platform.adapters import budget


class _Bail(RuntimeError):
    """Ends the turn once the scope has been observed. Nothing downstream is exercised."""


def _turn_state(session_id: str, employee_id: str) -> Any:
    state = initial_state("printer broken", "en-IN", [])
    state["session_id"] = session_id
    state["employee_id"] = employee_id
    return state


class _Sink:
    """Stands in for the Langfuse sink `run_turn` insists on before it starts."""

    def emit(self, record: dict[str, Any]) -> None:  # pragma: no cover - never called
        raise AssertionError("the turn is stopped before anything is emitted")

    def flush(self) -> None:
        """`run_turn` flushes in its `finally`, so this is reached on the way out."""


def _reach_the_scope(monkeypatch: pytest.MonkeyPatch, session: type) -> None:
    """Get `run_turn` as far as the budget scope without a database or Langfuse.

    Three things stand in front of it, and none of them is what this file is about:
    `os.environ["DATABASE_URL"]`, which the unit CI job does not set (a local run only
    has it because `settings.py` loads `.env.stack`, which is exactly the difference that
    made the first version of these tests pass here and fail in CI); the Langfuse sink,
    which `run_turn` requires by type; and the database session itself, replaced by
    `session` so the turn stops the moment the scope is observable.

    The URL is never connected to -- `create_async_engine` is lazy -- so a syntactically
    valid DSN pointing nowhere is enough, and is honest about needing no server.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:1/unused")
    monkeypatch.setattr(persistence, "LangfuseSink", _Sink)
    monkeypatch.setattr(persistence, "default_sink", _Sink)
    monkeypatch.setattr(persistence, "AsyncSession", session)


async def test_a_turn_charges_its_adapter_calls_to_its_own_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside a turn, the ambient session id is that turn's session -- not None.

    None is what a missing `session_scope` looks like, and it is not a loud failure: the
    day and month caps still apply, so spend still stops eventually, and the only symptom
    is that one runaway session can burn the whole day's budget instead of its own share.
    That is precisely threat T9 (denial of wallet) surviving a control that appears to be
    in place.
    """
    session_id = str(uuid.uuid4())
    seen: list[str | None] = []

    class SpySession:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "SpySession":
            seen.append(budget.current_session_id())
            raise _Bail

        async def __aexit__(self, *_: Any) -> bool:
            return False

    _reach_the_scope(monkeypatch, SpySession)

    with pytest.raises(_Bail):
        await persistence.run_turn(_turn_state(session_id, "emp-budget@example.com"))

    assert seen == [session_id], (
        "a turn must charge its adapter calls to its own session; "
        f"the ambient session id was {seen!r}"
    )


async def test_the_scope_does_not_leak_past_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn that raises still closes its scope.

    `run_turn` fails in plenty of ordinary ways -- an unknown session, an employee
    mismatch, a vendor timeout. If the contextvar survived one of those, the next turn on
    the same worker would be charged to the previous employee's session, which is both a
    wrong bill and a small cross-session information leak in the metrics.
    """

    class Boom:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> "Boom":
            raise _Bail

        async def __aexit__(self, *_: Any) -> bool:
            return False

    _reach_the_scope(monkeypatch, Boom)

    assert budget.current_session_id() is None
    with pytest.raises(_Bail):
        await persistence.run_turn(_turn_state(str(uuid.uuid4()), "emp-budget@example.com"))
    assert budget.current_session_id() is None, "the session scope outlived the turn that set it"
