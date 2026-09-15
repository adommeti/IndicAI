"""uc1/P7 F4: prove the redaction hook runs before every vendor call and before a log.

`redact("my number is 9876543210") == "my number is [PHONE]"` proves the *function*
works. It proves nothing about the control, which is that every path out of this
process passes through that function first. Delete `self.redact(...)` from
`SarvamTTS.speak` and a test of the function still passes; the employee's phone number
is on Sarvam's wire.

So nothing here calls `redact` and inspects its return value. Instead:

*Capture the wire.* Every vendor-facing adapter is driven through a fake transport --
`httpx.MockTransport` for the REST paths, a fake socket for the websocket ones -- that
records the exact bytes the SDK was about to send: URL, headers and body. The assertion
is over those bytes, so it holds whatever the adapter does internally.

*Prove the capture is load-bearing.* Each path is driven twice, once with the platform
redactor and once with an identity redactor standing in for a deleted hook. The first
run must show no fixture PII on the wire and the second must show it, which is what
makes the first assertion mean something: a fixture that never reached the wire would
pass the clean run for the wrong reason, and this catches that.

*Enumerate the paths rather than list them.* :func:`send_paths` discovers every public
async method on every adapter class that owns a vendor client, and
:func:`test_every_vendor_send_path_is_classified` fails unless each one is either
covered above or exempted here with a written reason. A new adapter method is a test
failure until somebody says which it is -- the point being that the dangerous case is
the send path nobody remembered to add to a list.

*Say what leaks.* :func:`test_redaction_gaps_are_asserted_not_wished_away` asserts the
patterns `redact` misses, including a real Indian mobile number grouped 4-3-3, which
reaches the vendor intact. Those assertions describe today's behaviour. If someone
widens the patterns the assertions fail, which is the moment to delete the matching
entry from `docs/build/BLOCKERS.md` -- a gap that is only mentioned in prose is a gap
that gets fixed and stays documented as open, or stays open and gets documented as
fixed.

No network, no credentials, no vendor spend: every client here is a mock transport.
"""

import asyncio
import base64
import importlib
import inspect
import json
import logging
import pkgutil
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import indic_platform.adapters as adapters_package
import pytest
from anthropic import AsyncAnthropic
from indic_platform.adapters import embeddings as embeddings_module
from indic_platform.adapters.claude import Claude
from indic_platform.adapters.embeddings import TEIEmbedder
from indic_platform.adapters.runtime import AdapterRuntime
from indic_platform.adapters.sarvam_common import SarvamAdapter
from indic_platform.adapters.sarvam_translate import SarvamTranslate
from indic_platform.adapters.sarvam_tts import SarvamTTS
from indic_platform.obs.langfuse import MemorySink
from indic_platform.security.redact import redact
from loguru import logger
from pydantic import BaseModel
from sarvamai import AsyncSarvamAI

# --------------------------------------------------------------------------------------
# Fixtures. A phone number and an email, as the prompt asks, plus an Aadhaar-shaped id,
# plus three things the hook is expected to miss -- carried in the same string so no
# path can be "clean" by never having seen the hard cases.
# --------------------------------------------------------------------------------------

PHONE = "9876543210"
EMAIL = "asha.rao@example.com"
NATIONAL_ID = "1234 5678 9012"
# A ten-digit Indian mobile written 4-3-3 -- the way people actually type one into a
# helpdesk widget. This USED to survive `redact`, which knew only the bare run, 3-3-4
# and 5-5, so the commonest spelling was the one reaching both vendors intact. Found by
# this file and fixed in uc1/P7 by widening the pattern; it stays in the probe so a
# regression puts it back on a wire in front of an assertion.
GROUPED_PHONE = "9876 543 210"
EMPLOYEE_ID = "EMP-48213"
EMPLOYEE_NAME = "Asha Rao"

