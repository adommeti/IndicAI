"""uc2's local development bypass, and the ways it must refuse.

The reviewer console had no server-side bypass at all: with the local stack's defaults
uc1 and uc3 opened and uc2 answered 401 on every route, which is how a demo path can
look finished and not be. The shape is uc1's and uc3's on purpose
(`test_uc1_dev_bypass.py`), and the interesting tests are the refusals.
"""

import importlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from training_localizer import auth


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient ENV or flag leaks in from the developer's shell or .env.stack."""
    for key in ("ENV", "AUTH__DEV_BYPASS", "AUTH__DEV_BYPASS_ROLES"):
        monkeypatch.delenv(key, raising=False)
    yield


def client() -> Any:
    from training_localizer.api import app

    return TestClient(app)


def bare_request() -> Request:
    """A request with no SSO middleware behind it: no `user`, no `auth` in scope."""
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


# --- refusing ------------------------------------------------------------------


@pytest.mark.parametrize("env", ["prod", "production", "", "Prod ", "staging", "dv", "devv"])
def test_the_bypass_is_refused_outside_a_known_non_prod_env(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset and misspelt refuse too; the error names what IS allowed."""
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    with pytest.raises(auth.DevBypassRefused) as caught:
        auth.check_dev_bypass()
    assert env.strip() or "<unset>" in str(caught.value)
    assert "ci, dev, local, test" in str(caught.value)


def test_a_request_cannot_use_a_bypass_the_environment_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-request check raises rather than returning False, and the dependency
    turns that into a 500 that names the cause -- not a 401 that looks like an outage."""
    from training_localizer.api import authenticated_owner

    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    with pytest.raises(auth.DevBypassRefused):
        auth.dev_bypass_active()
    with pytest.raises(HTTPException) as caught:
        authenticated_owner(bare_request())
    assert caught.value.status_code == 500
    assert "misconfigured" in caught.value.detail


def test_the_app_refuses_to_import_with_the_flag_set_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured deployment fails to start, which is the check people notice."""
    import training_localizer.api as api_module

    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    with pytest.raises(auth.DevBypassRefused):
        importlib.reload(api_module)
    # Leave the module importable for everything after this test.
    monkeypatch.delenv("AUTH__DEV_BYPASS")
    importlib.reload(api_module)


def test_a_non_prod_env_without_the_flag_still_requires_real_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Being in dev is not consent. The flag has to be asked for."""
    from training_localizer.api import authenticated_owner

    monkeypatch.setenv("ENV", "dev")
    assert auth.dev_bypass_active() is False
    with pytest.raises(HTTPException) as caught:
        authenticated_owner(bare_request())
    assert caught.value.status_code == 401
    assert client().get("/modules/00000000-0000-0000-0000-000000000000/status").status_code == 401


def test_the_flag_without_an_env_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The likeliest real misconfiguration: the flag copied into a deployment."""
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    with pytest.raises(auth.DevBypassRefused):
        auth.dev_bypass_active()


# --- admitting -----------------------------------------------------------------


def test_the_bypass_admits_a_fixed_fake_identity_in_a_non_prod_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity is the same obviously-fake subject the other two apps mint, so a
    bypassed session cannot be mistaken for a real training owner in `created_by`."""
    from training_localizer.api import authenticated_owner

    monkeypatch.setenv("ENV", "test")
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    assert authenticated_owner(bare_request()) == auth.DEV_BYPASS_IDENTITY
    assert auth.DEV_BYPASS_IDENTITY.endswith("@example.test")


def test_the_bypass_gets_a_caller_past_the_route_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the app, not only the function: with the bypass the upload route reaches
    body validation (422 on an empty body) instead of stopping at the auth dependency
    (401). Same route, same request, the only difference is the flag -- and no
    database is needed to prove it."""
    monkeypatch.setenv("ENV", "test")
    assert client().post("/modules", json={}).status_code == 401
    monkeypatch.setenv("AUTH__DEV_BYPASS", "true")
    assert client().post("/modules", json={}).status_code == 422


def test_the_allow_list_is_the_one_the_seeder_uses() -> None:
    """A stack where the console opens without SSO is exactly the set of stacks that
    may hold demonstration rows."""
    from training_localizer import demo_seed

    assert demo_seed.NON_PROD_ENVS is auth.NON_PROD_ENVS
    assert auth.NON_PROD_ENVS == frozenset({"dev", "local", "test", "ci"})
