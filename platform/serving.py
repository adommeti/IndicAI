"""What every app's FastAPI process needs and none of them should reimplement.

Three things live here because all three apps need identical behaviour and an app
cannot import another app: the `BudgetExceeded` boundary, the `/metrics` mount, and a
`/health` payload that is true.

`fastapi`, `uvicorn` and `prometheus-client` are already `indic-platform` dependencies,
so this adds no new edge to the dependency graph.
"""

from __future__ import annotations

import logging
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from indic_platform.adapters.budget import BudgetExceeded
from indic_platform.config.settings import settings
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

log = logging.getLogger(__name__)

#: Scopes a caller can do something about by waiting. A session cap is not one of them:
#: the session is already over budget and will be until it ends, so telling a client to
#: retry in N seconds would be a lie.
_RETRYABLE = {"day"}


def _retry_after_seconds(scope: str) -> int | None:
    if scope not in _RETRYABLE:
        return None
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((midnight - now).total_seconds()), 1)


async def budget_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    """Turn a spend refusal into 429, not the 500 it used to surface as.

    A spend cap is the system working: the call was refused deliberately, before the
    vendor was touched. 500 tells a client the server is broken and invites exactly the
    retry storm a budget control exists to prevent.

    The response names the scope and nothing else. The exception's own message carries
    INR amounts, and how much of GMO's monthly vendor budget is left is not something an
    end user -- or anyone who can reach the endpoint -- is owed. It is logged instead.
    """
    assert isinstance(exc, BudgetExceeded)
    log.warning("refused by the %s spend cap: %s", exc.scope, exc)
    headers = {}
    retry_after = _retry_after_seconds(exc.scope)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=429,
        content={
            "detail": f"{exc.scope} spend cap reached; this request was not sent to a vendor",
            "scope": exc.scope,
        },
        headers=headers,
    )


def app_version(distribution: str) -> str:
    """The installed version, or the image's git sha, or "unknown" -- never a guess."""
    sha = (os.environ.get("INDICAI_GIT_SHA") or "").strip()
    if sha:
        return sha[:12]
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return "unknown"


def health_payload(distribution: str) -> dict[str, Any]:
    """A liveness answer that is true today and stays true.

    The old payloads hard-coded a build stage (`scaffold`, `P2`, `P6`) that was stale the
    moment the next prompt merged, and a health endpoint that reports a fact nobody
    updates is worse than one that reports nothing. Everything here is read at call time.

    Deliberately cheap: no database, no broker, no vendor. A liveness probe that does I/O
    restarts a healthy container whenever a dependency hiccups.
    """
    return {
        "status": "ok",
        "app": settings.app or "unconfigured",
        "version": app_version(distribution),
    }


def install(app: FastAPI, *, distribution: str) -> None:
    """Add the budget handler and the `/metrics` endpoint to an app.

    `/metrics` is a ROUTE, not a `mount(make_asgi_app())`. A mount owns every path
    beneath it, and uc3 already serves `/metrics/precision` and
    `/metrics/false_negative_estimate` behind a `compliance_lead` dependency -- mounting
    over them answered both with Prometheus text, unauthenticated, to any caller. A route
    matches the exact path and leaves the sub-paths to the app that declared them.

    The endpoint is unauthenticated because a scraper is not a user and Prometheus has no
    credential here; it is reachable only on the compose network, and `obs/metrics.py`
    carries counters and labels (vendor, capability, status) -- never content. Exposing
    the port publicly would need an auth proxy in front.

    Call this BEFORE any `app.mount("/", ...)` for a UI bundle, which does swallow
    everything after it.
    """
    app.add_exception_handler(BudgetExceeded, budget_exceeded_handler)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        # The default REGISTRY is the one `indic_platform.obs.metrics` increments.
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.state.distribution = distribution
