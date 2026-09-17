"""The batch-of-vendor-calls loop contract (platform/eval/aio.py).

These tests exist because the bug they cover is invisible to a one-call smoke
test: item 1 always succeeds and item 2 fails with what looks like a network
error. `test_pool_bound_to_first_loop_survives_the_batch` is the regression --
it fails against a judge built on `asyncio.run` per item.
"""

import asyncio
import warnings

import pytest
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
    """A client bound to the first loop must still work on every later call.

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
    assert unused._loop is None

    used = LoopRunner()
    seen: list[asyncio.AbstractEventLoop] = []

    async def record() -> None:
        seen.append(asyncio.get_running_loop())

    used.run(record())
    loop = seen[0]  # held, so the assertion cannot be fooled by a reused address
    assert not loop.is_closed()
    used.close()
    assert loop.is_closed()
    assert used._loop is None
    used.close()
    assert loop.is_closed()


def test_closed_runner_refuses_to_open_a_second_loop() -> None:
    """Reopening would recreate the very defect this class prevents.

    A vendor client's pool stays bound to the first loop, so a quietly-created
    second loop hands the caller `Event loop is closed` again -- from the object
    that was supposed to stop it. Refusing is the honest behaviour.
    """
    runner = LoopRunner()

    async def noop() -> None:
        return None

    runner.run(noop())
    runner.close()
    with pytest.raises(RuntimeError, match="closed"):
        runner.run(noop())


def test_a_refused_run_does_not_leak_its_coroutine() -> None:
    """The refusal must close the coroutine it declined, or Python warns
    'coroutine was never awaited' and the caller sees a confusing second error."""
    runner = LoopRunner()
    runner.close()

    async def noop() -> None:
        return None

    coro = noop()
    with pytest.raises(RuntimeError):
        runner.run(coro)
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        del coro  # a still-pending coroutine would raise RuntimeWarning here


def test_close_cancels_work_the_batch_left_running() -> None:
    """`asyncio.run` cancels pending tasks; a reused loop must do it too, or a
    batch that raised part-way leaves a task pending on a loop being closed."""
    runner = LoopRunner()
    started = asyncio.Event()
    task: list[asyncio.Task[None]] = []

    async def forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    async def spawn() -> None:
        task.append(asyncio.create_task(forever()))
        await started.wait()

    runner.run(spawn())
    assert not task[0].done(), "the task outlives the coroutine that spawned it"
    runner.close()
    assert task[0].cancelled() or task[0].done()


def test_context_manager_closes_on_the_way_out() -> None:
    seen: list[asyncio.AbstractEventLoop] = []

    async def record() -> None:
        seen.append(asyncio.get_running_loop())

    with LoopRunner() as runner:
        runner.run(record())
    assert seen[0].is_closed()


def test_close_batch_ignores_a_callable_without_a_loop() -> None:
    """Stub judges injected by tests are plain functions and own no loop."""

    def stub_judge(source: str, produced: str, language: str) -> str:
        return "fine"

    close_batch(stub_judge)  # must not raise
    close_batch(None)
    assert stub_judge("a", "b", "hi-IN") == "fine", "the stub is untouched"


def test_close_batch_calls_a_carried_close_exactly_once() -> None:
    calls: list[int] = []

    def judge() -> None:
        return None

    judge.close = lambda: calls.append(1)  # type: ignore[attr-defined]
    close_batch(judge)
    assert calls == [1]


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
