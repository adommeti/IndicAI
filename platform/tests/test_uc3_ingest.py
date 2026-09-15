"""Ingestion: discovery, idempotency, transliteration and cost.

Vendors are mocked throughout. What these tests are actually about is the two
properties the prompt names and that nothing downstream can recover if they are
wrong: a sweep run twice must not duplicate a call, and every stored segment
must carry both its native text and a Roman rendering that says where it came
from.
"""

import os
import uuid as uuidlib
from dataclasses import dataclass
from decimal import Decimal

import pytest
from comms_surveillance import ingest, transliterate
from indic_platform.adapters.base import TranscriptSegment


@dataclass
class FakeObject:
    object_name: str
    size: int = 1024


class FakeMinio:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def list_objects(
        self, bucket: str, prefix: str = "", recursive: bool = False
    ) -> list[FakeObject]:
        return [FakeObject(n) for n in self.names if n.startswith(prefix)]


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


# --- stack --------------------------------------------------------------------


def _skip_without_db() -> None:
    if "DATABASE_URL" not in os.environ:
        pytest.skip("DATABASE_URL is not set; run `make stack-core` and `make migrate`")


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
