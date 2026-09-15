"""Ingestion: discovery, idempotency, transliteration and cost.

Vendors are mocked throughout. What these tests are actually about is the two
properties the prompt names and that nothing downstream can recover if they are
wrong: a sweep run twice must not duplicate a call, and every stored segment
must carry both its native text and a Roman rendering that says where it came
from.
"""

import os
import pathlib
import uuid as uuidlib
from dataclasses import dataclass
from decimal import Decimal

import pytest
from comms_surveillance import ingest, storage, transliterate
from indic_platform.adapters.base import TranscriptSegment
from indic_platform.adapters.sarvam_common import local_media


@dataclass
class FakeObject:
    object_name: str
    size: int = 1024


class FakeMinio:
    """Lists keys and, like the real client, can materialize one to a local file."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.fetched: list[str] = []

    def list_objects(
        self, bucket: str, prefix: str = "", recursive: bool = False
    ) -> list[FakeObject]:
        return [FakeObject(n) for n in self.names if n.startswith(prefix)]

    def fget_object(self, bucket: str, key: str, path: str) -> None:
        self.fetched.append(key)
        pathlib.Path(path).write_bytes(b"RIFF....WAVEfmt ")


class FakeSTT:
    """Returns a fixed two-speaker Tamil/Hindi transcript, and counts its calls."""

    def __init__(self, segments: list[TranscriptSegment] | None = None) -> None:
        self.calls = 0
        self.segments = segments or [
            TranscriptSegment(
                start_ms=0, end_ms=2000, speaker="SPEAKER_00", text="வணக்கம்", language="ta-IN"
            ),
            TranscriptSegment(
                start_ms=2300,
                end_ms=5000,
                speaker="SPEAKER_01",
                text="मैंने अप्रकाशित नतीजे देखे हैं",
                language="hi-IN",
            ),
        ]

    async def batch(
        self, uri: str, *, language: str = "auto", diarize: bool = False
    ) -> list[TranscriptSegment]:
        self.calls += 1
        assert diarize, "ingestion must ask for diarization"
        assert language == "auto", "the corpus is code-mixed; language is detected"
        # The adapter boundary takes local media only. Asserting it here is what
        # would have caught the s3:// URI being passed straight through.
        local_media(uri)
        return list(self.segments)


# --- discovery ----------------------------------------------------------------


def test_discovery_takes_audio_only_and_orders_it() -> None:
    minio = FakeMinio(
        [
            "recordings/b.wav",
            "recordings/a.wav",
            "recordings/notes.txt",
            "recordings/c.mp3",
            "other/d.wav",
        ]
    )
    found = ingest.discover(minio, bucket="b", prefix="recordings/")
    assert [r.key for r in found] == ["recordings/a.wav", "recordings/b.wav", "recordings/c.mp3"]
    assert all(r.uri.startswith("s3://b/") for r in found)


def test_a_transcript_file_is_not_a_recording() -> None:
    assert ingest.is_audio("x.wav") and ingest.is_audio("X.WAV")
    assert not ingest.is_audio("x.json") and not ingest.is_audio("x.wav.txt")


# --- transliteration ----------------------------------------------------------


def test_devanagari_and_telugu_transliterate_offline() -> None:
    roman, source = transliterate.offline("मैंने अप्रकाशित नतीजे देखे हैं", "hi-IN")
    assert roman == "maiṁnē aprakāśita natījē dēkhē haiṁ"
    assert source == "indic-transliteration:iso"
    telugu, _ = transliterate.offline("క్లయింట్ ఆర్డర్", "te-IN")
    assert telugu == "klayiṁṭ ārḍar"


def test_roman_script_input_is_left_alone() -> None:
    """Hinglish typed in Roman must not be run through a Devanagari mapping."""
    for text, language in [
        ("Client ko bolo bara percent return guaranteed hai", "hi-Latn"),
        ("Tell the client the fund is guaranteed", "en-IN"),
    ]:
        roman, source = transliterate.offline(text, language)
        assert roman == text
        assert source == "verbatim"


def test_the_offline_backend_is_wrong_for_tamil_and_this_is_recorded() -> None:
    """Pins a known defect so it cannot be mistaken for bad STT.

    Tamil script writes one letter for the k/g pair, so a script-to-script
    mapping with no phonology cannot choose: every scheme the library offers
    renders `வணக்கம்` with `gh` where it should be `kk`. Sarvam, which is
    phonological, returns `Vanakkam` (verified against the live API). This test
    fails if the library is ever fixed -- at which point `KNOWN_WEAK` should
    shrink, which is the point.
    """
    roman, _ = transliterate.offline("வணக்கம்", "ta-IN")
    assert "gh" in roman
    assert "vaṇakkam" not in roman.lower()
    assert "ta-IN" in transliterate.KNOWN_WEAK


async def test_the_sarvam_backend_is_used_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTranslate:
        def __init__(self) -> None:
            self.seen: list[tuple[str, str, str]] = []

        async def transliterate(self, text: str, *, source: str, target: str = "en-IN") -> str:
            self.seen.append((text, source, target))
            return "Vanakkam"

    client = FakeTranslate()
    monkeypatch.setenv("SARVAM_TRANSLITERATE", "true")
    roman, source = await transliterate.to_roman("வணக்கம்", "ta-IN", client)
    assert (roman, source) == ("Vanakkam", "sarvam:mayura:v1")
    assert client.seen == [("வணக்கம்", "ta-IN", "en-IN")]

    monkeypatch.setenv("SARVAM_TRANSLITERATE", "false")
    offline_roman, offline_source = await transliterate.to_roman("வணக்கம்", "ta-IN", client)
    assert offline_source.startswith("indic-transliteration")
    assert offline_roman != "Vanakkam"
    assert len(client.seen) == 1, "the offline path must not call the vendor"


async def test_sarvam_is_not_called_for_text_already_in_roman() -> None:
    class Exploding:
        async def transliterate(self, *a: object, **k: object) -> str:
            raise AssertionError("must not spend on text that needs no conversion")

    roman, source = await transliterate.via_sarvam("Client ko bolo", "hi-Latn", Exploding())
    assert (roman, source) == ("Client ko bolo", "verbatim")


# --- cost ---------------------------------------------------------------------


def test_cost_comes_from_the_adapters_records() -> None:
    class Sink:
        records = [
            {"model": "saaras:v3:diarized", "cost_inr": 0.41},
            {"model": "saaras:v3:diarized", "cost_inr": 0.09},
            {"model": "mayura:v1", "cost_inr": 5.00},
        ]

    # Only this call's STT, not the transliteration that happened alongside it.
    assert ingest.spend(Sink(), "saaras:v3:diarized") == Decimal("0.50")


def test_a_missing_sink_reports_zero_rather_than_an_estimate() -> None:
    assert ingest.spend(None, "saaras:v3:diarized") == Decimal("0")

    class NoRecords:
        records = None

    assert ingest.spend(NoRecords(), "saaras:v3:diarized") == Decimal("0")


# --- the defects the pre-ship review proved ------------------------------------
#
# Each of these failed before the fix.


def test_a_code_mixed_segment_is_fully_romanised() -> None:
    """The corpus is code-mixed; a majority-Latin segment still needs converting.

    `already_roman` used to be a >50%-ASCII test, so `Client ko bolo मैं करूंगा`
    was stored verbatim -- Devanagari still sitting inside `text_roman` under
    `roman_source="verbatim"`, which P3's lexicon would never match.
    """
    roman, source = transliterate.offline("Client ko bolo मैं करूंगा", "hi-IN")
    assert roman == "Client ko bolo maiṁ karūṁgā"
    assert source == "indic-transliteration:iso"
    assert not transliterate.NATIVE_RUN.search(roman), "no native script may survive"

    mixed, mixed_source = transliterate.offline("NAV 12% हो गया", "hi-IN")
    assert mixed == "NAV 12% hō gayā"
    assert mixed_source != "verbatim"


def test_pure_roman_text_is_still_left_alone() -> None:
    assert transliterate.already_roman("Client ko bolo bara percent")
    assert not transliterate.already_roman("Client ko bolo मैं")


def test_cost_counts_only_what_this_call_added() -> None:
    """A sweep reuses one sink, so call three must not be charged for one to three.

    Before the fix, three recordings at Rs 0.25 each persisted as 0.25 / 0.50 /
    0.75 -- Rs 1.50 billed against Rs 0.75 actually spent.
    """

    class Sink:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

    sink = Sink()
    charged = []
    for _ in range(3):
        before = ingest.sink_size(sink)
        sink.records.append({"model": "saaras:v3:diarized", "cost_inr": 0.25})
        charged.append(ingest.spend(sink, "saaras:v3:diarized", before))

    assert charged == [Decimal("0.25")] * 3
    assert sum(charged) == Decimal("0.75")


async def test_an_object_store_uri_never_reaches_the_adapter() -> None:
    """The adapter takes local media only; the app materializes first.

    `local_media` raises on any non-file scheme, so passing `s3://...` straight
    through meant every real recording would fail. `fetch_object` is the
    materialization step and this asserts the contract it exists to satisfy.
    """
    from indic_platform.adapters.sarvam_common import local_media

    with pytest.raises(ValueError, match="Materialize media"):
        local_media("s3://comms-surveillance/recordings/one.wav")

    minio = FakeMinio(["recordings/one.wav"])
    with storage.fetch_object(minio, "recordings/one.wav", bucket="b") as path:
        assert path.is_file()
        assert local_media(str(path)) == path  # the adapter would accept this
        kept = path
    assert not kept.exists(), "a call recording must not be left in the temp dir"


# --- stack --------------------------------------------------------------------


def _skip_without_db() -> None:
    """Skip unless Postgres is actually reachable.

    `DATABASE_URL` is set in this repo's dev shell whether or not the stack is
    up, so checking only for the variable turns "no database" into four
    connection-refused failures that read like real ones.
    """
    import socket
    from urllib.parse import urlparse

    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), 1):
            pass
    except OSError:
        pytest.skip(f"Postgres at {parsed.hostname}:{parsed.port} is not reachable")


@pytest.mark.integration
async def test_a_second_sweep_does_not_duplicate_a_call() -> None:
    """The acceptance criterion: idempotent on re-run.

    Runs in CI's integration job (Postgres). Saaras is mocked -- this is about
    the uniqueness constraint, not transcription quality. The second sweep must
    claim nothing and, crucially, must not call the vendor again: a duplicate
    transcription costs money even when the row is later discarded.
    """
    _skip_without_db()
    from indic_platform.db.models import Call, TranscriptSegment
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    prefix = f"itest/{uuidlib.uuid4()}/"
    minio = FakeMinio([f"{prefix}one.wav", f"{prefix}two.wav"])
    stt = FakeSTT()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        first = await ingest.ingest_prefix(minio, prefix=prefix, stt=stt, session_factory=factory)
        assert first == {"seen": 2, "claimed": 2, "transcribed": 2, "skipped": 0, "failed": 0}
        assert stt.calls == 2

        second = await ingest.ingest_prefix(minio, prefix=prefix, stt=stt, session_factory=factory)
        assert second["claimed"] == 0 and second["skipped"] == 2
        assert stt.calls == 2, "a re-sweep must not pay to transcribe the same recording twice"

        async with AsyncSession(engine) as db:
            calls = (
                await db.execute(
                    select(func.count()).select_from(Call).where(Call.source_key.like(f"{prefix}%"))
                )
            ).scalar_one()
            assert calls == 2
            ids = (
                (await db.execute(select(Call.id).where(Call.source_key.like(f"{prefix}%"))))
                .scalars()
                .all()
            )
            segments = (
                await db.execute(
                    select(func.count())
                    .select_from(TranscriptSegment)
                    .where(TranscriptSegment.call_id.in_(ids))
                )
            ).scalar_one()
            assert segments == 4, "two calls of two turns each, not four"
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_segments_are_stored_in_both_scripts_with_their_source() -> None:
    """The acceptance criterion: segments have both native and transliterated text."""
    _skip_without_db()
    from indic_platform.db.models import Call, TranscriptSegment
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    prefix = f"itest/{uuidlib.uuid4()}/"
    minio = FakeMinio([f"{prefix}call.wav"])
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await ingest.ingest_prefix(minio, prefix=prefix, stt=FakeSTT(), session_factory=factory)
        async with AsyncSession(engine) as db:
            call = (
                await db.execute(select(Call).where(Call.source_key == f"{prefix}call.wav"))
            ).scalar_one()
            assert call.status == "transcribed"
            assert call.duration_s == 5
            assert sorted(call.participants) == ["SPEAKER_00", "SPEAKER_01"]
            assert sorted(call.languages) == ["hi-IN", "ta-IN"]

            rows = (
                (
                    await db.execute(
                        select(TranscriptSegment)
                        .where(TranscriptSegment.call_id == call.id)
                        .order_by(TranscriptSegment.seg_id)
                    )
                )
                .scalars()
                .all()
            )
            assert [r.seg_id for r in rows] == [0, 1]
            for row in rows:
                assert row.text and row.text_roman
                assert row.roman_source, "every Roman rendering says what produced it"
            # Native text is preserved exactly; the Roman copy is derived.
            assert rows[0].text == "வணக்கம்"
            assert rows[1].text == "मैंने अप्रकाशित नतीजे देखे हैं"
            assert rows[1].text_roman == "maiṁnē aprakāśita natījē dēkhē haiṁ"
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_a_retranscription_replaces_turns_rather_than_appending() -> None:
    """A re-run that returns fewer turns must not leave the old ones behind."""
    _skip_without_db()
    from indic_platform.db.models import Call, TranscriptSegment
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    prefix = f"itest/{uuidlib.uuid4()}/"
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await ingest.ingest_prefix(
            FakeMinio([f"{prefix}call.wav"]), prefix=prefix, stt=FakeSTT(), session_factory=factory
        )
        async with AsyncSession(engine) as db:
            call_id = (
                await db.execute(select(Call.id).where(Call.source_key == f"{prefix}call.wav"))
            ).scalar_one()

        shorter = FakeSTT(
            [
                TranscriptSegment(
                    start_ms=0, end_ms=1000, speaker="SPEAKER_00", text="ठीक है", language="hi-IN"
                )
            ]
        )
        async with factory() as db:
            await ingest.transcribe_call(db, call_id, "s3://x/call.wav", stt=shorter)
            await db.commit()

        async with AsyncSession(engine) as db:
            rows = (
                (
                    await db.execute(
                        select(TranscriptSegment).where(TranscriptSegment.call_id == call_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1, "the two turns from the first run must be gone"
            assert rows[0].text == "ठीक है"
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_one_unreadable_recording_does_not_stop_the_sweep() -> None:
    """A night's batch must survive a single bad file, and mark it failed."""
    _skip_without_db()
    from indic_platform.db.models import Call
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    prefix = f"itest/{uuidlib.uuid4()}/"

    class Flaky(FakeSTT):
        async def batch(
            self, uri: str, *, language: str = "auto", diarize: bool = False
        ) -> list[TranscriptSegment]:
            if uri.endswith("bad.wav"):
                raise RuntimeError("unreadable media")
            return await super().batch(uri, language=language, diarize=diarize)

    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        counts = await ingest.ingest_prefix(
            FakeMinio([f"{prefix}bad.wav", f"{prefix}good.wav"]),
            prefix=prefix,
            stt=Flaky(),
            session_factory=factory,
        )
        assert counts["transcribed"] == 1 and counts["failed"] == 1
        async with AsyncSession(engine) as db:
            rows = (
                await db.execute(
                    select(Call.source_key, Call.status).where(Call.source_key.like(f"{prefix}%"))
                )
            ).all()
            statuses: dict[str, str] = {row[0]: row[1] for row in rows}
            assert statuses[f"{prefix}bad.wav"] == "failed"
            assert statuses[f"{prefix}good.wav"] == "transcribed"
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_a_failed_call_is_retried_on_the_next_sweep() -> None:
    """The constraint that stops duplicates must not also stop retry.

    Before the fix, `claim` returned None for any existing `source_key`, so a
    call marked `failed` by a transient vendor fault was never transcribed again
    and every later sweep reported a clean night over it.
    """
    _skip_without_db()
    from indic_platform.db.models import Call, TranscriptSegment
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    prefix = f"itest/{uuidlib.uuid4()}/"
    minio = FakeMinio([f"{prefix}call.wav"])

    class FailsOnce(FakeSTT):
        async def batch(
            self, uri: str, *, language: str = "auto", diarize: bool = False
        ) -> list[TranscriptSegment]:
            if self.calls == 0:
                self.calls += 1
                raise TimeoutError("vendor hiccup")
            return await super().batch(uri, language=language, diarize=diarize)

    stt = FailsOnce()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        first = await ingest.ingest_prefix(minio, prefix=prefix, stt=stt, session_factory=factory)
        assert first["claimed"] == 1 and first["failed"] == 1
        async with AsyncSession(engine) as db:
            status = (
                await db.execute(select(Call.status).where(Call.source_key == f"{prefix}call.wav"))
            ).scalar_one()
            assert status == "failed"

        second = await ingest.ingest_prefix(minio, prefix=prefix, stt=stt, session_factory=factory)
        assert second["retried"] == 1, "a failed call must come back"
        assert second["skipped"] == 0
        assert second["transcribed"] == 1

        async with AsyncSession(engine) as db:
            call = (
                await db.execute(select(Call).where(Call.source_key == f"{prefix}call.wav"))
            ).scalar_one()
            assert call.status == "transcribed"
            rows = (
                (
                    await db.execute(
                        select(TranscriptSegment).where(TranscriptSegment.call_id == call.id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 2

        # And once it IS transcribed, a further sweep skips it as before.
        third = await ingest.ingest_prefix(minio, prefix=prefix, stt=stt, session_factory=factory)
        assert third["skipped"] == 1 and third["retried"] == 0
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_the_persisted_cost_is_this_calls_own_spend() -> None:
    """`stt_cost_inr` on the row, not just the helper — and not cumulative.

    Three recordings through one shared sink, each costing Rs 0.25, must persist
    0.25 apiece rather than 0.25 / 0.50 / 0.75.
    """
    _skip_without_db()
    from indic_platform.db.models import Call
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    prefix = f"itest/{uuidlib.uuid4()}/"

    class Sink:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

    class Billing(FakeSTT):
        def __init__(self, sink: Sink) -> None:
            super().__init__()
            self.sink = sink

        async def batch(
            self, uri: str, *, language: str = "auto", diarize: bool = False
        ) -> list[TranscriptSegment]:
            out = await super().batch(uri, language=language, diarize=diarize)
            self.sink.records.append({"model": "saaras:v3:diarized", "cost_inr": 0.25})
            return out

    sink = Sink()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await ingest.ingest_prefix(
            FakeMinio([f"{prefix}a.wav", f"{prefix}b.wav", f"{prefix}c.wav"]),
            prefix=prefix,
            stt=Billing(sink),
            sink=sink,
            session_factory=factory,
        )
        async with AsyncSession(engine) as db:
            costs = (
                (
                    await db.execute(
                        select(Call.stt_cost_inr)
                        .where(Call.source_key.like(f"{prefix}%"))
                        .order_by(Call.source_key)
                    )
                )
                .scalars()
                .all()
            )
            models = (
                (await db.execute(select(Call.stt_model).where(Call.source_key.like(f"{prefix}%"))))
                .scalars()
                .all()
            )
        assert [Decimal(str(c)) for c in costs] == [Decimal("0.2500")] * 3
        assert set(models) == {"saaras:v3:diarized"}
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_the_recording_is_materialized_before_the_adapter_sees_it() -> None:
    """The sweep fetches each object to local disk; the adapter never sees s3://."""
    _skip_without_db()
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    prefix = f"itest/{uuidlib.uuid4()}/"
    minio = FakeMinio([f"{prefix}call.wav"])
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        counts = await ingest.ingest_prefix(
            minio, prefix=prefix, stt=FakeSTT(), session_factory=factory
        )
        # FakeSTT.batch calls local_media(uri); reaching "transcribed" proves the
        # path it was handed was a real local file.
        assert counts["transcribed"] == 1
        assert minio.fetched == [f"{prefix}call.wav"]
    finally:
        await engine.dispose()
