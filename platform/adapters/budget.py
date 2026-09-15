"""Spend accounting and caps for every vendor call.

All amounts are **INR**. INR is the currency both vendor legs share: `pricing.yaml` prices
Sarvam natively in INR and `indic_platform.obs.langfuse.cost` converts the USD-priced
Anthropic models with `fx_inr_per_usd`, returning `(inr, usd)` for both. Holding the ledger
in one currency is what makes a single budget meaningful across vendors.

Three scopes are tracked:

* ``session`` -- one conversation/call; **refuses** when the cap is reached.
* ``day``     -- one UTC day across the deployment; **refuses** when the cap is reached.
* ``month``   -- one UTC month; this is the *budget*, not a cap. It never refuses; it is the
  denominator for the 50/80/100% alerts.

Storage is a small counter store: Redis when ``REDIS_URL`` is configured, otherwise an
in-process dictionary. Both implement the same `CounterStore` protocol and the ledger logic
above them is identical, so the unit tests exercise the real enforcement path.

Failure policy: if the Redis store errors, the ledger falls back to the in-process store for a
cooldown and increments ``adapter_budget_degraded_total``. Accounting then holds per worker
rather than per deployment, which is weaker than intended but keeps a budget control from
becoming an availability incident. It is a deliberate fail-open on the *ledger backend* only;
a reachable ledger over its cap always refuses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, Protocol

from indic_platform.config.settings import settings
from indic_platform.obs.metrics import (
    BUDGET_ALERTS,
    BUDGET_DEGRADED,
    BUDGET_REFUSALS,
    BUDGET_SPEND,
)

log = logging.getLogger(__name__)

#: Percentages of the monthly budget that raise an alert, once each per month.
ALERT_THRESHOLDS = (50, 80, 100)

#: A per-session counter is meaningless once the session is long over; a day is generous.
SESSION_TTL_SECONDS = 86_400

PREFIX = "indic:spend"


class BudgetExceeded(RuntimeError):
    """Raised *before* a vendor call that a spend cap will not admit."""

    def __init__(self, scope: str, cap_inr: float, spent_inr: float, projected_inr: float) -> None:
        # Message carries amounts and the scope name only: no session id, no content.
        super().__init__(
            f"{scope} spend cap reached: {spent_inr:.2f} INR spent of {cap_inr:.2f} INR, "
            f"next call projected at {projected_inr:.2f} INR"
        )
        self.scope = scope
        self.cap_inr = cap_inr
        self.spent_inr = spent_inr
        self.projected_inr = projected_inr


class CounterStore(Protocol):
    """Atomic float counters with create-time expiry."""

    async def incr(self, key: str, amount: float, ttl: int) -> float:
        """Add ``amount`` and return the new total, setting ``ttl`` only when key is created."""

    async def get(self, key: str) -> float: ...

    async def mark_once(self, key: str, ttl: int) -> bool:
        """Set ``key`` if absent; True only for the caller that created it."""


class InMemoryCounterStore:
    """Process-local counters. Used by tests and as the fallback when Redis is unreachable."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._values: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    def _live(self, key: str) -> tuple[float, float] | None:
        entry = self._values.get(key)
        if entry is None:
            return None
        if entry[1] <= self._clock():
            del self._values[key]
            return None
        return entry

    async def incr(self, key: str, amount: float, ttl: int) -> float:
        async with self._lock:
            entry = self._live(key)
            total = (entry[0] if entry else 0.0) + amount
            expires = entry[1] if entry else self._clock() + ttl
            self._values[key] = (total, expires)
            return total

    async def get(self, key: str) -> float:
        async with self._lock:
            entry = self._live(key)
            return entry[0] if entry else 0.0

    async def mark_once(self, key: str, ttl: int) -> bool:
        async with self._lock:
            if self._live(key) is not None:
                return False
            self._values[key] = (1.0, self._clock() + ttl)
            return True


class RedisCounterStore:
    """Shared counters across workers. INCRBYFLOAT is atomic; the TTL is set on creation only."""

    SCRIPT = """
local total = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
if redis.call('TTL', KEYS[1]) < 0 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return total
"""

    def __init__(self, url: str) -> None:
        self.url = url
        self._redis: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _client(self) -> Any:
        # A redis-py pool belongs to the loop that created it; Celery workers and tests each
        # run their own, so rebind when the running loop changes.
        loop = asyncio.get_running_loop()
        if self._redis is None or self._loop is not loop:
            from redis.asyncio import Redis

            self._redis = Redis.from_url(self.url)
            self._loop = loop
        return self._redis

    async def incr(self, key: str, amount: float, ttl: int) -> float:
        return float(await self._client().eval(self.SCRIPT, 1, key, amount, ttl))

    async def get(self, key: str) -> float:
        value = await self._client().get(key)
        return float(value) if value is not None else 0.0

    async def mark_once(self, key: str, ttl: int) -> bool:
        return bool(await self._client().set(key, 1, nx=True, ex=ttl))


