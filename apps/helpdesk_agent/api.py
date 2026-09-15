from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.authentication import AuthCredentials, BaseUser

from helpdesk_agent.graph import initial_state
from helpdesk_agent.persistence import run_turn

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
    """
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
