"""uc1's roles, and the one word in them that means the opposite elsewhere.

## Read this before mapping an Entra group to this application

`governance` in **uc1** is the role that *grants* the most content access there
is: `GET /sessions/{id}/replay` returns a whole helpdesk session -- every
utterance an employee typed or spoke, every retrieval, every model decision and
every guard verdict -- and this role is the only thing that opens it.

`governance` in **uc3** (`apps/comms_surveillance/auth.py`, ADR 0013) is
*subtractive*. A principal carrying it is refused transcripts and audio even
when it also carries `compliance_reviewer` or `compliance_lead`: it is oversight
of the surveillance programme *without* access to the calls, enforced as a deny
that runs before any allow-list.

Same claim name, opposite effect. That is a deliberate decision by the build
coordinator -- uc1/P6's PRD requirement is that replay is restricted to a
"governance" role, and uc3/P6's is that governance cannot read transcripts, and
both are implemented as written -- but it is only safe because **the role is
application-scoped**.

**A deployment MUST map these to two different Entra groups.** One group named
`governance` granted to both applications would hand the helpdesk's full
transcript history to exactly the people uc3 exists to keep away from call
content, and it would do it silently: both apps would be behaving as specified.
Name them separately at the directory (`uc1-helpdesk-governance` and
`uc3-surveillance-governance`, or whatever the directory's convention is) and
map each to one application only.

Nothing in this module imports `comms_surveillance`. uc1's decision to admit a
caller is made from `KNOWN_ROLES` below and nothing else -- it does not consult
uc3's `CASE_ROLES`, `LEAD_ROLES` or `SEGREGATED_ROLES`, and holding a uc3 role
neither grants nor denies anything here. `platform/tests/test_uc1_replay.py`
pins that: a caller carrying uc3's content roles is refused uc1 replay, and a
caller carrying `governance` *and* a uc3 content role is admitted, where uc3
would have denied it.

## Where roles come from

The same place the identity comes from, and nowhere else: the `AuthCredentials`
that trusted server-side SSO middleware put on the ASGI scope
(`helpdesk_agent.api.authenticated_employee` reads the matching `BaseUser`).
Never a header, a query parameter or a body field -- anything the browser can
set is a claim by the caller about itself. `.claude/rules/ui.md` says roles come
from the API; this is the API end of that sentence, and UI hiding is not access
control.

Scopes this build has no rule for are dropped rather than carried. A role name
nobody has written a rule for must not be mistaken for one that has a rule, and
`GET /me` echoing arbitrary scope strings back to the browser would make the SSO
claim a reflection surface.

uc1 has exactly two identity paths, and this module is where both live. The first
is the SSO principal `api.authenticated_employee` reads off the ASGI scope. The
second is the local development bypass at the bottom of this file, added in
uc1/P6 -- read it before answering any question about how a caller gets in here.
`VITE_AUTH_DEV_BYPASS` is the browser half of the UI's own
local convenience and grants nothing on its own; the server-side half is
`AUTH__DEV_BYPASS` at the bottom of this module, which is refused unless `ENV`
names a known non-production environment. Without both, a local API needs real
authentication middleware in front of it or every route answers 401.
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import HTTPException, Request
from starlette.authentication import AuthCredentials

#: The replay role. uc1-scoped; see the module docstring before reusing the name.
ROLE_GOVERNANCE = "governance"

#: Every role uc1 has a rule for. Scopes outside this set are dropped.
KNOWN_ROLES = frozenset({ROLE_GOVERNANCE})

#: Who may replay a session. Held separately from `KNOWN_ROLES` so a role added
#: to this app later does not silently acquire replay by being known.
REPLAY_ROLES = frozenset({ROLE_GOVERNANCE})

#: The scope every SSO backend in this repo sets once it has verified a subject.
AUTHENTICATED = "authenticated"

#: Named so the refusal cannot drift into naming the session, the caller, or
#: which roles would have worked.
REPLAY_REFUSED = "The uc1 governance role is required to replay a session"


@dataclass(frozen=True)
class Caller:
    """A verified helpdesk caller: who they are, which roles uc1 recognised."""

    employee_id: str
    roles: frozenset[str]

    def as_json(self) -> dict[str, object]:
        return {"employee_id": self.employee_id, "roles": sorted(self.roles)}


def recognised_roles(scopes: Iterable[str]) -> frozenset[str]:
    """The roles uc1 has a rule for, out of whatever the claim carried."""
    return frozenset(scope for scope in scopes if scope in KNOWN_ROLES)


def grants_replay(roles: Iterable[str]) -> bool:
    """Whether these roles open `GET /sessions/{id}/replay`.

    A pure predicate, so the rule is provable without a request, a database or a
    running app. Additive, and deliberately so: uc1 has one role and no deny
    list, which is the opposite of uc3's composition rule (ADR 0013). If uc1
    ever grows a second role, a caller holding it plus `governance` still
    replays -- that is ordinary RBAC, and the surprise would be the other way.
    """
    return bool(frozenset(roles) & REPLAY_ROLES)


def caller_roles(request: Request) -> frozenset[str]:
    """The recognised roles on this request's verified credentials, or none.

    Returns empty rather than raising when the scope carries no credentials:
    whether the caller is authenticated at all is
    `api.authenticated_employee`'s question, and it answers 401. This function
    only answers "and what may they do", so a missing claim is no roles.
    """
    credentials = request.scope.get("auth")
    if not isinstance(credentials, AuthCredentials):
        return frozenset()
    return recognised_roles(credentials.scopes)


def enforce_replay(caller: Caller) -> Caller:
    """Return `caller` if it may replay a session; otherwise 403.

    403 and never 404, *including for a session id that does not exist*. The
    two status codes have to be indistinguishable to a caller without the role,
    or the endpoint answers "does session X exist?" for anyone who can
    authenticate -- and helpdesk session ids appear in Zammad tickets
    (`ticket_filings.source_key`), so that is a real oracle over a real
    identifier, not a theoretical one. Wiring follows: this runs as a FastAPI
    dependency, which is solved before the path parameter is parsed and long
    before anything is looked up, so there is no ordering in which the lookup
    could leak first.
    """
    if not grants_replay(caller.roles):
        raise HTTPException(403, REPLAY_REFUSED)
    return caller


# --- local development bypass --------------------------------------------------
#
# uc1/P6 asks for "SSO via Entra ID (MSAL; a dev bypass flag for local)", and the
# browser half (`VITE_AUTH_DEV_BYPASS`) is useless on its own: with no server-side
# counterpart a local API answers 401 on `/me` and on replay, so the UI renders an
# error state and a developer reads it as a broken API.
#
# This is a second identity path into an app whose whole auth posture is "fail
# closed", so it is built to uc3's already-reviewed shape rather than a new one
# (`apps/comms_surveillance/auth.py`), and the guards are the point:
#
#   * it is refused unless ENV names a *known* non-production environment -- an
#     unset or misspelt ENV refuses, so the failure mode of a typo is a locked
#     door rather than an open one;
#   * the refusal raises at startup AND on every request. The startup check is the
#     one anybody notices; the per-request check is the one that cannot be skipped
#     by exporting the variable after boot;
#   * the identity it grants is fixed and obviously fake, so a bypassed session
#     cannot be mistaken for a real employee in a turn row or a ticket.
#
# It grants `governance` by default because the thing a developer runs the UI to
# look at is the replay view, and a bypass that withheld the app's only role would
# send them hunting for a bug in the role check.

NON_PROD_ENVS = frozenset({"dev", "local", "test", "ci"})
DEV_BYPASS_IDENTITY = "dev-bypass@example.test"


class DevBypassRefused(RuntimeError):
    """`AUTH__DEV_BYPASS=true` outside a known non-production ENV."""


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _is_known_non_prod(env: str) -> bool:
    return env.strip().lower() in NON_PROD_ENVS


def dev_bypass_requested() -> bool:
    return _flag("AUTH__DEV_BYPASS")


def check_dev_bypass() -> None:
    """Raise if the bypass is switched on anywhere it is not allowed."""
    env = os.environ.get("ENV", "")
    if dev_bypass_requested() and not _is_known_non_prod(env):
        raise DevBypassRefused(
            f"AUTH__DEV_BYPASS=true is refused unless ENV is one of "
            f"{', '.join(sorted(NON_PROD_ENVS))}; got {env or '<unset>'!r}. "
            "Remove the flag or run against real SSO."
        )


def dev_bypass_active() -> bool:
    """Whether this request may use the bypass. Refuses loudly rather than quietly."""
    check_dev_bypass()
    return dev_bypass_requested() and _is_known_non_prod(os.environ.get("ENV", ""))


def dev_bypass_roles() -> frozenset[str]:
    """Roles for the fixed test identity; `AUTH__DEV_BYPASS_ROLES` narrows it.

    An unrecognised name yields no roles rather than falling back to the default,
    so `AUTH__DEV_BYPASS_ROLES=governence` shows a developer the no-role UI --
    which is the honest answer to what they asked for.
    """
    raw = os.environ.get("AUTH__DEV_BYPASS_ROLES", "")
    named = {item.strip() for item in raw.split(",") if item.strip()}
    return frozenset(named & KNOWN_ROLES) if named else REPLAY_ROLES
