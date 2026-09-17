"""One event loop for a synchronous runner that makes many vendor calls.

A vendor adapter builds its async HTTP client once, and that client's connection
pool binds to whichever event loop first drives it. ``asyncio.run`` creates a
loop, runs one coroutine and then closes it, so a runner that calls
``asyncio.run`` once per golden item hands item 2 a pool whose transports belong
to a loop that no longer exists.

The failure does not look like a lifecycle error. The SDK catches the underlying
``RuntimeError: Event loop is closed`` and re-raises it as
``anthropic.APIConnectionError: Connection error.``, which reads like a network
fault. The uc3 detector wore the same disguise and was diagnosed as one before
commit ``511ab0f`` fixed it. Item 1 always succeeds, which is why a smoke test of
a single call never catches this.

``LoopRunner`` keeps one loop open for a whole batch, so one adapter and one pool
serve every item. That is also the cheaper shape: TLS handshakes and prompt-cache
warmth survive between calls instead of being discarded 90 times.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class LoopRunner:
    """Drive coroutines from synchronous code on a single, reused event loop.

    One-shot by design: once closed it refuses to run again. Quietly opening a
    second loop would recreate the exact defect this class exists to prevent,
    because the adapter's pool is still bound to the first one -- the caller
    would get `Event loop is closed` again, from the object that promised not to.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` to completion, creating the loop on first use."""
        if self._closed:
            coro.close()  # never leave an un-awaited coroutine behind
            raise RuntimeError("LoopRunner is closed; build a new one for a new batch")
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            # `asyncio.run` sets this, and code reaching `get_event_loop()` from
            # sync context would otherwise see a different loop than the one its
            # work is running on.
            asyncio.set_event_loop(self._loop)
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        """Shut the loop down. Safe to call twice, and safe to call unused."""
        loop, self._loop = self._loop, None
        self._closed = True
        if loop is None or loop.is_closed():
            return
        try:
            # `asyncio.run` does all three of these; doing fewer strands work the
            # batch started. `platform/adapters/sarvam_stt.py` both spawns tasks
            # (`create_task`) and uses `asyncio.to_thread`, so a batch that raised
            # part-way can leave a task pending and an executor thread running.
            _cancel_pending(loop)
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def __enter__(self) -> LoopRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel whatever the batch left running, then let it unwind."""
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    if not pending:
        return
    for task in pending:
        task.cancel()
    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def close_batch(obj: object) -> None:
    """Close a callable that carries a ``LoopRunner``, ignoring ones that do not.

    Judges and transcribers are plain callables by protocol (`Judge` is a
    ``Callable``), so the loop they own travels as an attribute rather than as a
    type. A stub judge injected by a test has no loop and must not be required to
    grow one.

    This releases the batch's **loop**, not the vendor client that ran on it:
    `platform/adapters/claude.py` owns an `AsyncAnthropic` with no shutdown hook,
    so its httpx pool is reclaimed by garbage collection afterwards. Tests assert
    that the production factories attach a `close`
    (`test_claude_judge_attaches_its_loop_for_closing`), because a silent no-op
    here would otherwise hide the wiring being dropped.
    """
    close = getattr(obj, "close", None)
    if callable(close):
        close()