PROBE = (
    f"{EMPLOYEE_NAME} ({EMPLOYEE_ID}) ka number {PHONE} hai, email {EMAIL}, "
    f"ID {NATIONAL_ID}, alternate {GROUPED_PHONE}."
)
# What must never appear on a wire or in a log.
SECRETS = (PHONE, EMAIL, NATIONAL_ID, GROUPED_PHONE)
# What still does appear. Asserted, not hoped away. These are not pattern-shaped --
# an employee id and a personal name are identifiers a regex cannot recognise -- so
# unlike the 4-3-3 mobile they are not a bug with a fix, they are the documented limit
# of what a pattern hook can do. `docs/security/uc1-review.md` records them under T2.
KNOWN_LEAKS = (EMPLOYEE_ID, EMPLOYEE_NAME)

IDENTITY: Callable[[str], str] = lambda text: text  # noqa: E731 - a deleted hook's stand-in


def wire(request: httpx.Request | httpx2.Request) -> bytes:
    """Everything this request would put on the wire, minus the credential."""
    headers = "\n".join(
        f"{k}: {v}"
        for k, v in sorted(request.headers.items())
        if k.lower() not in {"authorization", "api-subscription-key", "x-api-key"}
    )
    return f"{request.method} {request.url}\n{headers}\n".encode() + request.content


def unredacted(payloads: list[bytes], needles: tuple[str, ...]) -> list[str]:
    blob = b"\n".join(payloads)
    return [n for n in needles if n.encode() in blob or n.encode("unicode_escape") in blob]


# --------------------------------------------------------------------------------------
# Drivers. Each one builds a real adapter over a fake transport, sends PROBE through one
# public method, and returns the captured request bytes. `redactor` is the adapter's
# redaction policy: None means the platform default.
# --------------------------------------------------------------------------------------

Redactor = Callable[[str], str] | None
Driver = Callable[[Redactor], Awaitable[list[bytes]]]


def sarvam_handler(payloads: list[bytes]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(wire(request))
        path = request.url.path
        if path.endswith("/translate"):
            body: dict[str, Any] = {"translated_text": "ok", "source_language_code": "en-IN"}
        elif path.endswith("/transliterate"):
            body = {"transliterated_text": "ok", "source_language_code": "hi-IN"}
        else:
            body = {"audios": [base64.b64encode(b"RIFF").decode()]}
        return httpx.Response(200, json=body)

    return handle


@asynccontextmanager
async def sarvam(redactor: Redactor) -> AsyncIterator[tuple[AsyncSarvamAI, list[bytes]]]:
    payloads: list[bytes] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(sarvam_handler(payloads))) as http:
        yield AsyncSarvamAI(api_subscription_key="mock", httpx_client=http), payloads


def sarvam_kwargs(redactor: Redactor) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"runtime": AdapterRuntime("sarvam", "translate", sink=MemorySink())}
    if redactor is not None:
        kwargs["redactor"] = redactor
    return kwargs


async def drive_translate(redactor: Redactor) -> list[bytes]:
    async with sarvam(redactor) as (sdk, payloads):
        await SarvamTranslate(client=sdk, **sarvam_kwargs(redactor)).translate(
            PROBE, target="hi-IN"
        )
    return payloads


async def drive_transliterate(redactor: Redactor) -> list[bytes]:
    async with sarvam(redactor) as (sdk, payloads):
        await SarvamTranslate(client=sdk, **sarvam_kwargs(redactor)).transliterate(
            PROBE, source="hi-IN"
        )
    return payloads


async def drive_speak(redactor: Redactor) -> list[bytes]:
    async with sarvam(redactor) as (sdk, payloads):
        kwargs = sarvam_kwargs(redactor) | {
            "runtime": AdapterRuntime("sarvam", "tts", sink=MemorySink())
        }
        await SarvamTTS(client=sdk, **kwargs).speak(PROBE, language="hi-IN")
    return payloads


