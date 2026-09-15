import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from indic_platform.db.models import Session as SessionRow
from indic_platform.db.models import TicketFiling, Turn
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.authentication import AuthCredentials, BaseUser

from helpdesk_agent import roles
from helpdesk_agent.graph import initial_state
from helpdesk_agent.persistence import run_turn


def _refuse_dev_bypass_in_production() -> None:
    """Fail to import rather than serve a fake identity in production.

    The per-request check in `authenticated_employee` cannot be skipped, but it
    is also not the one anybody notices. This one turns a misconfigured
    deployment into a process that will not start.
    """
    roles.check_dev_bypass()


_refuse_dev_bypass_in_production()

app = FastAPI(title="helpdesk agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "stage": "scaffold"}


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID | None = None
    utterance: str = Field(min_length=1, max_length=16000)
    language: Literal["hi-IN", "hi-Latn", "te-IN", "ta-IN", "en-IN"]


def authenticated_employee(request: Request) -> str:
    """Only trusted server-side SSO middleware can populate this ASGI principal.

    A bare deployment fails closed. Neither body fields nor identity headers
    authenticate callers. The middleware must validate SSO tokens and expose a
    stable, issuer-qualified employee subject as BaseUser.identity.

    The single exception is the local development bypass, which `roles` refuses
    outside a known non-production `ENV` -- and refuses by raising, so a
    production deployment with the flag set fails rather than quietly admitting
    a fake employee. See `roles.check_dev_bypass`.
    """
    if roles.dev_bypass_active():
        return roles.DEV_BYPASS_IDENTITY
    user = request.scope.get("user")
    credentials = request.scope.get("auth")
    if (
        not isinstance(user, BaseUser)
        or not user.is_authenticated
        or not isinstance(credentials, AuthCredentials)
        or "authenticated" not in credentials.scopes
    ):
        raise HTTPException(401, "Authenticated employee required")
    try:
        identity = user.identity
    except NotImplementedError as exc:
        raise HTTPException(401, "Verified employee subject required") from exc
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 128:
        raise HTTPException(401, "Invalid employee subject")
    return identity


def authenticated_caller(request: Request) -> roles.Caller:
    """The verified subject plus the roles uc1 recognised on the same claim.

    Identity and roles come from one place -- the credentials trusted SSO
    middleware put on the ASGI scope -- so there is no second, weaker path by
    which a caller could acquire a role its token did not carry.
    """
    if roles.dev_bypass_active():
        return roles.Caller(roles.DEV_BYPASS_IDENTITY, roles.dev_bypass_roles())
    return roles.Caller(authenticated_employee(request), roles.caller_roles(request))


def replay_reader(caller: Annotated[roles.Caller, Depends(authenticated_caller)]) -> roles.Caller:
    return roles.enforce_replay(caller)


#: Any verified subject, roles or none.
Caller = Annotated[roles.Caller, Depends(authenticated_caller)]
#: A verified subject holding uc1's replay role. See `roles.py` -- uc1-scoped.
ReplayReader = Annotated[roles.Caller, Depends(replay_reader)]


def engine() -> Any:
    return create_async_engine(os.environ["DATABASE_URL"])


async def db_session() -> Any:
    """One read session per request. `run_turn` still owns its own transaction."""
    eng = engine()
    try:
        async with AsyncSession(eng) as session:
            yield session
    finally:
        await eng.dispose()


Db = Annotated[AsyncSession, Depends(db_session)]


@app.post("/chat/turn")
async def chat_turn(
    body: ChatTurn, employee_id: Annotated[str, Depends(authenticated_employee)]
) -> dict[str, object]:
    state = initial_state(body.utterance, body.language, [])
    state["employee_id"] = employee_id
    if body.session_id:
        state["session_id"] = str(body.session_id)
    try:
        result = await run_turn(state, existing=body.session_id is not None)
    except LookupError as exc:
        raise HTTPException(404, "Session not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, "Session employee mismatch") from exc
    decision = result["decision"]
    assert decision is not None
    return {
        "session_id": result["session_id"],
        "decision": decision.model_dump(),
        "ticket_id": result["ticket_id"],
    }


@app.get("/me")
def me(caller: Caller) -> dict[str, object]:
    """Who the server thinks you are, and which roles it recognised.

    Open to any verified subject, including one holding no roles at all: a
    caller with no grant needs to be told that, and refusing `/me` would leave
    the UI unable to render anything but a blank page. Mirrors
    `comms_surveillance.api.me` in spirit.

    Roles are reported, never accepted. The list is the recognised subset of
    the verified claim's scopes (`roles.recognised_roles`), so an unknown scope
    is not echoed back and the browser cannot learn a role name by asking for
    one.
    """
    return caller.as_json()


# --- session replay (uc1/P6) ---------------------------------------------------


def langfuse_trace_url(trace_id: str) -> str | None:
    """A deep link to this turn's Langfuse trace, or None if one cannot be built.

    Langfuse's trace route is `{host}/project/{project_id}/traces/{trace_id}`
    (the SDK builds the same string in `Langfuse.get_trace_url`). The project id
    is not derivable from the trace id, so both `LANGFUSE_HOST` and
    `LANGFUSE_PROJECT_ID` have to be configured for the link to resolve.

    When either is unset this returns None and the field is null. That is the
    whole point of the function: a URL built from a host alone, or from a
    guessed project id, is a link that renders in the reviewer's browser and
    lands on a 404 -- worse than no link, because an auditor who follows it
    concludes the trace was lost rather than that the deployment was not
    configured.

    `LANGFUSE_PROJECT_ID` is not in `.env.example` yet; adding it there is
    outstanding (see the handback for uc1/P6). Until it is set, every
    `langfuse_url` in a replay is null and `trace_id` is what a reviewer takes
    to Langfuse by hand.
    """
    host = os.environ.get("LANGFUSE_HOST", "").strip().rstrip("/")
    project = os.environ.get("LANGFUSE_PROJECT_ID", "").strip()
    if not host or not project or not trace_id.strip():
        return None
    return f"{host}/project/{quote(project, safe='')}/traces/{quote(trace_id, safe='')}"


def _ticket(filing: TicketFiling | None) -> dict[str, object] | None:
    """What this turn did to Zammad: the status and the number, or null.

    Two named fields rather than the row: `payload` is the ticket body, already
    visible as the decision the graph produced, and `source_key`, `attempts` and
    `last_error` describe the filing machinery rather than the conversation.
    Projecting named fields is also what stops a column added later from
    riding into this response unreviewed.
    """
    if filing is None:
        return None
    return {"status": filing.status, "ticket_number": filing.ticket_number}


def replay_payload(
    session: SessionRow,
    turns: Sequence[Turn],
    filings: Mapping[int, TicketFiling],
) -> dict[str, object]:
    """The full chain for one session, as `GET /sessions/{id}/replay` returns it.

    Pure, so the shape is provable from fabricated rows without a database.

    ## `turn_index` is positional, and which position matters

    `turns` has no `turn_index` column. The index here is the row's position
    under `order_by(Turn.created_at, Turn.id)` -- the same ordering
    `persistence.run_turn` uses to rebuild a session's history and
    `persistence.record_voice_latency` uses to address a turn by offset, and the
    same number `ticket_filings.turn_index` was written with. They have to agree:
    under any other ordering, index *n* here and index *n* there are different
    rows, and this response would attribute one turn's ticket to another's
    words. The caller does the ordering in SQL; `filings` is keyed by that index.

    ## What this returns, and what it deliberately does not do to it

    Everything, verbatim. The utterance as the employee said it, the decision,
    the retrieval evidence, the grounding verdicts under
    `decision_json._grounding` and the guard rejections under `_guard_errors`.

    That is a decision, not a default, and it is worth stating why, because this
    is the densest content this application ever puts in one response.
    `indic_platform.security.redact` is an **outbound-to-vendor-and-to-logs**
    hook -- `persistence.run_turn` says so where it writes the row ("Keep
    persisted content locally; vendor/log exports are redacted by adapters"), so
    the stored text is unredacted by design. Redacting it on the way to an
    authorised reviewer would be a different control with a different purpose,
    and it would defeat this endpoint's: a replay exists so a governance
    reviewer can check what the employee actually said against what the agent
    actually did, and a transcript reading `[PHONE]` where the ticket's
    grounding evidence quotes the digits cannot settle that question. It would
    also make the replay disagree with the grounding hashes in `_grounding`,
    which are taken over the unredacted evidence.

    So the exposure is controlled by *who*, not by *what*:

    - the role gate is the whole control, and it is checked in a dependency
      before this function is reached (`roles.enforce_replay`);
    - the route sets `Cache-Control: no-store`, so the densest payload in the
      app is not left in a browser or proxy cache after the tab closes;
    - nothing here is logged. The route logs no line at all -- not the payload,
      not a field of it, not the session id. A log line is exactly the export
      `redact` exists to cover, and the way to keep this content out of logs is
      not to write it to one.

    `audio_url` is **always null in this build**, and will be until a recording
    store exists. uc1 keeps no audio at rest: the voice pipeline holds PCM in
    memory for the utterance, there is no LiveKit egress or recording service in
    `docker-compose.yml` and the app uses no bucket -- which is why uc1/P7's
    30-day audio retention policy resolves a `NoAudioSink` and matches nothing
    (`helpdesk_agent/retention.py`, `apps/helpdesk_agent/README.md`). The field
    is present and null rather than absent so the contract's shape is stable,
    and no URL scheme is invented for it: a link that resolves to nothing tells
    a reviewer the recording was lost rather than that none was ever made.
    """
    return {
        "session_id": str(session.id),
        "employee_id": session.metadata_json.get("employee_id"),
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "turns": [
            {
                "turn_index": index,
                "utterance": turn.utterance,
                "language": turn.language,
                "decision_json": turn.decision_json,
                "retrieval_json": turn.retrieval_json,
                "latency_ms": turn.latency_ms,
                "model": turn.model,
                "prompt_version": turn.prompt_version,
                "policy_version": turn.policy_version,
                "trace_id": turn.trace_id,
                "langfuse_url": langfuse_trace_url(turn.trace_id or ""),
                # uc1 stores no audio at rest; see this function's docstring.
                "audio_url": None,
                "ticket": _ticket(filings.get(index)),
            }
            for index, turn in enumerate(turns)
        ],
    }


def _by_turn_index(filings: Iterable[TicketFiling]) -> dict[int, TicketFiling]:
    return {filing.turn_index: filing for filing in filings}


@app.get("/sessions/{session_id}/replay")
async def session_replay(
    session_id: UUID,
    caller: ReplayReader,
    db: Db,
    response: Response,
) -> dict[str, object]:
    """The full chain for one helpdesk session, for a uc1 governance reader.

    `ReplayReader` is uc1's `governance` role and it is **not** uc3's role of
    the same name -- read `helpdesk_agent/roles.py` before mapping a directory
    group to it. There, `governance` denies transcript access; here it is the
    only thing that grants it.

    A caller without the role gets 403 for every session id, real or invented,
    because the dependency is solved before the path parameter is parsed and
    before anything is looked up. A caller *with* the role gets 404 for a
    session that does not exist, and for one belonging to another application:
    `sessions` is a shared platform table and uc1's replay has no authority over
    uc2's or uc3's rows.

    `Cache-Control: no-store` because of what the body is; see `replay_payload`.
    """
    session = await db.get(SessionRow, session_id)
    if session is None or session.app != "helpdesk_agent":
        raise HTTPException(404, "Session not found")
    turns = (
        await db.scalars(
            select(Turn).where(Turn.session_id == session_id).order_by(Turn.created_at, Turn.id)
        )
    ).all()
    filings = (
        await db.scalars(select(TicketFiling).where(TicketFiling.session_id == session_id))
    ).all()
    response.headers["Cache-Control"] = "no-store"
    return replay_payload(session, list(turns), _by_turn_index(filings))
