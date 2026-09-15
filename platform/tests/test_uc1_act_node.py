"""uc1 act node: file the ticket and read its number back in the session language.

Ticketing is mocked throughout: these tests need neither Postgres nor Zammad. A test that
needs the stack is marked ``integration``; a test that needs Zammad is marked ``ticketing``.
"""

import re
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from helpdesk_agent import ticketing
from helpdesk_agent.graph import (
    ESCALATE,
    OUTAGE_TICKET,
    TICKET_FILED,
    TICKET_PENDING,
    Agent,
    Decision,
    TurnState,
    initial_state,
    language_matches,
)
from helpdesk_agent.retriever import RetrievalResult
from indic_platform.adapters.runtime import CircuitOpen
from sqlalchemy.ext.asyncio import AsyncSession

# The act node only hands the session to create_ticket, which is mocked here.
DB = cast(AsyncSession, object())

UTTERANCES = {
    "en-IN": "the printer near my desk is jammed and I need help",
    "hi-IN": "मेरे डेस्क के पास वाले प्रिंटर में कागज़ फँस गया है",
    "hi-Latn": "mere desk ke paas wale printer mein kagaz fas gaya hai",
    "te-IN": "నా డెస్క్ దగ్గరి ప్రింటర్‌లో కాగితం ఇరుక్కుంది",
    "ta-IN": "என் மேசைக்கு அருகில் உள்ள அச்சுப்பொறியில் காகிதம் சிக்கிக்கொண்டது",
}
LANGUAGES = list(UTTERANCES)
# Shapes a Zammad ticket number can take; a pending reply must contain none of them.
NUMBER_FORMATS = ["70001", "2024070001", "#70042", "ZAM-70042", "10001-2026"]


def filed(number: str | None, *, pending: bool = False, replay: bool = False) -> Any:
    return ticketing.Filed(ticket_number=number, pending=pending, idempotent_replay=replay)