async def drive_tts_stream(redactor: Redactor) -> list[bytes]:
    """The websocket path: the "wire" is whatever text is handed to `socket.convert`."""
    payloads: list[bytes] = []

    class Socket:
        async def configure(self, **kwargs: Any) -> None:
            payloads.append(json.dumps(kwargs, ensure_ascii=False).encode())

        async def convert(self, text: str) -> None:
            payloads.append(text.encode())

        async def flush(self) -> None:
            return None

        async def recv(self) -> Any:
            return SimpleNamespace(
                type="event", data=SimpleNamespace(event_type="final", audio=None)
            )

    @asynccontextmanager
    async def connect(**kwargs: Any) -> AsyncIterator[Socket]:
        payloads.append(json.dumps({k: str(v) for k, v in kwargs.items()}).encode())
        yield Socket()

    async def one_chunk() -> AsyncIterator[str]:
        yield PROBE

    sdk = SimpleNamespace(text_to_speech_streaming=SimpleNamespace(connect=connect))
    kwargs = sarvam_kwargs(redactor) | {
        "runtime": AdapterRuntime("sarvam", "tts", sink=MemorySink())
    }
    adapter = SarvamTTS(client=sdk, **kwargs)
    async for _ in adapter.stream(one_chunk(), language="hi-IN", voice="priya"):
        pass
    return payloads


class Answer(BaseModel):
    answer: str


ANTHROPIC_MESSAGE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-4-5",
    "content": [{"type": "text", "text": '{"answer":"ok"}'}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 2},
}

SSE_EVENTS = [
    ("message_start", {"type": "message_start", "message": {**ANTHROPIC_MESSAGE, "content": []}}),
    (
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 2},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
]


def claude_adapter(
    redactor: Redactor, handle: Callable[[httpx2.Request], httpx2.Response], http: Any
) -> Claude:
    kwargs: dict[str, Any] = {
        "client": AsyncAnthropic(api_key="mock", http_client=http, max_retries=0),
        "runtime": AdapterRuntime("anthropic", "llm", sink=MemorySink(), retry_base=0),
    }
    if redactor is not None:
        kwargs["redactor"] = redactor
    return Claude(**kwargs)


async def drive_structured(redactor: Redactor) -> list[bytes]:
    payloads: list[bytes] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        payloads.append(wire(request))
        return httpx2.Response(200, json=ANTHROPIC_MESSAGE)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        # The system prompt is redacted too, so it carries the probe as well: an operator
        # pasting a ticket into a system prompt is exactly the accident this covers.
        await claude_adapter(redactor, handle, http).structured(
            system=f"Return JSON. Context: {PROBE}",
            user=PROBE,
            schema=Answer,
            model="claude-haiku-4-5",
        )
    return payloads


async def drive_stream_text(redactor: Redactor) -> list[bytes]:
    payloads: list[bytes] = []
    body = "".join(f"event: {name}\ndata: {json.dumps(p)}\n\n" for name, p in SSE_EVENTS)

    def handle(request: httpx2.Request) -> httpx2.Response:
        payloads.append(wire(request))
        return httpx2.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        adapter = claude_adapter(redactor, handle, http)
        stream = adapter.stream_text(
            system=f"Be brief. Context: {PROBE}",
            messages=[
                {"role": "user", "content": PROBE},
                {"role": "assistant", "content": "ok"},
                {"role": "user", "content": PROBE},
            ],
            model="claude-haiku-4-5",
        )
        async for _ in stream:
            pass
    return payloads


@contextmanager
def embedder_redactor(redactor: Redactor) -> Iterator[None]:
    """`TEIEmbedder` calls the module-level `redact`; there is no policy to inject.

    That asymmetry is itself worth knowing: `Claude` and `SarvamAdapter` take a
    `redactor` because PRD E9 needs one of them turned off, and the embedder does not
    because nothing is allowed to turn it off. Standing in for a deleted hook therefore
    needs a patch here rather than a constructor argument.
    """
    if redactor is None:
        yield
        return
    original = embeddings_module.redact
    embeddings_module.redact = redactor  # type: ignore[assignment]
    try:
        yield
    finally:
        embeddings_module.redact = original  # type: ignore[assignment]


