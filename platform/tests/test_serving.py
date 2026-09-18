"""The three things every app's FastAPI process gets from `indic_platform.serving`."""

import pytest
from fastapi import FastAPI
from indic_platform import serving
from indic_platform.adapters.budget import BudgetExceeded
from indic_platform.config import settings as settings_module
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.testclient import TestClient


def build(scope: str = "day") -> TestClient:
    app = FastAPI()
    serving.install(app, distribution="indic-platform")

    @app.get("/spend")
    def spend() -> dict[str, str]:
        raise BudgetExceeded(scope, 100.0, 100.0, 5.0)

    @app.get("/metrics/precision")
    def precision() -> dict[str, str]:
        return {"owner": "the app, not the scraper"}

    return TestClient(app, raise_server_exceptions=False)


def test_a_spend_refusal_is_429_not_500() -> None:
    """A cap doing its job is not a server fault, and 500 invites the retry storm the
    cap exists to prevent."""
    response = build().get("/spend")
    assert response.status_code == 429
    assert response.json()["scope"] == "day"


def test_the_response_does_not_leak_what_the_budget_is() -> None:
    """`BudgetExceeded`'s message carries INR amounts. How much of the monthly vendor
    budget is left is not owed to whoever can reach the endpoint."""
    body = build().get("/spend").text
    for amount in ("100", "5.0", "INR"):
        assert amount not in body, f"the response leaked {amount!r}"


def test_a_day_cap_says_when_to_come_back_and_a_session_cap_does_not() -> None:
    """Waiting clears a day cap. It never clears a session cap, so promising a retry
    window there would be a lie the client would act on."""
    day = build("day").get("/spend")
    assert 0 < int(day.headers["Retry-After"]) <= 86_400
    assert "Retry-After" not in build("session").get("/spend").headers


def test_metrics_is_served_and_does_not_swallow_the_apps_own_subpaths() -> None:
    """The regression: mounting `make_asgi_app()` at "/metrics" took ownership of every
    path beneath it, so uc3's `compliance_lead`-only `/metrics/precision` answered
    Prometheus text to anyone who asked. A route matches one exact path."""
    client = build()
    scrape = client.get("/metrics")
    assert scrape.status_code == 200
    assert CONTENT_TYPE_LATEST.split(";")[0] in scrape.headers["content-type"]
    own = client.get("/metrics/precision")
    assert own.status_code == 200
    assert own.json() == {"owner": "the app, not the scraper"}


def test_metrics_exposes_the_registry_the_platform_actually_increments() -> None:
    from indic_platform.obs.metrics import BUDGET_REFUSALS

    BUDGET_REFUSALS.labels("probe-vendor", "probe-capability", "day").inc()
    body = build().get("/metrics").text
    assert "adapter_budget_refusals_total" in body
    assert "probe-vendor" in body


def test_health_reports_the_app_it_is_rather_than_a_build_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`{"stage": "scaffold"}` was wrong within one prompt of being written, and nothing
    made it wrong loudly. Every field here is read at request time."""
    monkeypatch.setattr(settings_module.settings, "app", "uc3")
    payload = serving.health_payload("indic-platform")
    assert payload["status"] == "ok"
    assert payload["app"] == "uc3"
    assert "stage" not in payload


def test_an_unconfigured_process_says_so_instead_of_claiming_an_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings_module.settings, "app", "")
    assert serving.health_payload("indic-platform")["app"] == "unconfigured"


def test_the_image_sha_wins_over_the_package_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a container the package version is a constant; the sha says what actually shipped."""
    # Not a credential: a stand-in commit sha. pragma: allowlist secret
    monkeypatch.setenv("INDICAI_GIT_SHA", "0123456789abcdef0123")  # pragma: allowlist secret
    assert serving.app_version("indic-platform") == "0123456789ab"
    monkeypatch.delenv("INDICAI_GIT_SHA")
    assert serving.app_version("indic-platform") == "0.1.0"


def test_an_uninstalled_distribution_is_unknown_not_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INDICAI_GIT_SHA", raising=False)
    assert serving.app_version("not-a-real-distribution") == "unknown"
