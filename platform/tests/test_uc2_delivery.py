"""Pilot delivery, cohort assignment and the comprehension report (uc2/P5).

The load-bearing tests are `test_cohort_assignment_is_stable_for_an_employee`
(an employee who drifts between arms biases the whole comparison) and
`test_a_small_cell_reports_no_rate` (a pass rate over three attempts reads as a
finding and is not one).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from training_localizer.reports import (
    MIN_CELL,
    Attempt,
    assign_cohort,
    cell,
    comprehension,
    delivery_language,
    load_pilot,
    score_attempt,
    to_markdown,
)

DASHBOARD = Path(__file__).parents[2] / "infra" / "grafana" / "dashboards" / "uc2.json"


@pytest.fixture
def pilot() -> dict[str, Any]:
    return load_pilot()


def attempts(
    n: int, *, cohort: str = "native", language: str = "hi-IN", passing: int | None = None
) -> list[Attempt]:
    passing = n if passing is None else passing
    return [
        Attempt(
            module_id="m",
            language=language,
            employee_id=f"e{i}",
            score=5 if i < passing else 1,
            max_score=5,
            duration_ms=300_000 + i * 1000,
            cohort=cohort,
        )
        for i in range(n)
    ]


# --- cohort assignment --------------------------------------------------------


def test_cohort_assignment_is_stable_for_an_employee(pilot: dict[str, Any]) -> None:
    """An employee who drifts between arms appears in both and biases the result."""
    for employee in (f"emp-{i}@example.test" for i in range(200)):
        first = assign_cohort(employee, pilot)
        assert all(assign_cohort(employee, pilot) == first for _ in range(5))


def test_a_different_pilot_reshuffles(pilot: dict[str, Any]) -> None:
    other = {**pilot, "pilot_id": "uc2-pilot-2027Q1"}
    employees = [f"emp-{i}@example.test" for i in range(300)]
    moved = sum(1 for e in employees if assign_cohort(e, pilot) != assign_cohort(e, other))
    assert moved > 0, "a new pilot must not inherit the old assignment"


def test_the_control_arm_is_roughly_the_configured_fraction(pilot: dict[str, Any]) -> None:
    employees = [f"emp-{i}@example.test" for i in range(4000)]
    share = sum(1 for e in employees if assign_cohort(e, pilot) == "control") / len(employees)
    assert abs(share - pilot["control_fraction"]) < 0.03, share


def test_an_exempt_employee_is_never_in_the_control_arm(pilot: dict[str, Any]) -> None:
    """Some people have a compliance obligation to take it in a language they read."""
    forced = next(
        e
        for e in (f"emp-{i}@example.test" for i in range(500))
        if assign_cohort(e, pilot) == "control"
    )
    exempted = {**pilot, "control_exempt": [forced]}
    assert assign_cohort(forced, exempted) == "native"


def test_the_control_arm_is_served_english(pilot: dict[str, Any]) -> None:
    """That is what makes it a control."""
    for employee in (f"emp-{i}@example.test" for i in range(200)):
        language = delivery_language(employee, "ta-IN", pilot)
        assert language == ("en-IN" if assign_cohort(employee, pilot) == "control" else "ta-IN")


# --- scoring ------------------------------------------------------------------


def test_an_unanswered_item_is_wrong_not_skipped() -> None:
    """Otherwise an employee raises their rate by leaving items blank."""
    items = {1: 0, 2: 1, 3: 2, 4: 0, 5: 3}
    assert score_attempt({1: 0, 2: 1, 3: 2, 4: 0, 5: 3}, items) == (5, 5)
    assert score_attempt({1: 0, 2: 1}, items) == (2, 5)
    assert score_attempt({}, items) == (0, 5)


def test_a_wrong_answer_and_a_missing_one_score_the_same() -> None:
    items = {1: 0, 2: 1}
    assert score_attempt({1: 9}, items) == score_attempt({}, items)


# --- the report ---------------------------------------------------------------


def test_a_small_cell_reports_no_rate(pilot: dict[str, Any]) -> None:
    """A pass rate over three attempts reads as a finding and is not one."""
    small = cell(attempts(MIN_CELL - 1), pilot["pass_mark"])
    assert small["attempts"] == MIN_CELL - 1
    assert small["pass_rate"] is None and small["suppressed"] is True
    assert small["mean_score"] is not None, "the mean is still shown, with its count"

    big = cell(attempts(MIN_CELL), pilot["pass_mark"])
    assert big["pass_rate"] == 1.0 and big["suppressed"] is False


def test_pass_rate_uses_the_configured_mark(pilot: dict[str, Any]) -> None:
    # 3/5 = 0.6 exactly, which is the configured mark, so it passes.
    on_the_mark = [Attempt("m", "hi-IN", f"e{i}", 3, 5, 100, "native") for i in range(MIN_CELL)]
    assert cell(on_the_mark, pilot["pass_mark"])["pass_rate"] == 1.0
    below = [Attempt("m", "hi-IN", f"e{i}", 2, 5, 100, "native") for i in range(MIN_CELL)]
    assert cell(below, pilot["pass_mark"])["pass_rate"] == 0.0


def test_the_report_states_the_lift_the_pilot_exists_to_measure(pilot: dict[str, Any]) -> None:
    report = comprehension(
        attempts(10, cohort="native", passing=9)
        + attempts(10, cohort="control", language="en-IN", passing=5),
        pilot,
    )
    assert report["by_cohort"]["native"]["pass_rate"] == 0.9
    assert report["by_cohort"]["control"]["pass_rate"] == 0.5
    assert report["native_minus_control"] == pytest.approx(0.4)
    assert report["by_language"]["hi-IN"]["attempts"] == 10
    assert report["by_language"]["en-IN"]["attempts"] == 10


def test_no_lift_is_stated_when_an_arm_is_too_small(pilot: dict[str, Any]) -> None:
    report = comprehension(attempts(10) + attempts(2, cohort="control"), pilot)
    assert report["native_minus_control"] is None
    assert "control" in report["suppressed_cells"]


def test_time_on_task_is_the_median_not_the_mean(pilot: dict[str, Any]) -> None:
    """One employee who left the tab open for an hour must not move the number."""
    normal = [Attempt("m", "hi-IN", f"e{i}", 5, 5, 300_000, "native") for i in range(6)]
    with_outlier = [*normal, Attempt("m", "hi-IN", "slow", 5, 5, 3_600_000, "native")]
    assert cell(normal, 0.6)["median_seconds"] == 300.0
    assert cell(with_outlier, 0.6)["median_seconds"] == 300.0


def test_markdown_renders_the_same_numbers(pilot: dict[str, Any]) -> None:
    report = comprehension(
        attempts(10, cohort="native", passing=9)
        + attempts(10, cohort="control", language="en-IN", passing=5),
        pilot,
    )
    text = to_markdown(report)
    assert "# Comprehension — uc2-pilot-2026Q3" in text
    assert "90%" in text and "50%" in text
    assert "+40%" in text
    assert "| native | 10 |" in text


def test_markdown_says_when_it_withheld_a_rate(pilot: dict[str, Any]) -> None:
    text = to_markdown(comprehension(attempts(10) + attempts(2, cohort="control"), pilot))
    assert "Not stated" in text
    assert "Rates withheld" in text and "control" in text


def test_an_empty_pilot_reports_nothing_rather_than_zero(pilot: dict[str, Any]) -> None:
    report = comprehension([], pilot)
    assert report["attempts"] == 0
    assert report["by_cohort"]["native"]["pass_rate"] is None
    assert report["native_minus_control"] is None
    assert "Not stated" in to_markdown(report)


# --- the dashboard ------------------------------------------------------------


def test_the_dashboard_has_the_panels_the_prompt_asks_for() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    titles = [p["title"] for p in dashboard["panels"]]
    assert any("throughput" in t.lower() for t in titles)
    assert any("spend" in t.lower() for t in titles)
    assert dashboard["uid"] and dashboard["title"]


def test_the_spend_panel_reads_adapter_calls_not_a_hand_kept_total() -> None:
    """Spend has to come from the table every vendor call writes, or it will
    drift from what was actually billed."""
    dashboard = json.loads(DASHBOARD.read_text())
    spend = next(p for p in dashboard["panels"] if "spend" in p["title"].lower())
    sql = spend["targets"][0]["rawSql"]
    assert "from adapter_calls" in sql
    assert "cost_inr" in sql and "cost_usd" in sql


def test_the_dashboard_and_the_api_apply_the_same_small_cell_rule() -> None:
    """A dashboard that shows a rate the report withholds is worse than neither."""
    dashboard = json.loads(DASHBOARD.read_text())
    panel = next(p for p in dashboard["panels"] if "Comprehension" in p["title"])
    sql = panel["targets"][0]["rawSql"]
    assert f"count(*) >= {MIN_CELL}" in sql
    assert f">= {load_pilot()['pass_mark']}" in sql


# --- API ----------------------------------------------------------------------


def client(authed: bool = True) -> Any:
    from fastapi.testclient import TestClient
    from training_localizer.api import app, authenticated_owner

    if authed:
        app.dependency_overrides[authenticated_owner] = lambda: "asha@example.test"
    else:
        app.dependency_overrides.clear()
    return TestClient(app)


MODULE = "00000000-0000-4000-8000-000000000042"


def test_delivery_and_attempts_fail_closed_without_sso() -> None:
    api = client(authed=False)
    assert api.get(f"/delivery/{MODULE}").status_code == 401
    assert api.post("/delivery/attempts", json={}).status_code == 401
    assert api.get("/reports/comprehension").status_code == 401


def test_an_attempt_body_that_names_its_own_score_is_rejected() -> None:
    """Scoring is server-side; a client that could post a score could post a pass."""
    api = client()
    try:
        response = api.post(
            "/delivery/attempts",
            json={
                "module_id": MODULE,
                "language": "hi-IN",
                "answers": {"1": 0},
                "duration_ms": 1000,
                "score": 5,
            },
        )
        assert response.status_code == 422
    finally:
        from training_localizer.api import app

        app.dependency_overrides.clear()


def test_an_implausible_duration_is_rejected() -> None:
    api = client()
    try:
        for duration in (-1, 25 * 60 * 60 * 1000):
            response = api.post(
                "/delivery/attempts",
                json={
                    "module_id": MODULE,
                    "language": "hi-IN",
                    "answers": {},
                    "duration_ms": duration,
                },
            )
            assert response.status_code == 422, duration
    finally:
        from training_localizer.api import app

        app.dependency_overrides.clear()


def test_media_only_resolves_uris_this_app_recorded() -> None:
    """Without the artifacts check this endpoint would sign any object in the
    bucket for anyone who could name it."""
    import inspect

    from training_localizer import api

    source = inspect.getsource(api.media)
    assert "select(Artifact).where(Artifact.uri == uri)" in source
    assert 'raise HTTPException(404, "Unknown artifact")' in source
    # The signature must expire; a permanent URL is a leaked artifact.
    assert "expires=timedelta(minutes=30)" in source


def test_media_requires_authentication() -> None:
    api = client(authed=False)
    assert api.get("/media", params={"uri": "s3://b/k"}).status_code == 401


def test_the_delivery_payload_never_carries_the_correct_answer() -> None:
    """The one field that would let an employee pass without watching anything."""
    import inspect

    from training_localizer import api

    source = inspect.getsource(api.delivery)
    quiz_block = source[source.index('"quiz":') :]
    assert "q.answer" not in quiz_block
    assert "q.question" in quiz_block and "q.options" in quiz_block


@pytest.mark.integration
async def test_upload_to_recorded_attempt_end_to_end() -> None:
    """uc2/P5 acceptance: a demo from upload to a quiz attempt recorded.

    Runs in CI's Postgres job. Covers the whole chain without vendors: create a
    module and segments, approve them, produce captions, approve quiz items,
    serve delivery, submit answers, and read the comprehension report back.
    """
    import os
    import uuid as uuidlib

    from indic_platform.db.models import Localization, Module, QuizAttempt, QuizItem, Segment
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from training_localizer.production import captions_task
    from training_localizer.storage import InMemoryStorage

    if "DATABASE_URL" not in os.environ:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")

    pilot = load_pilot()
    module_id = uuidlib.uuid4()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    storage = InMemoryStorage()

    try:
        # Upload.
        async with AsyncSession(engine) as db, db.begin():
            db.add(Module(id=module_id, title="delivery", status="uploaded"))
            await db.flush()
            for i in range(5):
                db.add(
                    Segment(
                        module_id=module_id,
                        seg_id=i + 1,
                        start_ms=i * 5000,
                        end_ms=i * 5000 + 4500,
                        source_text=f"Source {i + 1}.",
                        locked=False,
                    )
                )
                db.add(
                    Localization(
                        module_id=module_id,
                        seg_id=i + 1,
                        language="hi-IN",
                        stage="approved",
                        version=1,
                        text=f"[hi-IN] approved {i + 1}",
                        meta={},
                        created_by="asha@example.test",
                    )
                )
                db.add(
                    QuizItem(
                        module_id=module_id,
                        language="hi-IN",
                        item_id=i + 1,
                        seg_id=i + 1,
                        question=f"Question {i + 1}?",
                        options=["right", "wrong", "wrong", "wrong"],
                        answer=0,
                        rationale="because",
                        approved=True,
                    )
                )

        # Produce captions so the delivery payload has something to point at.
        async with AsyncSession(engine) as db, db.begin():
            await captions_task(db, module_id=module_id, language="hi-IN", storage=storage)

        # Two employees take it: one all-correct, one below the mark.
        for employee, answers in (
            ("alpha@example.test", {i + 1: 0 for i in range(5)}),
            ("beta@example.test", {1: 0, 2: 1, 3: 1, 4: 1, 5: 1}),
        ):
            async with AsyncSession(engine) as db, db.begin():
                items = (
                    await db.scalars(
                        select(QuizItem).where(
                            QuizItem.module_id == module_id, QuizItem.approved.is_(True)
                        )
                    )
                ).all()
                score, max_score = score_attempt(answers, {q.item_id: q.answer for q in items})
                db.add(
                    QuizAttempt(
                        module_id=module_id,
                        language="hi-IN",
                        employee_id=employee,
                        score=score,
                        max_score=max_score,
                        cohort=assign_cohort(employee, pilot),
                        duration_ms=420_000,
                        pilot_id=str(pilot["pilot_id"]),
                    )
                )

        async with AsyncSession(engine) as db:
            rows = (
                await db.scalars(select(QuizAttempt).where(QuizAttempt.module_id == module_id))
            ).all()

        assert len(rows) == 2
        assert {r.score for r in rows} == {5, 1}
        assert all(r.pilot_id == pilot["pilot_id"] for r in rows)
        assert all(r.cohort in ("native", "control") for r in rows)
        assert all(r.duration_ms == 420_000 for r in rows)

        report = comprehension(
            [
                Attempt(
                    module_id=str(r.module_id),
                    language=r.language,
                    employee_id=r.employee_id,
                    score=r.score,
                    max_score=r.max_score,
                    duration_ms=r.duration_ms,
                    cohort=r.cohort,
                )
                for r in rows
            ],
            pilot,
        )
        assert report["attempts"] == 2
        # Two attempts is below MIN_CELL, so no rate is stated — which is the
        # behaviour under test, not a shortcoming of the fixture.
        assert report["by_language"]["hi-IN"]["pass_rate"] is None
        assert report["by_language"]["hi-IN"]["mean_score"] == pytest.approx(0.6)
        assert "Rates withheld" in to_markdown(report)
    finally:
        await engine.dispose()
