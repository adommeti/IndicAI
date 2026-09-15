"""Who is calling, and what that lets them see (PRD E8 roles, uc3/P6).

Identity comes from the ASGI scope that server-side SSO middleware populated --
`request.scope["user"]` and `request.scope["auth"]` -- exactly as
`training_localizer.api.authenticated_owner` reads it. Never from a header, a
query parameter or a body field: anything the browser can set is not an
identity, it is a claim by the caller about itself.

Fail closed. A deployment with no trusted authentication middleware in front of
it has no verified subject on the scope, so every route here answers 401 rather
than defaulting to an anonymous reader. That is deliberate: the alternative
failure mode is an unauthenticated surveillance queue.

## The three roles, and why `governance` is subtractive

`compliance_reviewer` works the queue. `compliance_lead` is a strict superset:
everything a reviewer can do, plus the random QA-sample stream that the false
negative estimate is built from.

`governance` is not a third rung of the same ladder. It is a
segregation-of-duties role: the people who read the *numbers* about the
surveillance programme are deliberately not the people who can read the
*calls*. So it is enforced here as a deny, not as a smaller grant --
`SEGREGATED_ROLES` is checked after the allow-list, and a principal carrying
`governance` is refused transcripts and audio even if it also carries a
reviewer role.

That is the stricter of the two readings of the contract, and it is chosen on
purpose. Additive roles would mean a caller holding `governance` *and*
`compliance_reviewer` could fetch transcripts, which is true to how RBAC
usually composes but makes the guarantee "governance cannot fetch transcripts"
conditional on nobody ever being granted both. A dual-hatted person needs two
subjects, which is what segregation of duties means in the first place.
"""

import logging
import os
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from starlette.authentication import AuthCredentials, BaseUser

log = logging.getLogger(__name__)

ROLE_REVIEWER = "compliance_reviewer"
ROLE_LEAD = "compliance_lead"
ROLE_GOVERNANCE = "governance"

KNOWN_ROLES = frozenset({ROLE_REVIEWER, ROLE_LEAD, ROLE_GOVERNANCE})

# Reviewer work: the queue, a flag's transcript, its audio, its dispositions.
CASE_ROLES = frozenset({ROLE_REVIEWER, ROLE_LEAD})
# The QA-sample stream is the lead's alone.
LEAD_ROLES = frozenset({ROLE_LEAD})
# Aggregates and chain status: all three, because that is the whole of what
# governance is for.
METRICS_ROLES = KNOWN_ROLES

# Held alongside anything else, these still deny the transcript-bearing routes.
# See the module docstring.
SEGREGATED_ROLES = frozenset({ROLE_GOVERNANCE})

# The scope every SSO backend in this repo sets once it has verified a subject.
AUTHENTICATED = "authenticated"

DEV_BYPASS_IDENTITY = "dev-bypass@example.test"
DEV_BYPASS_ROLES = (ROLE_REVIEWER, ROLE_LEAD)


class DevBypassRefused(RuntimeError):
    """`AUTH__DEV_BYPASS=true` with `ENV=prod` (.claude/rules/apps.md)."""


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def dev_bypass_requested() -> bool:
    return _flag("AUTH__DEV_BYPASS")


def check_dev_bypass() -> None:
    """Raise if the dev bypass is switched on in production.

    Called from the app's lifespan so a misconfigured deployment refuses to
    start, and again on every request so the same misconfiguration applied to a
    running process cannot quietly take effect. Belt and braces on purpose: the
    startup check is the one that gets noticed, the per-request check is the one
    that cannot be skipped by setting the variable after boot.
    """
    if dev_bypass_requested() and os.environ.get("ENV", "").strip().lower() == "prod":
        raise DevBypassRefused(
            "AUTH__DEV_BYPASS=true is refused when ENV=prod. "
            "Remove the flag or run against real SSO."
        )