async def drive_embed(redactor: Redactor) -> list[bytes]:
    payloads: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(wire(request))
        if request.url.path.endswith("/embed_sparse"):
            return httpx.Response(200, json=[{"1": 0.5}])
        return httpx.Response(200, json=[[0.01] * 1024])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        embedder = TEIEmbedder(
            "http://tei.invalid",
            "http://sparse.invalid",
            client=http,
            runtime=AdapterRuntime("tei", "embedding", sink=MemorySink()),
        )
        with embedder_redactor(redactor):
            await embedder.embed([PROBE])
    return payloads


# qualname -> driver. The key is what `send_paths()` discovers, so a typo here shows up
# as an unclassified path rather than as a quietly missing test.
COVERED: dict[str, Driver] = {
    "SarvamTranslate.translate": drive_translate,
    "SarvamTranslate.transliterate": drive_transliterate,
    "SarvamTTS.speak": drive_speak,
    "SarvamTTS.stream": drive_tts_stream,
    "Claude.structured": drive_structured,
    "Claude.stream_text": drive_stream_text,
    "TEIEmbedder.embed": drive_embed,
}

# Send paths that carry no employee text, each with the reason it needs no hook. A
# reason, not a bare name: "exempt" with no argument is how a text path gets exempted.
EXEMPT: dict[str, str] = {
    "SarvamAdapter.request": (
        "Transport helper. It forwards kwargs a concrete method already redacted; it "
        "never sees text of its own, and giving it its own hook would double-redact."
    ),
    "SarvamSTT.stream": "Audio frames in, transcript out. Nothing textual is uploaded.",
    "SarvamSTT.batch": (
        "Uploads an audio file and an idempotency hash. `redact.py` says so in its own "
        "first line: audio is not transcribed by this hook."
    ),
    "SarvamDubbing.submit": "Uploads media plus language codes and a voice id.",
    "SarvamDubbing.status": "Sends a job id.",
    "SarvamDubbing.fetch": "Sends a job id.",
    "TEIEmbedder.check": "Two GETs for /info; no body.",
    "TEIEmbedder.close": "Closes the HTTP client.",
    "QdrantVectorStore.ensure_collection": "Creates the local collection; no text.",
    "QdrantVectorStore.search": (
        "Self-hosted Qdrant, not a vendor. The query text it does send goes out through "
        "`TEIEmbedder.embed`, which is covered above."
    ),
    "QdrantVectorStore.upsert_article": (
        "Self-hosted Qdrant. KB article text, not employee utterances, and "
        "`helpdesk_agent.ingest` redacts it before it gets here."
    ),
}


def send_paths() -> dict[str, Any]:
    """Every public async method on an adapter class that owns a vendor client.

    "Owns a client" is the structural test for "can reach a vendor": an adapter either
    keeps its SDK or HTTP client on `self.client` or accepts one as `client=` so a test
    can inject a transport, and both signals count. The things that do neither -- the
    runtime, the limiter, the spend ledger -- cannot send anything on their own.
    Discovery walks the package, so a new adapter module is picked up without anyone
    remembering to add it here.
    """
    found: dict[str, Any] = {}
    for info in pkgutil.iter_modules(adapters_package.__path__):
        module = importlib.import_module(f"{adapters_package.__name__}.{info.name}")
        for cls in vars(module).values():
            if not inspect.isclass(cls) or cls.__module__ != module.__name__:
                continue
            if issubclass(cls, BaseModel) or issubclass(cls, BaseException):
                continue
            owners = [
                base
                for base in cls.__mro__
                if base.__module__.startswith(adapters_package.__name__)
            ]
            holds_client = any("self.client" in inspect.getsource(base) for base in owners)
            takes_client = "client" in inspect.signature(cls.__init__).parameters
            if not (holds_client or takes_client):
                continue
            for name in dir(cls):
                if name.startswith("_"):
                    continue
                attribute = inspect.getattr_static(cls, name, None)
                function = attribute.__func__ if isinstance(attribute, staticmethod) else attribute
                if not (
                    inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function)
                ):
                    continue
                found[function.__qualname__] = function
    return found


