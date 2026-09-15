"""uc1's local development bypass, and the four ways it must refuse.

uc1/P6 asks for "a dev bypass flag for local". A bypass is a second identity path
into an app whose entire auth posture is "fail closed", so the interesting tests are
not that it works — they are that it refuses, and that the refusal is loud.

The shape is uc3's (`apps/comms_surveillance/auth.py`), deliberately: that one has
been through a security review, and a second hand-rolled bypass with subtly different
guards is how one of them ends up wrong.
"""

import importlib
import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpdesk_agent import roles


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient ENV or flag leaks in from the developer's shell or .env.stack."""
    for key in ("ENV", "AUTH__DEV_BYPASS", "AUTH__DEV_BYPASS_ROLES"):
        monkeypatch.delenv(key, raising=False)
    yield


def client() -> Any:
    from helpdesk_agent.api import app

    return TestClient(app)


# --- refusing ------------------------------------------------------------------


@pytest.mark.parametrize("env", ["prod", "production", "", "Prod ", "staging", "dv", "devv"])
def test_the_bypass_is_refused_outside_a_known_non_prod_env(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset and misspelt refuse too.

    This is the property that matters most. A guard that only checked `env != "prod"`
    would admit a fake employee on an unset ENV and on every typo — and the failure
    mode of a typo must be a locked door, not an open one. `"dv"` and `"devv"` are in
    the list for exactly that reason.
    """
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")

    with pytest.raises(roles.DevBypassRefused) as caught:
        roles.check_dev_bypass()
    assert env.strip() or "<unset>" in str(caught.value)
    assert "ci, dev, local, test" in str(caught.value), "the error must name what IS allowed"


def test_a_request_cannot_use_a_bypass_the_environment_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-request check raises rather than returning False.

    Returning False would degrade a misconfigured production deployment to "401 for
    everyone", which looks like an outage and gets fixed by turning something else
    off. Raising names the actual cause.
    """
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    with pytest.raises(roles.DevBypassRefused):
        roles.dev_bypass_active()


def test_the_app_refuses_to_import_with_the_flag_set_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured deployment fails to start, which is the check people notice."""
    # Imported BEFORE the flag is set: the module-level check fires on execution, so a
    # first import under ENV=prod would raise here rather than inside `pytest.raises`,
    # and the test would error instead of proving anything.
    import helpdesk_agent.api as api_module

    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")

    with pytest.raises(roles.DevBypassRefused):
        importlib.reload(api_module)

    # Leave the module importable for everything after this test.
    monkeypatch.delenv("AUTH__DEV_BYPASS")
    importlib.reload(api_module)


def test_a_non_prod_env_without_the_flag_still_requires_real_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Being in dev is not consent. The flag has to be asked for."""
    monkeypatch.setenv("ENV", "dev")
    assert roles.dev_bypass_active() is False
    assert client().get("/me").status_code == 401


def test_the_flag_without_an_env_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The likeliest real misconfiguration: the flag copied into a deployment."""
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    with pytest.raises(roles.DevBypassRefused):
        roles.dev_bypass_active()


# --- admitting -----------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "local", "test", "ci", "DEV", " local "])
def test_the_bypass_admits_a_fixed_fake_identity_in_a_known_dev_env(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")

    body = client().get("/me").json()
    assert body["employee_id"] == roles.DEV_BYPASS_IDENTITY
    assert "example.test" in body["employee_id"], (
        "the bypass identity must be obviously fake, so a bypassed session cannot be "
        "mistaken for a real employee in a turn row or a filed ticket"
    )
    assert body["roles"] == ["governance"]


def test_the_bypass_grants_replay_so_the_local_ui_can_render_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not 403. A bypass that withheld uc1's only role sends a developer bug-hunting."""
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    # 404, not 403: the role was granted and the session genuinely does not exist.
    assert client().get("/sessions/11111111-1111-4111-8111-111111111111/replay").status_code == 404


def test_narrowing_the_bypass_roles_narrows_what_it_can_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`AUTH__DEV_BYPASS_ROLES=` with an unknown name yields none, not the default.

    Falling back to the default would show a developer who asked for the no-role view
    the full-access one, which is the wrong direction for a mistake to resolve in.
    """
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    monkeypatch.setenv("AUTH__DEV_BYPASS_ROLES", "governence")  # deliberate typo

    assert roles.dev_bypass_roles() == frozenset()
    assert client().get("/me").json()["roles"] == []
    assert client().get("/sessions/11111111-1111-4111-8111-111111111111/replay").status_code == 403


def test_the_bypass_never_reaches_uc3(monkeypatch: pytest.MonkeyPatch) -> None:
    """uc1's flag is uc1's. The two apps' bypasses share a variable name, not a grant.

    `AUTH__DEV_BYPASS` is read by both, which is fine — but uc1's roles come from
    uc1's `KNOWN_ROLES`, so a uc3 role name asked for here yields nothing rather than
    silently importing uc3's vocabulary (ADR 0015).
    """
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    monkeypatch.setenv("AUTH__DEV_BYPASS_ROLES", "compliance_lead,compliance_reviewer")

    assert roles.dev_bypass_roles() == frozenset()
    assert "comms_surveillance" not in os.path.abspath(roles.__file__)
