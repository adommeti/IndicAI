"""Per-app spend caps (PRD B8: "caps are set at 2x the estimate for each app").

The ledger used to be one process-wide counter with one set of limits and app-agnostic
keys, so three apps pointed at one Redis shared a single day total: a UC3 sweep of a
night's recordings could consume the headroom UC1 needed to answer a helpdesk call the
next morning, and the refusal would surface in the app that had spent nothing.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from indic_platform.adapters.budget import (
    BudgetExceeded,
    InMemoryCounterStore,
    SpendLedger,
    default_ledger,
    ledger_for,
)
from indic_platform.config import settings as settings_module
from indic_platform.config.settings import (
    APP_BUDGETS,
    AppBudget,
    BudgetSettings,
    Settings,
)

START = datetime(2026, 3, 10, 6, 0, tzinfo=UTC).timestamp()


def ledger(app: str, *, store: InMemoryCounterStore, day_cap: float = 100.0) -> SpendLedger:
    return SpendLedger(
        store,
        monthly_budget_inr=10_000.0,
        session_cap_inr=0.0,
        day_cap_inr=day_cap,
        enabled=True,
        clock=lambda: START,
        app=app,
    )


async def test_one_apps_spend_does_not_consume_anothers_headroom() -> None:
    """The regression, on ONE shared store -- the Redis case, without needing Redis."""
    shared = InMemoryCounterStore(clock=lambda: START)
    uc3 = ledger("uc3", store=shared)
    uc1 = ledger("uc1", store=shared)

    await uc3.reserve(100.0)  # UC3 spends its entire day cap
    with pytest.raises(BudgetExceeded):
        await uc3.reserve(1.0)  # and is correctly refused its next call

    # UC1 has spent nothing, so it must still be admitted.
    await uc1.reserve(100.0)
    assert await uc1.spent("day") == pytest.approx(100.0)
    assert await uc3.spent("day") == pytest.approx(100.0), "UC3's own total is unchanged"


async def test_the_pooled_ledger_still_shares_one_counter() -> None:
    """An unnamed ledger keeps the original keys, so evals and tests pool as before.

    The mirror of the test above: without the app namespace this is the ONLY behaviour,
    and it must survive so nothing that relied on one pooled budget changes underfoot.
    """
    shared = InMemoryCounterStore(clock=lambda: START)
    first, second = ledger("", store=shared), ledger("", store=shared)
    await first.reserve(60.0)
    with pytest.raises(BudgetExceeded) as refused:
        await second.reserve(50.0)
    assert refused.value.scope == "day"
    assert refused.value.spent_inr == pytest.approx(60.0), (
        "the second ledger must see the first one's spend, not start from zero"
    )


async def test_the_key_namespace_is_the_app() -> None:
    assert ledger_for("uc2").prefix == "indic:spend:uc2"
    assert ledger_for("").prefix == "indic:spend", "the pooled namespace is the original one"


async def test_the_alert_once_marker_is_namespaced_per_app() -> None:
    """Each app measures against its OWN budget, so each crosses 50% independently.

    A shared marker would mean the second app to cross a threshold never alerted -- the
    failure mode is silence, which is the one a spend control cannot have. This reaches
    for `_mark_once` directly because that is exactly the shared-state question; the
    end-to-end alert path over `settle` is covered in `test_spend_caps.py`.
    """
    shared = InMemoryCounterStore(clock=lambda: START)
    uc1, uc3 = ledger("uc1", store=shared), ledger("uc3", store=shared)
    stamp = f"{datetime.fromtimestamp(START, UTC):%Y-%m}"
    for led in (uc1, uc3):
        assert await led._mark_once(f"{led.prefix}:alert:{stamp}:50", 3600) is True
    # ...and the same app's second crossing is still suppressed.
    assert await uc1._mark_once(f"{uc1.prefix}:alert:{stamp}:50", 3600) is False


def test_every_app_in_the_plan_has_a_cap_derived_from_its_prd_estimate() -> None:
    budget = BudgetSettings()
    assert set(APP_BUDGETS) == {"uc1", "uc2", "uc3"}
    for app in APP_BUDGETS:
        monthly, session_cap, day_cap = budget.for_app(app)
        assert 0 < day_cap < monthly, f"{app}: a day cap must bound less than a month"
        assert session_cap > 0, f"{app}: an uncapped session is the runaway B8 guards against"
    # UC2 dubs at Rs 40/min, so a 20-minute module is Rs 800 before any Claude call: its
    # session cap must clear that, and the shared Rs 250 default would not have.
    assert budget.for_app("uc2")[1] >= 800
    # UC1 is the cheapest app and must not be handed UC3's much larger allowance.
    assert budget.for_app("uc1")[0] < budget.for_app("uc3")[0]


def test_an_unknown_app_pools_rather_than_silently_capping_at_zero() -> None:
    budget = BudgetSettings()
    assert budget.for_app("nope") == budget.for_app("")
    assert budget.for_app("") == (budget.monthly_inr, budget.session_inr, budget.day_inr)


def test_an_override_replaces_only_the_value_it_names() -> None:
    """Per field: uc3's other two caps stay at ITS defaults, not the shared pool's."""
    budget = BudgetSettings(apps={"uc3": AppBudget(day_inr=7.0)})
    monthly, session_cap, day_cap = budget.for_app("uc3")
    assert day_cap == 7.0
    assert (monthly, session_cap) == (
        APP_BUDGETS["uc3"].monthly_inr,
        APP_BUDGETS["uc3"].session_inr,
    ), "an override must not drop uc3 back to the pooled budget"


def test_an_override_for_one_app_leaves_the_others_alone() -> None:
    """pydantic replaces a dict field wholesale rather than merging into its
    default_factory, so `BUDGET__APPS__UC3__DAY_INR=2000` left `apps` as `{"uc3": ...}`
    and silently put uc1 and uc2 back on the pooled budget this class exists to split --
    including the Rs 250 session cap that would refuse every uc2 dub. Nothing errored;
    the caps were just the wrong ones."""
    budget = BudgetSettings(apps={"uc3": AppBudget(day_inr=2000.0)})
    for app in ("uc1", "uc2"):
        assert budget.for_app(app) != budget.for_app(""), f"{app} fell back to the pooled budget"
    assert budget.for_app("uc2")[1] == APP_BUDGETS["uc2"].session_inr


def test_the_environment_override_path_merges_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape `.env.example` documents, exercised as the environment actually sets it."""
    monkeypatch.setenv("BUDGET__APPS__UC3__DAY_INR", "2000")
    budget = Settings().budget
    assert budget.for_app("uc3")[2] == 2000.0
    assert budget.for_app("uc1") == (6_500.0, 250.0, 650.0)