def dev_bypass_roles() -> frozenset[str]:
    """Which roles the fixed test identity carries.

    Defaults to reviewer + lead rather than to every role, because `governance`
    is subtractive: a bypass that handed out all three would deny the local UI
    the transcripts it exists to render, and the developer would read that as a
    bug in the API. `AUTH__DEV_BYPASS_ROLES=governance` switches to the
    governance view.
    """
    raw = os.environ.get("AUTH__DEV_BYPASS_ROLES", "")
    named = {item.strip() for item in raw.split(",") if item.strip()}
    roles = named & KNOWN_ROLES if named else set(DEV_BYPASS_ROLES)
    return frozenset(roles)


@dataclass(frozen=True)
class Principal:
    """A verified caller: who they are, what roles the claim carried."""

    identity: str
    roles: frozenset[str]
    dev_bypass: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "roles": sorted(self.roles),
            "dev_bypass": self.dev_bypass,
        }


def _subject(request: Request) -> str:
    """The verified subject on the scope, or 401.

    Mirrors `training_localizer.api.authenticated_owner`: the user object has to
    be a Starlette `BaseUser` that says it is authenticated, the credentials
    have to be `AuthCredentials` carrying the `authenticated` scope, and the
    identity has to be a non-empty string of sane length. Each of those is a
    separate way a half-configured middleware stack can present an unverified
    caller as a verified one.
    """
    user = request.scope.get("user")
    credentials = request.scope.get("auth")
    if (
        not isinstance(user, BaseUser)
        or not user.is_authenticated
        or not isinstance(credentials, AuthCredentials)
        or AUTHENTICATED not in credentials.scopes
    ):
        raise HTTPException(401, "Authenticated compliance subject required")
    try:
        identity = user.identity
    except NotImplementedError as exc:
        raise HTTPException(401, "Verified subject required") from exc
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 128:
        raise HTTPException(401, "Invalid subject")
    return identity


def principal(request: Request) -> Principal:
    """The calling principal, or 401. Roles are the scopes we recognise.

    Scopes we do not recognise are dropped rather than carried: a role this
    build has no rule for must never be mistaken for one that it does, and
    echoing arbitrary scope strings back through `/me` would make the SSO
    claim a reflection surface.
    """
    try:
        check_dev_bypass()
    except DevBypassRefused as exc:
        log.error("refusing to serve: %s", exc)
        raise HTTPException(500, "Authentication is misconfigured") from exc

    if dev_bypass_requested():
        return Principal(DEV_BYPASS_IDENTITY, dev_bypass_roles(), dev_bypass=True)

    identity = _subject(request)
    credentials = request.scope["auth"]
    return Principal(identity, frozenset(set(credentials.scopes) & KNOWN_ROLES))


def require(allowed: frozenset[str], *, deny: frozenset[str] = frozenset()) -> object:
    """A dependency admitting only these roles, and never those in `deny`.

    Returned as a dependency so the check runs during FastAPI's dependency
    solving -- which happens *before* path, query and body validation. That
    ordering is the reason a refused caller gets 403 rather than a 422 that
    would have echoed their request back to them, and the reason an unknown
    flag id is 403 rather than the 404 that would have confirmed, to a caller
    with no right to know, whether that flag exists.
    """

    def dependency(caller: Annotated[Principal, Depends(principal)]) -> Principal:
        if denied := caller.roles & deny:
            raise HTTPException(
                403,
                f"The {'/'.join(sorted(denied))} role is refused this resource",
            )
        if not caller.roles & allowed:
            raise HTTPException(403, "This role is refused this resource")
        return caller

    return Depends(dependency)


# The four grants the API uses. Named rather than inlined so a route cannot
# quietly acquire a different rule than the one the contract's table states.
Authenticated = Depends(principal)
CaseReader = require(CASE_ROLES, deny=SEGREGATED_ROLES)
LeadOnly = require(LEAD_ROLES, deny=SEGREGATED_ROLES)
MetricsReader = require(METRICS_ROLES)