# --------------------------------------------------------------------------------------
# The wire.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(COVERED))
async def test_no_fixture_pii_reaches_a_vendor_on_any_send_path(path: str) -> None:
    """The control, stated over bytes: nothing identifying survives to the transport."""
    payloads = await COVERED[path](None)
    assert payloads, f"{path} sent nothing; the capture proves nothing"
    assert not unredacted(payloads, SECRETS), f"{path} put employee PII on the wire"
    assert b"[PHONE]" in b"\n".join(payloads) and b"[EMAIL]" in b"\n".join(payloads), (
        f"{path} sent no redaction markers, so the probe never reached the transport "
        "and the clean result above means nothing"
    )


@pytest.mark.parametrize("path", sorted(COVERED))
async def test_each_send_path_would_notice_a_deleted_redaction_hook(path: str) -> None:
    """Drive the same path with the hook neutralized; the PII must reappear.

    This is the half that makes the previous test a control rather than a coincidence.
    An identity redactor is what `self.redact(...)` becoming `text` looks like from
    outside, and if the assertion above cannot see that change, it could not have seen
    a real deletion either.
    """
    payloads = await COVERED[path](IDENTITY)
    leaked = unredacted(payloads, SECRETS)
    assert set(leaked) == set(SECRETS), (
        f"{path} did not carry the probe to the transport even with redaction off "
        f"(saw {leaked}); its clean-run assertion is vacuous"
    )


def test_every_vendor_send_path_is_classified() -> None:
    """A new adapter method fails this test until it is covered or exempted in writing.

    The failure mode this exists for is not a removed `self.redact(...)` -- the wire
    tests catch that -- but a method added next to the redacted ones that quietly
    forgets the hook. Nothing else in the suite would notice, because nothing else
    knows the method exists.
    """
    discovered = set(send_paths())
    classified = set(COVERED) | set(EXEMPT)
    assert not discovered - classified, (
        "unclassified vendor send path(s): "
        f"{sorted(discovered - classified)}. Add a driver to COVERED that captures what "
        "the method sends, or an EXEMPT entry saying why no employee text reaches it."
    )
    assert not classified - discovered, (
        f"classified but no longer present: {sorted(classified - discovered)}"
    )
    assert not set(COVERED) & set(EXEMPT)
    assert all(EXEMPT.values()), "an exemption needs a reason, not a name"


def test_the_discovery_rule_finds_the_adapters_it_is_supposed_to_find() -> None:
    """Guard the guard: a discovery rule that finds nothing passes every other test."""
    discovered = send_paths()
    for expected in ("Claude.structured", "SarvamTTS.speak", "SarvamSTT.batch"):
        assert expected in discovered
    # The runtime, the limiter and the spend ledger are not send paths and must not be
    # swept in; if they ever are, the exemption list becomes noise nobody reads.
    assert not any(name.startswith(("AdapterRuntime.", "TokenBucket.")) for name in discovered)
    assert len(discovered) >= len(COVERED)


# --------------------------------------------------------------------------------------
# The documented override.
# --------------------------------------------------------------------------------------


def test_redaction_is_on_by_default_in_both_vendor_adapters() -> None:
    claude = Claude(client=SimpleNamespace(), runtime=AdapterRuntime("anthropic", "llm"))
    sarvam = SarvamAdapter("translate", client=SimpleNamespace())
    assert claude.redact is redact and sarvam.redact is redact


def test_the_uc3_override_needs_a_passed_policy_and_leaves_the_default_alone() -> None:
    """PRD E9 turns redaction off for uc3 transcripts. That has to stay a passed object.

    The override is legitimate -- an off-channel-comms finding can turn on the phone
    number itself -- and it is also the one thing that could quietly disable the hook
    everywhere. `.claude/rules/adapters.md`: an app that needs an override "documents it
    in their README and passes an explicit policy object; they do not monkeypatch". So
    this asserts three things: uc3 gets its unredacted adapter, it gets it by passing
    `redactor=`, and a `Claude()` built afterwards still redacts.
    """
    from pathlib import Path

    from comms_surveillance import detector

    assert "redactor" in inspect.signature(detector.claude).parameters
    assert "redactor=" in inspect.getsource(detector.claude)

    detector.claude.cache_clear()
    try:
        override = detector.claude()
        assert override.redact is not redact
        assert override.redact(PROBE) == PROBE, "uc3's transcripts must reach Claude verbatim"
    finally:
        detector.claude.cache_clear()

    # The platform default is untouched by uc3 having asked for an exception.
    assert Claude(client=SimpleNamespace(), runtime=AdapterRuntime("anthropic", "llm")).redact is (
        redact
    )
    assert redact(PROBE) != PROBE

    readme = Path(detector.__file__).parent / "README.md"
    assert "Redaction" in readme.read_text(), "an undocumented override is not an override"