def test_the_name_the_image_sets_resolves_to_a_real_cap() -> None:
    """The defect this guards: the image exported INDICAI_APP as the PACKAGE name
    (`helpdesk_agent`), while the caps are keyed `uc1|uc2|uc3`, so `for_app()` fell through
    to the pooled defaults and every per-app cap was inert in every container. Nothing
    failed -- uc1 simply ran on a Rs 5,000 day cap instead of Rs 650.
    """
    dockerfile = (
        Path(__file__).resolve().parents[2] / "infra" / "docker" / "Dockerfile.app"
    ).read_text()
    assert "INDICAI_APP=${APP_KEY}" in dockerfile, "the image must export the short app name"
    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text()
    )
    budget = BudgetSettings()
    pooled = budget.for_app("")
    for service, expected in (("uc1-api", "uc1"), ("uc2-api", "uc2"), ("uc3-api", "uc3")):
        key = compose["services"][service]["build"]["args"]["APP_KEY"]
        assert key == expected
        assert budget.for_app(key) != pooled, f"{key} would run on the pooled budget"


def test_the_process_ledger_follows_the_app_the_process_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`INDICAI_APP` is read per call, so a worker configured after import is not stuck."""
    monkeypatch.setattr(settings_module.settings, "app", "uc3")
    assert default_ledger().app == "uc3"
    monkeypatch.setattr(settings_module.settings, "app", "uc1")
    assert default_ledger().app == "uc1"
    assert default_ledger() is ledger_for("uc1"), "per-app instances stay cached"
