"""The Celery-side boundary for a spend refusal.

A `BudgetExceeded` reaching Celery unhandled is logged as an ERROR with a traceback and
left in the FAILED state, which is what a bug looks like. It is not a bug: the ledger
refused the call deliberately, before the vendor was touched. The cost of that confusion
is real -- an operator triaging a red task at 02:00 goes looking for a broken worker.

What a refused task must NOT do is carry on. Every uc2 pipeline chain is built from
immutable `.si()` signatures, so a task that swallowed the refusal and returned a
"degraded" value would let the next stage run against work that was never done --
`translate` over segments `adapt` never adapted. Chains continue on success and stop on
anything else, so this records the refusal and then stops.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Task
from celery.exceptions import Ignore
from indic_platform.adapters.budget import BudgetExceeded
from indic_platform.obs.metrics import TASK_BUDGET_STOPS

log = logging.getLogger(__name__)

#: Celery state for "stopped on purpose, nothing is broken", written to the result
#: backend with the scope before the task bows out. A custom state rather than FAILURE so
#: dashboards and alerts can tell a refusal from a defect.
#:
#: What is guaranteed either way is that the task ends neither SUCCESS nor FAILURE: the
#: chain stops and nothing logs a traceback. The final state string is not guaranteed --
#: under `task_always_eager` Celery reports IGNORED and never surfaces this meta -- so
#: alert on `task_budget_stops_total`, which is recorded unconditionally, rather than on
#: the state name.
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class BudgetAwareTask(Task):
    """Base task that turns a spend refusal into a recorded stop, not a crash."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().__call__(*args, **kwargs)
        except BudgetExceeded as exc:
            TASK_BUDGET_STOPS.labels(self.name or "unknown", exc.scope).inc()
            # The amounts go to the log, which is operator-facing. The task state is
            # readable by anything that can poll a task id, so it carries the scope only.
            log.warning("task %s stopped by the %s spend cap: %s", self.name, exc.scope, exc)
            self.update_state(state=BUDGET_EXCEEDED, meta={"scope": exc.scope})
            # `Ignore` leaves the state just set in place. Without it Celery would
            # overwrite it with FAILURE and log the traceback this class exists to avoid.
            raise Ignore() from exc