async def test_an_injected_policy_reaches_the_wire_only_for_the_app_that_passed_it() -> None:
    """Two adapters, one overridden, one not, over the same kind of transport."""
    overridden = await drive_structured(IDENTITY)
    default = await drive_structured(None)
    assert unredacted(overridden, SECRETS) and not unredacted(default, SECRETS)


# --------------------------------------------------------------------------------------
# The log side. CLAUDE.md: redaction runs before text reaches a vendor *or a log*.
# --------------------------------------------------------------------------------------


@contextmanager
def captured_logs(caplog: pytest.LogCaptureFixture) -> Iterator[list[str]]:
    lines: list[str] = []
    sink_id = logger.add(lambda message: lines.append(str(message)), level="INFO")
    caplog.set_level(logging.INFO)
    try:
        yield lines
    finally:
        logger.remove(sink_id)


async def test_no_fixture_pii_reaches_a_log_at_info_or_above(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drive every covered send path with logging attached and read the whole log.

    Both loggers, because the platform uses loguru and the apps also use the standard
    library, and a rule that only holds for one of them is not a rule.
    """
    with captured_logs(caplog) as lines:
        for driver in COVERED.values():
            await driver(None)
        blob = "\n".join([*lines, caplog.text])
    assert not unredacted([blob.encode()], SECRETS), "employee PII reached a log"
    assert not unredacted([blob.encode()], KNOWN_LEAKS), (
        "the adapters log no content at all, so even the patterns redaction misses "
        "must be absent here; a hit means an adapter started logging its input"
    )


async def test_a_vendor_error_body_is_not_logged_and_not_traced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one text the hook cannot clean: what the vendor sends back.

    A vendor that echoes the offending input in its 400 body hands this process PII it
    never redacted. The adapters must therefore not log or trace error bodies at all --
    `platform/obs/langfuse.py` says "never send raw inputs, outputs, headers, URIs or
    exception messages", and this is the test that says it out loud.
    """
    sink = MemorySink()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": f"invalid input: {PHONE} {EMAIL}"})

    with captured_logs(caplog) as lines:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            adapter = SarvamTranslate(
                client=AsyncSarvamAI(api_subscription_key="mock", httpx_client=http),
                runtime=AdapterRuntime("sarvam", "translate", sink=sink, retry_base=0),
            )
            with pytest.raises(Exception, match=r".*"):
                await adapter.translate(PROBE, target="hi-IN")
        blob = "\n".join([*lines, caplog.text])

    assert not unredacted([blob.encode()], SECRETS)
    assert sink.records and sink.records[-1]["status"] == "error"
    assert not unredacted([json.dumps(sink.records).encode()], SECRETS + KNOWN_LEAKS)


async def test_the_observability_span_carries_metadata_and_no_content() -> None:
    """Langfuse is a log with a nicer UI; the same rule applies to what it receives."""
    sink = MemorySink()
    async with httpx.AsyncClient(transport=httpx.MockTransport(sarvam_handler([]))) as http:
        adapter = SarvamTranslate(
            client=AsyncSarvamAI(api_subscription_key="mock", httpx_client=http),
            runtime=AdapterRuntime("sarvam", "translate", sink=sink),
        )
        await adapter.translate(PROBE, target="hi-IN")
    assert sink.records
    recorded = json.dumps(sink.records)
    assert not unredacted([recorded.encode()], SECRETS + KNOWN_LEAKS)
    # Metadata only, and the character count is a length, not the characters.
    assert sink.records[-1]["units"]["characters"] == len(redact(PROBE))


# --------------------------------------------------------------------------------------
# What the hook misses.
# --------------------------------------------------------------------------------------


def test_redaction_gaps_are_asserted_not_wished_away() -> None:
    """The patterns `redact` does not match. These assertions describe today, not the goal.

    The 4-3-3 mobile is the one that matters: `9876 543 210` is how people write a
    number, `redact` knows only 3-3-4 and 5-5, and the number therefore reaches Sarvam
    and Anthropic intact. It is recorded in `docs/build/BLOCKERS.md`; when the patterns
    grow, this test fails and that entry comes out with it.
    """
    assert redact(f"call {PHONE}") == "call [PHONE]"
    assert redact(f"mail {EMAIL}") == "mail [EMAIL]"
    assert redact(f"id {NATIONAL_ID}") == "id [NATIONAL_ID]"
    # Devanagari digits are matched: `\d` is Unicode-aware.
    assert redact("९२३४५ ६७८९०") == "[PHONE]"

    # Fixed in uc1/P7: every grouping of the same ten digits is masked now. Kept as an
    # assertion rather than deleted, because the bare run and 3-3-4 were always masked
    # and it was the inconsistency between them that hid this one.
    assert redact(GROUPED_PHONE) == "[PHONE]", "4-3-3 mobiles must be masked like any other"
    assert redact("9876-543-210") == "[PHONE]"
    assert redact(f"+91 {GROUPED_PHONE}") == "[PHONE]"
    # Not newly aggressive: these were already masked before the pattern was widened,
    # so nothing is caught now that a differently-spaced copy was not caught before.
    assert redact("order 1234567890") == "order [PHONE]"
    assert redact("ticket 4521") == "ticket 4521", "short reference numbers are untouched"
    assert redact(EMPLOYEE_ID) == EMPLOYEE_ID, "internal identifiers are not patterns"
    assert redact(EMPLOYEE_NAME) == EMPLOYEE_NAME, "names are not redacted; nothing claims they are"
    assert redact("ABCDE1234F") == "ABCDE1234F", "PAN is not in the pattern list"
    assert redact("nau aath saat chhah paanch") == "nau aath saat chhah paanch"
    # Masked, but as the wrong label: a 12-digit run reads as an id before it reads as a
    # number with a country code. It is still masked, which is what the control requires.
    assert redact("919876543210") == "[NATIONAL_ID]"


async def test_the_fixed_grouping_is_masked_on_a_real_wire_not_just_in_the_unit() -> None:
    """The 4-3-3 fix, measured where it matters: the bytes going to Sarvam.

    This test is the reason the fix is trustworthy. Before uc1/P7 it asserted the
    opposite -- that `9876 543 210` left the process intact -- and it passed, which is
    how the leak was found in the first place: a gap asserted only against `redact`
    could plausibly be caught somewhere downstream, and this proved it was not.

    What remains leaking is a different kind of thing. An employee id and a personal
    name are not pattern-shaped, so no regex reaches them; that is the documented limit
    of a pattern hook rather than a bug waiting for a wider expression, and pretending
    otherwise by widening until they matched would catch every capitalised word.
    """
    payloads = await drive_speak(None)
    assert not unredacted(payloads, SECRETS), "a masked pattern reached the vendor"
    assert sorted(unredacted(payloads, KNOWN_LEAKS)) == sorted(KNOWN_LEAKS), (
        "the non-pattern identifiers are expected to pass; if they stopped, say so here"
    )


def test_the_probe_would_fail_a_naive_spot_check() -> None:
    """Sanity: the fixture is not one the hook happens to find easy."""
    cleaned = redact(PROBE)
    assert all(secret not in cleaned for secret in SECRETS)
    assert any(leak in cleaned for leak in KNOWN_LEAKS)
    assert asyncio.iscoroutinefunction(drive_speak)
