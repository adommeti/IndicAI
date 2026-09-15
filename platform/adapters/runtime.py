import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TypeVar

from indic_platform.adapters.budget import (
    Reservation,
    SpendLedger,
    current_session_id,
    default_ledger,
)
from indic_platform.adapters.ratelimit import SHARED_BUCKET, Limiter
from indic_platform.obs.langfuse import Sink, cost, default_sink
from indic_platform.obs.metrics import CALLS, DEGRADED, LATENCY

T = TypeVar("T")


def status_code(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )


class CircuitOpen(RuntimeError):
    pass


class AdapterRuntime:
    def __init__(
        self,
        vendor: str,
        capability: str,
        *,
        sink: Sink | None = None,
        limiter: Limiter = SHARED_BUCKET,
        timeout: float = 30,
        retry_base: float = 0.5,
        failure_threshold: int = 5,
        recovery_seconds: float = 30,
        ledger: SpendLedger | None = None,
        session_id: str | None = None,
    ) -> None:
        self.vendor, self.capability = vendor, capability
        self.sink, self.limiter = sink, limiter
        self.timeout, self.retry_base = timeout, retry_base
        self.failure_threshold, self.recovery_seconds = failure_threshold, recovery_seconds
        self.failures = 0
        self.opened_at: float | None = None
        self.probing = False
        self._ledger = ledger
        #: Spend is charged to this session when set; otherwise to whatever
        #: `budget.session_scope` the calling task is inside, and to no session at all when
        #: neither is present (the day and month scopes still apply).
        self.session_id = session_id

    @property
    def ledger(self) -> SpendLedger:
        return self._ledger if self._ledger is not None else default_ledger()

    def session(self) -> str | None:
        return self.session_id or current_session_id()

    async def reserve(self, projected_inr: float, *, estimated: bool) -> Reservation:
        """Take budget headroom before the vendor is touched; raises `BudgetExceeded`."""
        return await self.ledger.reserve(
            projected_inr,
            estimated=estimated,
            session_id=self.session(),
            vendor=self.vendor,
            capability=self.capability,
        )

    @property
    def degraded(self) -> bool:
        return self.opened_at is not None

    @property
    def degraded_mode(self) -> str | None:
        if not self.degraded:
            return None
        return {"stt": "chat_only", "tts": "text_only", "llm": "ticket_template_or_queue"}.get(
            self.capability, "queue"
        )

    def enter(self) -> None:
        if self.opened_at is not None:
            if time.monotonic() - self.opened_at < self.recovery_seconds or self.probing:
                raise CircuitOpen(self.degraded_mode)
            self.probing = True

    def finish(self, success: bool, unhealthy: bool = True) -> None:
        self.probing = False
        if success:
            self.failures, self.opened_at = 0, None
        elif unhealthy:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                if self.opened_at is None:
                    DEGRADED.labels(self.vendor, self.capability).inc()
                self.opened_at = time.monotonic()

    def record(
        self,
        model: str,
        units: dict[str, float],
        started: float,
        status: str,
        attempts: int,
        prompt_version: str | None = None,
    ) -> tuple[float, float]:
        inr, usd = cost(model, units)
        elapsed = time.monotonic() - started
        CALLS.labels(self.vendor, self.capability, status).inc()
        LATENCY.labels(self.vendor, self.capability).observe(elapsed)
        (self.sink or default_sink()).emit(
            {
                "vendor": self.vendor,
                "capability": self.capability,
                "model": model,
                "prompt_version": prompt_version,
                "latency_ms": elapsed * 1000,
                "units": dict(units),
                "cost_inr": inr,
                "cost_usd": usd,
                "status": status,
                "attempts": attempts,
            }
        )
        return inr, usd

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        model: str,
        units: dict[str, float] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - wrapper enforces asyncio.timeout
        prompt_version: str | None = None,
    ) -> T:
        cost(model, {})  # Validate pricing before incurring spend.
        # Units are given up front here, so the cost is projected exactly and the call is
        # refused before the operation runs if it would cross a cap.
        projected, _ = cost(model, units or {})
        reservation = await self.reserve(projected, estimated=False)
        started, attempts, status = time.monotonic(), 0, "error"
        billed: dict[str, float] = {}
        admitted = False
        try:
            self.enter()
            admitted = True
            for attempt in range(3):
                attempts += 1
                await self.limiter.acquire()
                try:
                    async with asyncio.timeout(timeout if timeout is not None else self.timeout):
                        result = await operation()
                    billed = units or {}
                    self.finish(True)
                    status = "ok"
                    return result
                except Exception as exc:
                    code = status_code(exc)
                    transient = code == 429 or (isinstance(code, int) and 500 <= code <= 599)
                    if not transient or attempt == 2:
                        self.finish(False, transient or isinstance(exc, TimeoutError))
                        raise
                    await asyncio.sleep(self.retry_base * 2**attempt * random.uniform(0.5, 1.5))
            raise AssertionError("unreachable")
        finally:
            if admitted:
                self.probing = False
            inr, _ = self.record(model, billed, started, status, attempts, prompt_version)
            await self.ledger.settle(reservation, inr)

    async def stream(
        self,
        factory: Callable[[], AsyncIterator[T]],
        *,
        model: str,
        units: dict[str, float],
        prompt_version: str | None = None,
    ) -> AsyncIterator[T]:
        cost(model, {})
        # `units` is filled in as the stream runs (a streaming STT bills on audio duration
        # the caller cannot know up front), so the value here is a lower bound, not a cost.
        # The call is admitted only when a floor of headroom is free and is charged for
        # what it really spent afterwards -- including a stream that failed mid-flight.
        projected, _ = cost(model, units)
        reservation = await self.reserve(projected, estimated=True)
        started, status, attempts = time.monotonic(), "error", 0
        emitted = False
        admitted = False
        try:
            self.enter()
            admitted = True
            for attempt in range(3):
                attempts += 1
                await self.limiter.acquire()
                iterator = factory()
                try:
                    while True:
                        try:
                            value = await asyncio.wait_for(anext(iterator), self.timeout)
                        except StopAsyncIteration:
                            self.finish(True)
                            status = "ok"
                            return
                        emitted = True
                        yield value
                except Exception as exc:
                    code = status_code(exc)
                    transient = code == 429 or (isinstance(code, int) and 500 <= code <= 599)
                    if emitted or not transient or attempt == 2:
                        self.finish(False, transient or isinstance(exc, TimeoutError))
                        raise
                    await asyncio.sleep(self.retry_base * 2**attempt * random.uniform(0.5, 1.5))
                finally:
                    close = getattr(iterator, "aclose", None)
                    if close:
                        await close()
        finally:
            if admitted:
                self.probing = False
            inr, _ = self.record(model, units, started, status, attempts, prompt_version)
            await self.ledger.settle(reservation, inr)
