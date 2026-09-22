"""The local development bypass, and the guards that make it refuse.

uc2's SSO contract is uc1's: identity comes from a verified claim the middleware in
front of the app puts on `request.scope`, and a bare deployment refuses every
request. That is right for a deployment and unusable on a laptop, where nothing
stands in front of the app: before this module the reviewer console opened on a
local stack and every route answered 401.

This is the server half of the switch the other two apps already have
(`comms_surveillance.auth`, `helpdesk_agent.roles`) and it is built to their
already-reviewed shape rather than a new one -- the guards are the point:

  * it is refused unless ENV names a *known* non-production environment. An unset
    or misspelt ENV refuses, so the failure mode of a typo is a locked door;
  * the refusal raises at import AND on every request. The import-time check is the
    one anybody notices; the per-request check is the one that cannot be skipped by
    exporting the variable after boot;
  * the identity it grants is fixed and obviously fake, so a bypassed session cannot
    be mistaken for a real training owner in a `created_by` column or an approval.

Copied rather than imported from the other apps because apps do not import each
other (CLAUDE.md); the copies are kept identical on purpose and a divergence between
them is a defect.
"""

import os

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