async def run_act(
    language: str,
    *,
    create: Any,
    db: AsyncSession | None = DB,
    turn_index: int = 0,
    vendor_down: bool = False,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TurnState, dict[str, Any]]:
    """Drive the real graph to the act node via the clarify-cap fallback, as uc1 tests do."""
    monkeypatch.setattr(ticketing, "create_ticket", create)
    retrieve = AsyncMock(
        return_value=RetrievalResult(
            chunks=[], translate_status="disabled", latency_s=0, original_latency_s=0
        )
    )
    structured = AsyncMock(
        side_effect=CircuitOpen() if vendor_down else None,
        return_value=None if vendor_down else Decision(action="clarify", reply_text="Explain."),
    )
    state = initial_state(UTTERANCES[language], language, [])
    state["clarify_count"] = 2
    metadata: dict[str, Any] = {}
    result = await Agent(
        retrieve=retrieve, structured=structured, db=db, turn_index=turn_index
    ).run(state, metadata=metadata)
    assert result["decision"] is not None
    assert result["decision"].action == "file_ticket"
    return cast(TurnState, result), metadata


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("number", NUMBER_FORMATS)
async def test_filed_reply_reads_the_number_back_in_session_language(
    language: str, number: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = AsyncMock(return_value=filed(number))
    result, metadata = await run_act(language, create=create, monkeypatch=monkeypatch)
    decision = result["decision"]
    assert decision is not None
    reply = decision.reply_text
    assert number in reply
    assert result["ticket_id"] == number
    assert language_matches(reply, language, UTTERANCES[language])
    assert reply == TICKET_FILED[language].format(number=number)
    # The stub notices must be gone; the employee is not told submission is disabled.
    assert reply not in (ESCALATE[language], OUTAGE_TICKET[language])
    assert metadata["ticket"] == {"pending": False, "idempotent_replay": False, "number": number}


@pytest.mark.parametrize("language", LANGUAGES)
async def test_pending_reply_promises_email_and_invents_no_number(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = AsyncMock(return_value=filed(None, pending=True))
    result, metadata = await run_act(language, create=create, monkeypatch=monkeypatch)
    decision = result["decision"]
    assert decision is not None
    reply = decision.reply_text
    assert result["ticket_id"] is None
    assert metadata["ticket"]["pending"] is True
    assert "number" not in metadata["ticket"]
    assert reply == TICKET_PENDING[language]
    assert language_matches(reply, language, UTTERANCES[language])
    # No digit at all, so no digit sequence of any ticket-number format can be read out of it.
    assert re.search(r"\d", reply) is None
    for candidate in NUMBER_FORMATS:
        for run in re.findall(r"\d+", candidate):
            assert run not in reply
    assert reply != TICKET_FILED[language].format(number="")


@pytest.mark.parametrize("language", LANGUAGES)
async def test_pending_and_filed_replies_are_distinct_per_language(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending, _ = await run_act(
        language, create=AsyncMock(return_value=filed(None, pending=True)), monkeypatch=monkeypatch
    )
    done, _ = await run_act(
        language, create=AsyncMock(return_value=filed("70042")), monkeypatch=monkeypatch
    )
    assert pending["decision"] is not None and done["decision"] is not None
    assert pending["decision"].reply_text != done["decision"].reply_text


@pytest.mark.parametrize("pending", [True, False])
async def test_romanised_hindi_user_gets_a_romanised_reply(
    pending: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hi-IN typed in Latin script must not be answered in Devanagari, filed or pending."""
    create = AsyncMock(return_value=filed(None if pending else "70042", pending=pending))
    monkeypatch.setattr(ticketing, "create_ticket", create)
    retrieve = AsyncMock(
        return_value=RetrievalResult(
            chunks=[], translate_status="disabled", latency_s=0, original_latency_s=0
        )
    )
    structured = AsyncMock(return_value=Decision(action="clarify", reply_text="Explain."))
    utterance = UTTERANCES["hi-Latn"]
    state = initial_state(utterance, "hi-IN", [])
    state["clarify_count"] = 2
    result = await Agent(retrieve=retrieve, structured=structured, db=DB).run(state)
    decision = result["decision"]
    assert decision is not None
    reply = decision.reply_text
    assert not re.search(r"[ऀ-ॿ]", reply)
    assert language_matches(reply, "hi-IN", utterance)
    table = TICKET_PENDING if pending else TICKET_FILED
    assert reply == table["hi-Latn"].format(number="70042")
    assert reply != table["hi-IN"].format(number="70042")


@pytest.mark.parametrize("language", LANGUAGES)
async def test_escaped_ticketing_unavailable_is_treated_as_pending(
    language: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = AsyncMock(side_effect=ticketing.TicketingUnavailable("zammad down"))
    result, metadata = await run_act(language, create=create, monkeypatch=monkeypatch)
    decision = result["decision"]
    assert decision is not None
    assert decision.reply_text == TICKET_PENDING[language]
    assert result["ticket_id"] is None
    assert re.search(r"\d", decision.reply_text) is None
    assert metadata["ticket"] == {"pending": True, "idempotent_replay": False}


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("malformed ticket payload"),
        RuntimeError("bad column"),
        KeyError("source_session_id"),
        TypeError("create_ticket() got an unexpected keyword argument"),
    ],
)
async def test_unexpected_failure_is_not_reported_as_filed_or_pending(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A defect must surface, not become a cheerful reply or a silent pending marker."""
    assert not isinstance(failure, ticketing.TicketingUnavailable)
    create = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)):
        await run_act("en-IN", create=create, monkeypatch=monkeypatch)
    create.assert_awaited_once()


async def test_unexpected_failure_leaves_no_ticket_reply_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = AsyncMock(side_effect=ValueError("malformed ticket payload"))
    monkeypatch.setattr(ticketing, "create_ticket", create)
    retrieve = AsyncMock(
        return_value=RetrievalResult(
            chunks=[], translate_status="disabled", latency_s=0, original_latency_s=0
        )
    )
    structured = AsyncMock(return_value=Decision(action="clarify", reply_text="Explain."))
    state = initial_state(UTTERANCES["en-IN"], "en-IN", [])
    state["clarify_count"] = 2
    metadata: dict[str, Any] = {}
    with pytest.raises(ValueError):
        await Agent(retrieve=retrieve, structured=structured, db=DB).run(state, metadata=metadata)
    assert "ticket" not in metadata
    assert state["ticket_id"] is None


async def test_idempotent_replay_still_reads_the_existing_number_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = AsyncMock(return_value=filed("70042", replay=True))
    result, metadata = await run_act("en-IN", create=create, turn_index=3, monkeypatch=monkeypatch)
    decision = result["decision"]
    assert decision is not None
    assert "70042" in decision.reply_text
    assert result["ticket_id"] == "70042"
    assert metadata["ticket"]["idempotent_replay"] is True


async def test_missing_number_without_pending_flag_never_claims_a_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contract violation from ticketing degrades to the pending wording, not a filed one."""
    create = AsyncMock(return_value=filed(None, pending=False))
    result, _ = await run_act("en-IN", create=create, monkeypatch=monkeypatch)
    decision = result["decision"]
    assert decision is not None
    assert decision.reply_text == TICKET_PENDING["en-IN"]
    assert result["ticket_id"] is None
    assert "{number}" not in decision.reply_text
    assert "None" not in decision.reply_text


async def test_create_ticket_receives_the_session_turn_and_employee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = AsyncMock(return_value=filed("70042"))
    monkeypatch.setattr(ticketing, "create_ticket", create)
    retrieve = AsyncMock(
        return_value=RetrievalResult(
            chunks=[], translate_status="disabled", latency_s=0, original_latency_s=0
        )
    )
    structured = AsyncMock(return_value=Decision(action="clarify", reply_text="Explain."))
    state = initial_state(UTTERANCES["en-IN"], "en-IN", [])
    state["clarify_count"] = 2
    state["employee_id"] = "E1"
    result = await Agent(retrieve=retrieve, structured=structured, db=DB, turn_index=4).run(state)
    create.assert_awaited_once()
    decision = result["decision"]
    assert decision is not None
    assert create.await_args is not None
    args, kwargs = create.await_args
    assert args[0] is DB
    assert args[1] is decision.ticket
    assert kwargs["session_id"] == uuid.UUID(state["session_id"])
    assert kwargs["turn_index"] == 4
    assert kwargs["employee_id"] == "E1"


@pytest.mark.parametrize("vendor_down", [False, True])
async def test_vendor_outage_ticket_is_still_filed_and_read_back(
    vendor_down: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    create = AsyncMock(return_value=filed("70042"))
    result, metadata = await run_act(
        "hi-IN", create=create, vendor_down=vendor_down, monkeypatch=monkeypatch
    )
    decision = result["decision"]
    assert decision is not None
    assert metadata.get("vendor_unavailable", False) is vendor_down
    assert "70042" in decision.reply_text
    assert language_matches(decision.reply_text, "hi-IN", UTTERANCES["hi-IN"])
    create.assert_awaited_once()


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("vendor_down", [False, True])
async def test_without_a_session_nothing_is_filed_and_the_notice_stands(
    language: str, vendor_down: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No database session means no side effect: keep the pre-ticketing notice, not a number."""
    create = AsyncMock(return_value=filed("70042"))
    result, _ = await run_act(
        language, create=create, db=None, vendor_down=vendor_down, monkeypatch=monkeypatch
    )
    decision = result["decision"]
    assert decision is not None
    create.assert_not_awaited()
    assert result["ticket_id"] is None
    expected = (OUTAGE_TICKET if vendor_down else ESCALATE)[language]
    assert decision.reply_text == expected
    assert re.search(r"\d", decision.reply_text) is None
