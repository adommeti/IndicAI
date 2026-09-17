"""The batch-of-vendor-calls loop contract (platform/eval/aio.py).

These tests exist because the bug they cover is invisible to a one-call smoke
test: item 1 always succeeds and item 2 fails with what looks like a network
error. `test_pool_bound_to_first_loop_survives_the_batch` is the regression --
it fails against a judge built on `asyncio.run` per item.
"""

import asyncio

from indic_platform.eval.aio import LoopRunner, close_batch


def test_runner_reuses_one_loop_across_calls() -> None:
    runner = LoopRunner()

    async def which_loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    try:
        first = runner.run(which_loop())
        second = runner.run(which_loop())
        # Hold both objects: comparing id() of a collected loop is how this same
        # class of test gave a false pass in uc3 (CPython reuses addresses).
        assert first is second
        assert not first.is_closed()
    finally:
        runner.close()


def test_pool_bound_to_first_loop_survives_the_batch() -> None:
    """A client bound to the first loop must still work on call 90.

    This models what `AsyncAnthropic` does: the connection pool captures the
    running loop the first time it is driven and raises if that loop is later
    closed. Under `asyncio.run` per item this raises on the second call.
    """

    class PoolBoundClient:
        def __init__(self) -> None:
            self.bound: asyncio.AbstractEventLoop | None = None
            self.calls = 0

        async def send(self) -> int:
            running = asyncio.get_running_loop()
            if self.bound is None:
                self.bound = running
            elif self.bound is not running or self.bound.is_closed():
                raise RuntimeError("Event loop is closed")
            self.calls += 1
            return self.calls

        async def aclose(self) -> None:
            return None

    client = PoolBoundClient()
    runner = LoopRunner()
    try:
        results = [runner.run(client.send()) for _ in range(5)]
    finally:
        runner.close()
    assert results == [1, 2, 3, 4, 5]
    assert client.calls == 5


def test_close_is_idempotent_and_safe_when_unused() -> None:
    unused = LoopRunner()
    unused.close()
    unused.close()

    used = LoopRunner()

    async def noop() -> None:
        return None

    used.run(noop())
    used.close()
    used.close()


def test_runner_reopens_after_close() -> None:
    runner = LoopRunner()

    async def noop() -> None:
        return None

    runner.run(noop())
    runner.close()
    runner.run(noop())  # must not raise "Event loop is closed"
    runner.close()


def test_close_batch_ignores_a_callable_without_a_loop() -> None:
    """Stub judges injected by tests are plain functions and own no loop."""

    def stub_judge(source: str, produced: str, language: str) -> str:
        return "fine"

    close_batch(stub_judge)
    close_batch(None)


def test_close_batch_closes_a_callable_that_carries_one() -> None:
    runner = LoopRunner()
    seen: list[asyncio.AbstractEventLoop] = []

    async def record() -> None:
        seen.append(asyncio.get_running_loop())

    def judge() -> None:
        runner.run(record())

    judge.close = runner.close  # type: ignore[attr-defined]
    judge()
    loop = seen[0]
    assert not loop.is_closed()
    close_batch(judge)
    # Hold the loop object so this observes the real one, not an address reused
    # by a later allocation.
    assert loop.is_closed()
