"""The Claude adapter must not outlive the event loop its sockets belong to.

`detect` -- the eval's `Transcript -> [Flag]` entry point -- calls `asyncio.run` once
per transcript, so every item runs on a fresh loop and the previous one is closed.
`claude()` used to be an `@lru_cache(maxsize=1)`, which handed every item the adapter
built for the first: `AsyncAnthropic` owns an httpx pool registered with one loop, so
item two raised `RuntimeError: Event loop is closed`, which the SDK re-raised as
`APIConnectionError: Connection error.`

It read as a network fault on a run that had already spent money, and no single-call
test could catch it, because one call is one loop. These tests are the shape that can:
they compare adapter identity ACROSS loops, which is the only place the bug lives.
"""

import asyncio
from collections.abc import Iterator

import pytest
from comms_surveillance import detector


@pytest.fixture(autouse=True)
def fresh_adapter() -> Iterator[None]:
    """Each test starts with no cached adapter and leaves none behind."""
    detector.claude.cache_clear()
    yield
    detector.claude.cache_clear()


def test_a_new_event_loop_gets_a_new_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression. On the old code both runs returned the same object.

    That object's connection pool belonged to the first loop, which `asyncio.run` had
    already closed by the time the second call used it -- so the second live request
    failed with a connection error that had nothing to do with the network.
    """
    monkeypatch.setenv("INDICAI_ANTHROPIC_API_KEY", "not-a-real-key")  # pragma: allowlist secret

    # The adapters themselves, not their ids: CPython reuses an address once the
    # first object is freed, so comparing ids passes or fails depending on whether
    # the garbage collector got there first. Holding both references settles it.
    async def build() -> object:
        return detector.claude()

    first = asyncio.run(build())
    second = asyncio.run(build())
    assert first is not second, (
        "the same adapter was reused on a second event loop; its httpx pool is bound "
        "to the first loop, which asyncio.run has since closed"
    )


def test_one_loop_still_reuses_its_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix must not become "a new client per call".

    Rebuilding every time would open a fresh TLS connection for all three stages of
    every transcript. Within one loop the adapter is still shared.
    """
    monkeypatch.setenv("INDICAI_ANTHROPIC_API_KEY", "not-a-real-key")  # pragma: allowlist secret

    async def build_twice() -> tuple[object, object]:
        return detector.claude(), detector.claude()

    first, second = asyncio.run(build_twice())
    assert first is second


def test_only_one_adapter_is_held_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevents the obvious wrong fix: a dict of adapters keyed by loop.

    A 200-item eval would accumulate 200 clients, each holding sockets against a loop
    that no longer exists. The module keeps one, rebuilt when the loop changes.
    """
    monkeypatch.setenv("INDICAI_ANTHROPIC_API_KEY", "not-a-real-key")  # pragma: allowlist secret

    async def build() -> object:
        return detector.claude()

    held = [asyncio.run(build()) for _ in range(5)]
    assert len({id(a) for a in held}) == 5, "each loop must get its own adapter"
    assert detector._ADAPTER is held[-1], "only the newest adapter is retained"


def test_a_synchronous_caller_still_gets_an_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude()` is called from sync test code and must not raise off-loop.

    `asyncio.get_running_loop()` raises when there is no loop; the loop id falls back
    to 0 rather than propagating, so a synchronous caller keeps working.
    """
    monkeypatch.setenv("INDICAI_ANTHROPIC_API_KEY", "not-a-real-key")  # pragma: allowlist secret
    assert detector.claude() is not None
    assert detector._ADAPTER_LOOP is None, "a synchronous caller has no loop"


def test_cache_clear_is_still_part_of_the_contract() -> None:
    """Tests and callers reset the adapter through it; the lru_cache surface is kept."""
    assert callable(detector.claude.cache_clear)
    detector.claude.cache_clear()
    assert detector._ADAPTER is None
    assert detector._ADAPTER_LOOP is None