@dataclass(frozen=True)
class _ScopeKey:
    scope: str
    key: str
    ttl: int
    cap_inr: float | None


@dataclass(frozen=True)
class Reservation:
    """Headroom taken before a call; reconciled against the real cost by `SpendLedger.settle`."""

    amount_inr: float = 0.0
    scopes: tuple[_ScopeKey, ...] = ()
    month_key: _ScopeKey | None = None
    vendor: str = ""
    capability: str = ""


_SESSION: ContextVar[str | None] = ContextVar("indic_spend_session", default=None)


def current_session_id() -> str | None:
    """The session the current task charges to, or None if no scope has been entered."""
    return _SESSION.get()


@contextmanager
def session_scope(session_id: str | None) -> Iterator[None]:
    """Charge every adapter call made inside this block to ``session_id``.

    Explicit and task-local: nothing outside the block is affected, and a runtime constructed
    with its own ``session_id`` keeps that one.
    """
    token = _SESSION.set(session_id)
    try:
        yield
    finally:
        _SESSION.reset(token)


def _end_of_day(now: datetime) -> int:
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((nxt - now).total_seconds()), 1)


def _end_of_month(now: datetime) -> int:
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    nxt = now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(int((nxt - now).total_seconds()), 1)


@dataclass
class SpendLedger:
    """Enforces the session and day caps and alerts against the monthly budget. Amounts in INR."""

    store: CounterStore
    monthly_budget_inr: float = 0.0
    session_cap_inr: float = 0.0
    day_cap_inr: float = 0.0
    unknown_reserve_inr: float = 0.0
    enabled: bool = True
    fallback_seconds: float = 30.0
    clock: Callable[[], float] = time.time
    _fallback: InMemoryCounterStore = field(init=False, repr=False)
    _degraded_until: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._fallback = InMemoryCounterStore(clock=self.clock)

    # -- storage, with a cooldown fallback when the shared backend misbehaves ---------------
    def _active(self) -> CounterStore:
        if self.clock() < self._degraded_until:
            return self._fallback
        return self.store

    def _degrade(self) -> None:
        self._degraded_until = self.clock() + self.fallback_seconds
        BUDGET_DEGRADED.inc()
        log.warning(
            "spend ledger backend unavailable; accounting falls back in-process for %.0fs",
            self.fallback_seconds,
        )

    async def _incr(self, key: str, amount: float, ttl: int) -> float:
        store = self._active()
        try:
            return await store.incr(key, amount, ttl)
        except Exception:
            if store is self._fallback:
                raise
            self._degrade()
            return await self._fallback.incr(key, amount, ttl)

    async def _mark_once(self, key: str, ttl: int) -> bool:
        store = self._active()
        try:
            return await store.mark_once(key, ttl)
        except Exception:
            if store is self._fallback:
                raise
            self._degrade()
            return await self._fallback.mark_once(key, ttl)

    # -- scopes -----------------------------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _month_key(self, now: datetime | None = None) -> _ScopeKey:
        now = now or self._now()
        return _ScopeKey("month", f"{PREFIX}:month:{now:%Y-%m}", _end_of_month(now), None)

    def _scopes(self, session_id: str | None) -> tuple[_ScopeKey, ...]:
        """The scopes a call is charged to, refusing ones first and the month always last.

        A scope whose cap is 0 is not tracked -- 0 means "no cap for this scope". With no
        session id the session scope is simply absent: a long-lived worker must not share one
        anonymous session counter, and day plus month still bound the spend.
        """
        now = self._now()
        scopes: list[_ScopeKey] = []
        if session_id and self.session_cap_inr > 0:
            scopes.append(
                _ScopeKey(
                    "session",
                    f"{PREFIX}:session:{session_id}",
                    SESSION_TTL_SECONDS,
                    self.session_cap_inr,
                )
            )
        if self.day_cap_inr > 0:
            scopes.append(
                _ScopeKey("day", f"{PREFIX}:day:{now:%Y-%m-%d}", _end_of_day(now), self.day_cap_inr)
            )
        scopes.append(self._month_key(now))
        return tuple(scopes)

    # -- public API -------------------------------------------------------------------------
    async def spent(self, scope: str, session_id: str | None = None) -> float:
        """Current INR spend for ``scope`` ("session", "day" or "month")."""
        if scope == "session" and not session_id:
            return 0.0
        now = self._now()
        key = {
            "session": f"{PREFIX}:session:{session_id}",
            "day": f"{PREFIX}:day:{now:%Y-%m-%d}",
            "month": f"{PREFIX}:month:{now:%Y-%m}",
        }[scope]
        store = self._active()
        try:
            return await store.get(key)
        except Exception:
            if store is self._fallback:
                raise
            self._degrade()
            return await self._fallback.get(key)

    async def reserve(
        self,
        projected_inr: float,
        *,
        estimated: bool = False,
        session_id: str | None = None,
        vendor: str = "",
        capability: str = "",
    ) -> Reservation:
        """Take headroom for a call, or raise `BudgetExceeded` before it happens.

        ``estimated`` marks a call whose units are only known afterwards (a stream billed on
        audio duration). Such a call reserves ``unknown_reserve_inr`` as a floor, so it is
        refused when less than that remains, and is reconciled to the real cost in `settle`.
        """
        amount = max(projected_inr, 0.0)
        if estimated:
            amount = max(amount, self.unknown_reserve_inr)
        if not self.enabled:
            return Reservation(vendor=vendor, capability=capability)
        scopes = self._scopes(session_id)
        applied: list[_ScopeKey] = []
        for scope in scopes:
            total = await self._incr(scope.key, amount, scope.ttl)
            applied.append(scope)
            # A zero-cost call adds nothing to spend, so a cap already breached does not
            # refuse it; only a call that would itself consume headroom is refused.
            if scope.cap_inr is not None and amount > 0 and total > scope.cap_inr:
                for done in applied:
                    await self._incr(done.key, -amount, done.ttl)
                BUDGET_REFUSALS.labels(vendor, capability, scope.scope).inc()
                raise BudgetExceeded(scope.scope, scope.cap_inr, total - amount, amount)
        return Reservation(amount, tuple(applied), scopes[-1], vendor, capability)

    async def settle(self, reservation: Reservation, actual_inr: float) -> None:
        """Charge what the call really cost and alert if the month crossed a threshold.

        A call that already happened is always charged, including one that failed after
        streaming part of its audio: the reservation is reconciled by the difference.
        """
        if not self.enabled or reservation.month_key is None:
            return
        delta = actual_inr - reservation.amount_inr
        total = 0.0
        for scope in reservation.scopes:
            # The month scope is always incremented, delta or not, because its running total
            # is what the alert thresholds are measured against.
            if delta or scope.scope == "month":
                total = await self._incr(scope.key, delta, scope.ttl)
        if actual_inr > 0:
            BUDGET_SPEND.labels(reservation.vendor, reservation.capability).inc(actual_inr)
            await self._alert(total, reservation.vendor, reservation.capability)

    async def _alert(self, month_total: float, vendor: str, capability: str) -> None:
        if self.monthly_budget_inr <= 0:
            return
        share = month_total / self.monthly_budget_inr * 100
        stamp = f"{self._now():%Y-%m}"
        month_ttl = _end_of_month(self._now())
        for threshold in ALERT_THRESHOLDS:
            if share < threshold:
                continue
            # One marker per threshold per month: the crossing alerts, the calls after it
            # do not. The marker expires with the month it belongs to.
            if not await self._mark_once(f"{PREFIX}:alert:{stamp}:{threshold}", month_ttl):
                continue
            BUDGET_ALERTS.labels(str(threshold)).inc()
            log.warning(
                "monthly spend crossed %d%% of budget: %.2f of %.2f INR (%.1f%%), "
                "triggered by %s/%s",
                threshold,
                month_total,
                self.monthly_budget_inr,
                share,
                vendor or "unknown",
                capability or "unknown",
            )


@lru_cache(maxsize=1)
def default_ledger() -> SpendLedger:
    """The process ledger: Redis-backed when REDIS_URL is configured, in-memory otherwise."""
    budget = settings.budget
    store: CounterStore = (
        RedisCounterStore(settings.redis_url) if settings.redis_url else InMemoryCounterStore()
    )
    return SpendLedger(
        store,
        monthly_budget_inr=budget.monthly_inr,
        session_cap_inr=budget.session_inr,
        day_cap_inr=budget.day_inr,
        unknown_reserve_inr=budget.unknown_reserve_inr,
        enabled=budget.enabled,
    )
