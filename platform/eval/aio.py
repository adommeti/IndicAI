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
    """Drive coroutines from synchronous code on a single, reused event loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        """Run ``coro`` to completion, creating the loop on first use."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    def close(self) -> None:
        """Shut the loop down. Safe to call twice, and safe to call unused."""
        loop, self._loop = self._loop, None
        if loop is None or loop.is_closed():
            return
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def close_batch(obj: object) -> None:
    """Close a callable that carries a ``LoopRunner``, ignoring ones that do not.

    Judges and transcribers are plain callables by protocol (`Judge` is a
    ``Callable``), so the loop they own travels as an attribute rather than as a
    type. A stub judge injected by a test has no loop and must not be required to
    grow one.
    """
    close = getattr(obj, "close", None)
    if callable(close):
        close()
