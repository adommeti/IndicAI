"""Spend caps and budget alerts.

Every test uses `InMemoryCounterStore`, which is the same `CounterStore` the Redis backend
implements: the ledger logic under test is the one that runs in production, and no Redis
server is needed.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime

import pytest
from indic_platform.adapters.budget import (
    PREFIX,
    BudgetExceeded,
    InMemoryCounterStore,
    SpendLedger,
    session_scope,
)
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.obs.langfuse import MemorySink
from prometheus_client import REGISTRY

# 2026-03-10T06:00Z: mid-day, mid-month, so a day TTL and a month TTL differ visibly.
START = datetime(2026, 3, 10, 6, 0, tzinfo=UTC).timestamp()


class Clock:
    def __init__(self, start: float = START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def ledger(clock: Clock | None = None, **caps: float) -> SpendLedger:
    tick = clock or Clock()
    settings: dict[str, float] = {
        "monthly_budget_inr": 1_000.0,
        "session_cap_inr": 100.0,
        "day_cap_inr": 500.0,
        "unknown_reserve_inr": 5.0,
        **caps,
    }
    return SpendLedger(InMemoryCounterStore(clock=tick), clock=tick, **settings)


def alerts(threshold: int) -> float:
    return (
        REGISTRY.get_sample_value("adapter_budget_alerts_total", {"threshold": str(threshold)})
        or 0.0
    )


def refusals(scope: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "adapter_budget_refusals_total",
            {"vendor": "sarvam", "capability": "translate", "scope": scope},
        )
        or 0.0
    )


async def test_call_under_the_cap_proceeds_and_is_charged() -> None:
    book = ledger()
    sink = MemorySink()
    runtime = AdapterRuntime("sarvam", "translate", sink=sink, ledger=book, session_id="s-1")

    async def operation() -> str:
        return "ok"

    # 1000 characters of mayura:v1 is INR 2.00.
    assert await runtime.call(operation, model="mayura:v1", units={"characters": 1000}) == "ok"
    assert sink.records[-1]["cost_inr"] == 2.0
    assert await book.spent("session", "s-1") == pytest.approx(2.0)
    assert await book.spent("day") == pytest.approx(2.0)
    assert await book.spent("month") == pytest.approx(2.0)


async def test_call_over_the_cap_is_refused_before_the_operation_runs() -> None:
    book = ledger(session_cap_inr=1.0)
    sink = MemorySink()
    runtime = AdapterRuntime("sarvam", "translate", sink=sink, ledger=book, session_id="s-2")
    ran = 0
    before = refusals("session")

    async def operation() -> str:
        nonlocal ran
        ran += 1
        return "ok"

    with pytest.raises(BudgetExceeded) as raised:
        await runtime.call(operation, model="mayura:v1", units={"characters": 1000})

    assert ran == 0, "the vendor operation must never be awaited once a cap refuses the call"
    assert raised.value.scope == "session"
    assert raised.value.cap_inr == 1.0
    assert raised.value.projected_inr == pytest.approx(2.0)
    assert sink.records == []
    # The refused reservation is rolled back, so the cap is not poisoned by the refusal.
    assert await book.spent("session", "s-2") == 0.0
    assert await book.spent("day") == 0.0
    assert refusals("session") == before + 1


async def test_day_cap_refuses_and_session_scope_carries_the_id() -> None:
    book = ledger(day_cap_inr=3.0)
    runtime = AdapterRuntime("sarvam", "translate", sink=MemorySink(), ledger=book)

    async def operation() -> str:
        return "ok"

    with session_scope("s-3"):
        await runtime.call(operation, model="mayura:v1", units={"characters": 1000})
        assert await book.spent("session", "s-3") == pytest.approx(2.0)
        with pytest.raises(BudgetExceeded) as raised:
            await runtime.call(operation, model="mayura:v1", units={"characters": 1000})
    assert raised.value.scope == "day"
    assert await book.spent("day") == pytest.approx(2.0)


async def test_without_a_session_id_only_day_and_month_apply() -> None:
    book = ledger(session_cap_inr=0.01)
    runtime = AdapterRuntime("sarvam", "translate", sink=MemorySink(), ledger=book)

    async def operation() -> str:
        return "ok"

    # No session id anywhere: the session scope is absent rather than shared, and the call
    # is bounded by the day and month scopes instead.
    assert runtime.session() is None
    await runtime.call(operation, model="mayura:v1", units={"characters": 1000})
    assert await book.spent("day") == pytest.approx(2.0)


async def test_stream_of_unknown_cost_is_refused_when_headroom_is_below_the_floor() -> None:
    book = ledger(day_cap_inr=3.0, unknown_reserve_inr=5.0)
    runtime = AdapterRuntime("sarvam", "stt", sink=MemorySink(), ledger=book)
    opened = 0

    async def generate() -> AsyncIterator[str]:
        nonlocal opened
        opened += 1
        yield "never"

    with pytest.raises(BudgetExceeded) as raised:
        async with aclosing(
            runtime.stream(generate, model="saaras:v3", units={"seconds": 0.0})
        ) as stream:
            async for _ in stream:
                pass
    assert opened == 0
    assert raised.value.scope == "day"
    assert await book.spent("day") == 0.0


async def test_failed_stream_is_charged_for_what_it_spent() -> None:
    book = ledger()
    sink = MemorySink()
    runtime = AdapterRuntime("sarvam", "stt", sink=sink, ledger=book, retry_base=0)
    units = {"seconds": 0.0}

    async def generate() -> AsyncIterator[str]:
        units["seconds"] += 60  # a minute of audio reached Sarvam...
        yield "partial"
        units["seconds"] += 60  # ...and a second one, before the socket died
        raise RuntimeError("stream died")

    with pytest.raises(RuntimeError):
        async with aclosing(runtime.stream(generate, model="saaras:v3", units=units)) as stream:
            async for _ in stream:
                pass

    # saaras:v3 is INR 30/hour, so 120 streamed seconds cost INR 1.00 even though the call
    # ended in an error and the up-front reservation was the INR 5 unknown-cost floor.
    assert sink.records[-1]["status"] == "error"
    assert sink.records[-1]["cost_inr"] == pytest.approx(1.0)
    assert await book.spent("day") == pytest.approx(1.0)
    assert await book.spent("month") == pytest.approx(1.0)


async def test_each_alert_threshold_fires_exactly_once_per_month(
    caplog: pytest.LogCaptureFixture,
) -> None:
    book = ledger(monthly_budget_inr=100.0, session_cap_inr=0.0, day_cap_inr=1_000.0)
    before = {t: alerts(t) for t in (50, 80, 100)}
    caplog.set_level(logging.WARNING, logger="indic_platform.adapters.budget")

    async def charge(amount: float) -> None:
        reservation = await book.reserve(
            amount, session_id="s-alert", vendor="sarvam", capability="tts"
        )
        await book.settle(reservation, amount)

    for _ in range(14):  # 14 x INR 10 = INR 140, past every threshold
        await charge(10.0)

    assert {t: alerts(t) - before[t] for t in (50, 80, 100)} == {50: 1.0, 80: 1.0, 100: 1.0}
    lines = [r.getMessage() for r in caplog.records if "monthly spend crossed" in r.getMessage()]
    assert len(lines) == 3
    assert all("s-alert" not in line for line in lines), "alerts carry no identifiers"
    assert "100.00 INR" in lines[-1] and "sarvam/tts" in lines[-1]


async def test_concurrent_reservations_cannot_both_slip_under_a_cap() -> None:
    book = ledger(day_cap_inr=10.0, session_cap_inr=0.0)
    outcomes = await asyncio.gather(
        *(book.reserve(6.0, vendor="sarvam", capability="tts") for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(o, BudgetExceeded) for o in outcomes) == 1
    # The winner keeps its headroom; the loser's increment is rolled back.
    assert await book.spent("day") == pytest.approx(6.0)


async def test_day_counter_expires_and_the_month_counter_survives() -> None:
    clock = Clock()
    book = ledger(clock)
    store = book.store
    assert isinstance(store, InMemoryCounterStore)

    reservation = await book.reserve(5.0, vendor="sarvam", capability="tts")
    await book.settle(reservation, 5.0)
    assert await book.spent("day") == pytest.approx(5.0)

    clock.advance(19 * 3600)  # past the UTC midnight the day key was scoped to
    assert await store.get(f"{PREFIX}:day:2026-03-10") == 0.0
    assert await book.spent("day") == 0.0
    assert await book.spent("month") == pytest.approx(5.0)

    clock.advance(30 * 86400)  # into April
    assert await store.get(f"{PREFIX}:month:2026-03") == 0.0
    assert await book.spent("month") == 0.0


async def test_disabled_ledger_never_refuses() -> None:
    book = ledger(day_cap_inr=0.5)
    book.enabled = False
    runtime = AdapterRuntime("sarvam", "translate", sink=MemorySink(), ledger=book)

    async def operation() -> str:
        return "ok"

    assert await runtime.call(operation, model="mayura:v1", units={"characters": 10_000}) == "ok"
    assert await book.spent("day") == 0.0


class BrokenStore(InMemoryCounterStore):
    """Stands in for an unreachable Redis."""

    async def incr(self, key: str, amount: float, ttl: int) -> float:
        raise ConnectionError("redis unreachable")

    async def get(self, key: str) -> float:
        raise ConnectionError("redis unreachable")


async def test_unreachable_backend_falls_back_to_in_process_accounting() -> None:
    clock = Clock()
    book = SpendLedger(
        BrokenStore(clock=clock),
        monthly_budget_inr=1_000.0,
        session_cap_inr=0.0,
        day_cap_inr=3.0,
        unknown_reserve_inr=5.0,
        clock=clock,
    )
    runtime = AdapterRuntime("sarvam", "translate", sink=MemorySink(), ledger=book)

    async def operation() -> str:
        return "ok"

    # Accounting degrades to this worker instead of taking the platform down with the ledger,
    # and the cap still bites -- just per process until the backend comes back.
    await runtime.call(operation, model="mayura:v1", units={"characters": 1000})
    assert await book.spent("day") == pytest.approx(2.0)
    with pytest.raises(BudgetExceeded):
        await runtime.call(operation, model="mayura:v1", units={"characters": 1000})


# --------------------------------------------------------------------------------------
# The Redis backend, against a real server.
#
# Everything above runs on `InMemoryCounterStore`, which proves the ledger's arithmetic
# but says nothing about the backend a multi-worker deployment actually shares. Two
# properties only a real server can show, and both are the difference between a cap and
# a decoration:
#
#   * the cap is SHARED -- two workers racing at one cap admit one of them, not both,
#     because the increment and the test are one atomic round trip rather than a
#     read-then-write;
#   * the TTL is set on CREATE only -- a busy key must expire on schedule rather than
#     having its expiry pushed forward by every increment, which would turn a per-day
#     counter into a permanent one and silently stop the day cap from ever resetting.
#
# `integration`, because CI's integration job provisions redis:7.4-alpine on 6380 and
# fails on any skip; the gate deselects it.
# --------------------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_redis_ledger_shares_one_cap_and_expires_on_schedule() -> None:
    import os
    import uuid

    import redis.asyncio as aioredis
    from indic_platform.adapters.budget import RedisCounterStore

    url = os.getenv("REDIS_URL", "redis://localhost:6380/0")
    session = f"s-{uuid.uuid4().hex[:12]}"  # unique: the database is shared
    ledger = SpendLedger(
        RedisCounterStore(url), monthly_budget_inr=1000, session_cap_inr=10, day_cap_inr=1000
    )

    async def reserve_six() -> str:
        try:
            await ledger.reserve(
                6.0, estimated=False, session_id=session, vendor="sarvam", capability="stt"
            )
            return "admitted"
        except BudgetExceeded as exc:
            return f"refused:{exc.scope}"

    outcomes = sorted(await asyncio.gather(reserve_six(), reserve_six()))
    spent = await ledger.spent("session", session_id=session)
    print(
        f"redis ledger: two concurrent Rs 6 reserves at a Rs 10 cap -> {outcomes}, Rs {spent:.2f}"
    )
    assert outcomes == ["admitted", "refused:session"], (
        f"both callers slipped under one cap: {outcomes}"
    )
    assert abs(spent - 6.0) < 1e-6, f"the refused reservation was not rolled back: Rs {spent}"

    client = aioredis.from_url(url)
    try:
        key = f"{PREFIX}:session:{session}"
        before = await client.ttl(key)
        await asyncio.sleep(1.1)
        with session_scope(session):
            await ledger.reserve(
                1.0, estimated=False, session_id=session, vendor="sarvam", capability="stt"
            )
        after = await client.ttl(key)
        print(f"redis ledger: session key ttl {before}s -> {after}s after a second increment")
        assert 0 < after <= before, (
            f"the TTL moved forward ({before} -> {after}): a busy counter would never expire"
        )
        await client.delete(key)
    finally:
        await client.aclose()
