"""A spend refusal inside a Celery task stops the chain without looking like a crash."""

import pytest
from celery import Celery
from celery.exceptions import Ignore
from indic_platform.adapters.budget import BudgetExceeded
from indic_platform.tasks import BudgetAwareTask
from prometheus_client import REGISTRY


def stops(task: str, scope: str) -> float:
    return (
        REGISTRY.get_sample_value("task_budget_stops_total", {"task": task, "scope": scope}) or 0.0
    )


@pytest.fixture
def celery_app() -> Celery:
    app = Celery("test", broker="memory://", backend="cache+memory://")
    app.conf.update(task_always_eager=True, task_eager_propagates=False)
    return app


def test_a_refused_task_is_not_a_failed_task(celery_app: Celery) -> None:
    """FAILURE plus a traceback is what a bug looks like, and sends an operator hunting
    for a broken worker at 02:00. This one was refused on purpose.

    Asserted as "neither SUCCESS nor FAILURE" rather than as the exact string, because
    that is what this test can actually prove: under `task_always_eager` Celery reports
    IGNORED and does not surface the `BUDGET_EXCEEDED` meta written just before. The
    custom state is still written for a real worker's backend -- see `tasks.py` -- but
    an eager test is not evidence of it, and pinning it here would assert a fiction.
    """

    @celery_app.task(name="probe.refused", base=BudgetAwareTask, bind=True)
    def refused(self: BudgetAwareTask) -> str:
        raise BudgetExceeded("day", 100.0, 100.0, 5.0)

    result = refused.apply()
    assert result.state != "FAILURE", "a deliberate refusal must not read as a crash"
    assert result.state != "SUCCESS", "and must not let a chain continue"


def test_the_refusal_is_counted_with_the_task_and_scope_that_caused_it(
    celery_app: Celery,
) -> None:
    @celery_app.task(name="probe.counted", base=BudgetAwareTask, bind=True)
    def counted(self: BudgetAwareTask) -> str:
        raise BudgetExceeded("session", 10.0, 10.0, 1.0)

    before = stops("probe.counted", "session")
    counted.apply()
    assert stops("probe.counted", "session") == before + 1


def test_a_refused_task_does_not_return_a_value_a_chain_would_build_on(
    celery_app: Celery,
) -> None:
    """The uc2 pipelines are chains of immutable `.si()` signatures, so a task that
    swallowed the refusal and returned a degraded value would let the next stage run
    against work that was never done -- translate over segments adapt never adapted.
    `Ignore` is what keeps the chain from continuing."""

    @celery_app.task(name="probe.chained", base=BudgetAwareTask, bind=True)
    def chained(self: BudgetAwareTask) -> str:
        raise BudgetExceeded("day", 1.0, 1.0, 1.0)

    result = chained.apply()
    assert result.state != "SUCCESS", "success is what lets the next link run"
    assert isinstance(result.result, Ignore) or result.result is None


def test_an_ordinary_failure_is_still_an_ordinary_failure(celery_app: Celery) -> None:
    """The boundary must catch a spend refusal and nothing else: a real bug that stopped
    looking like one would be far worse than the noise this class removes."""

    @celery_app.task(name="probe.broken", base=BudgetAwareTask, bind=True)
    def broken(self: BudgetAwareTask) -> str:
        raise ValueError("a genuine defect")

    result = broken.apply()
    assert result.state == "FAILURE"
    assert isinstance(result.result, ValueError)


def test_a_task_that_succeeds_is_untouched(celery_app: Celery) -> None:
    @celery_app.task(name="probe.fine", base=BudgetAwareTask, bind=True)
    def fine(self: BudgetAwareTask) -> str:
        return "done"

    result = fine.apply()
    assert result.state == "SUCCESS" and result.result == "done"


def test_every_registered_task_binds_the_boundary() -> None:
    """Asserted over the REGISTRY, not the options dict.

    The options-dict version of this test passed while `uc2.localize` and `uc2.produce`
    were declared with a bare `@celery_app.task(name=...)` and inherited nothing: it
    proved the dict was right, not that every task used it. Any task added later without
    the shared options fails here.
    """
    from comms_surveillance.ingest import celery_app as uc3
    from helpdesk_agent.ticketing import celery_app as uc1
    from training_localizer.pipeline import celery_app as uc2

    for prefix, app in (("uc1", uc1), ("uc2", uc2), ("uc3", uc3)):
        app.loader.import_default_modules()
        for name, task in app.tasks.items():
            if not name.startswith(f"{prefix}."):
                continue
            assert isinstance(task, BudgetAwareTask), f"{name} bypasses the spend boundary"
