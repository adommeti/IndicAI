"""Nightly ingestion: object storage -> diarized, transliterated transcript rows.

The shape of PRD E3's batch pipeline. A sweep lists a MinIO prefix, and for every
recording it has not already transcribed it creates a `calls` row, runs Saaras
batch STT with diarization and language auto-detect through the adapter, and
writes one `transcript_segments` row per turn in both the spoken script and a
Roman rendering.

**Idempotency is a database constraint, not a check.** `calls.source_key` is
unique, so a second sweep over the same prefix -- or two workers racing on the
same object -- cannot produce two calls for one recording. `claim` inserts and
lets the constraint decide the winner; the loser skips. Segments are written
with `(call_id, seg_id)` as the primary key and deleted-then-inserted inside the
same transaction, so a retried transcription replaces its turns rather than
appending a second copy.

What this module deliberately does not do: decide anything about a call. It
transcribes and stores. Detection is P3 and P4, and keeping ingestion free of
policy means a re-transcription never changes a flag.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from celery import Celery
from celery.schedules import crontab
from indic_platform.adapters import budget
from indic_platform.adapters.base import TranscriptSegment as AdapterSegment
from indic_platform.db.models import Call, TranscriptSegment
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from comms_surveillance import storage, transliterate

log = logging.getLogger(__name__)

BUCKET = os.environ.get("UC3_BUCKET", "comms-surveillance")
PREFIX = os.environ.get("UC3_PREFIX", "recordings/")
AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".flac", ".ogg")

celery_app = Celery(
    "comms_surveillance",
    broker=os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
    backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1"),
    # `retention` registers `uc3.retention_sweep` and adds its 04:00 beat entry at
    # import time, and a worker started on this module would otherwise import neither:
    # the schedule would be missing from beat and the task unknown to the worker, while
    # every test that imports the module directly still passed. Named here rather than
    # imported at the top of this file because retention imports `celery_app` from it.
    include=["comms_surveillance.retention"],
)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    beat_schedule={
        # Nightly, after the working day the recordings come from (E3).
        "uc3-nightly-sweep": {"task": "uc3.sweep", "schedule": crontab(hour=2, minute=0)},
        # After the sweep, so the night's appends are included (PRD E8).
        "uc3-chain-verify": {"task": "uc3.chain_verify", "schedule": crontab(hour=5, minute=0)},
    },
)

# A transcription that failed on a transient fault should come back, but a
# recording that is simply unreadable should not be retried forever.
RETRY_ON = (OSError, TimeoutError, DBAPIError)
TASK = {
    "autoretry_for": RETRY_ON,
    "retry_backoff": True,
    "retry_backoff_max": 600,
    "retry_jitter": True,
    "max_retries": 3,
}


def engine() -> Any:
    return create_async_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)


@dataclass(frozen=True)
class Recording:
    """One object in the watched prefix."""

    key: str
    uri: str
    size: int


def is_audio(key: str) -> bool:
    return key.lower().endswith(AUDIO_SUFFIXES)


def discover(minio: Any, *, bucket: str = BUCKET, prefix: str = PREFIX) -> list[Recording]:
    """Every audio object under the prefix, oldest key first.

    Listing is all this does. Deciding which of these are new is `claim`'s job,
    and it decides it in the database -- a "have I seen this?" check here would
    be a race between listing and insert.
    """
    found = [
        Recording(key=obj.object_name, uri=f"s3://{bucket}/{obj.object_name}", size=obj.size or 0)
        for obj in minio.list_objects(bucket, prefix=prefix, recursive=True)
        if is_audio(obj.object_name)
    ]
    return sorted(found, key=lambda r: r.key)


async def claim(session: AsyncSession, recording: Recording) -> tuple[uuid.UUID | None, bool]:
    """The call id to transcribe, or None if this recording is already done.

    The unique constraint on `source_key` is what makes the sweep safe to run
    twice and safe to run in parallel -- but "already claimed" is not the same
    as "already transcribed". A call left `pending` because the process died
    between claiming and transcribing, or marked `failed` by a transient vendor
    fault, has to come back on the next sweep. Returning None for any existing
    key would make the constraint that prevents duplicates also prevent retry,
    and the nightly sweep would report a clean night over a call that never got
    transcribed.

    So: insert, and if the key is taken, look at what is actually there. Returns
    (call id or None, whether this is a retry of an earlier attempt).
    """
    call_id = uuid.uuid4()
    session.add(
        Call(
            id=call_id,
            source_uri=recording.uri,
            source_key=recording.key,
            status="pending",
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.execute(select(Call).where(Call.source_key == recording.key))
        ).scalar_one_or_none()
        if existing is None or existing.status == "transcribed":
            return None, False
        return existing.id, True
    return call_id, False


async def romanise(
    segments: Sequence[AdapterSegment], client: Any | None = None
) -> list[tuple[str, str]]:
    """(roman, source) for each segment, via the configured backend.

    When Sarvam is configured, one client is built for the whole transcript
    rather than one per segment -- a call has dozens of turns and each adapter
    carries its own HTTP client.
    """
    if client is None and transliterate.use_sarvam():
        from indic_platform.adapters.sarvam_translate import SarvamTranslate

        client = SarvamTranslate()
    return [await transliterate.to_roman(s.text, s.language or "", client) for s in segments]


async def store(
    session: AsyncSession,
    call_id: uuid.UUID,
    segments: Sequence[AdapterSegment],
    romans: Sequence[tuple[str, str]],
    *,
    cost_inr: Decimal,
    model: str,
) -> None:
    """Replace this call's segments and mark it transcribed, in one transaction.

    Delete-then-insert rather than upsert: a re-transcription can return a
    different number of turns, and leaving orphaned segments from the previous
    run would corrupt every span offset downstream.
    """
    await session.execute(delete(TranscriptSegment).where(TranscriptSegment.call_id == call_id))
    languages: list[str] = []
    for index, (segment, (roman, source)) in enumerate(zip(segments, romans, strict=True)):
        session.add(
            TranscriptSegment(
                call_id=call_id,
                seg_id=index,
                speaker=segment.speaker or "",
                start_ms=segment.start_ms,
                end_ms=max(segment.end_ms, segment.start_ms),
                text=segment.text,
                text_roman=roman,
                language=segment.language or "",
                roman_source=source,
            )
        )
        if segment.language and segment.language not in languages:
            languages.append(segment.language)

    call = await session.get(Call, call_id)
    if call is None:
        raise LookupError(f"call {call_id} disappeared mid-transcription")
    call.status = "transcribed"
    call.languages = languages
    call.duration_s = round(max((s.end_ms for s in segments), default=0) / 1000)
    call.participants = sorted({s.speaker for s in segments if s.speaker})
    call.stt_cost_inr = cost_inr
    call.stt_model = model


def spend(sink: Any, model: str, since: int = 0) -> Decimal:
    """What this call's transcription cost, from the adapter's own records.

    `since` is how many records the sink already held before this call started.
    Without it a sweep that reuses one sink charges the third recording for the
    first three, which is exactly the bug this signature exists to prevent.

    `None` -- an injected adapter with no sink -- is zero, not an estimate. A
    made-up per-minute figure in a cost column is worse than a visible zero.
    """
    total = Decimal("0")
    for record in (getattr(sink, "records", None) or [])[since:]:
        if not model or record.get("model") == model:
            total += Decimal(str(record.get("cost_inr", 0) or 0))
    return total


def sink_size(sink: Any) -> int:
    return len(getattr(sink, "records", None) or [])


DIARIZE_MODEL = "saaras:v3:diarized"


def stt_with_sink() -> tuple[Any, Any]:
    """A Saaras adapter and the sink recording what it spends.

    The cost of transcribing one call has to come from that call's own adapter
    records, so each call gets its own sink rather than reading a shared one and
    attributing someone else's minutes.
    """
    from indic_platform.adapters.runtime import AdapterRuntime
    from indic_platform.adapters.sarvam_stt import SarvamSTT
    from indic_platform.obs.langfuse import MemorySink, TeeSink, default_sink

    # Tee, not replace. Passing a bare MemorySink here substituted for the default
    # one, so every Saaras call uc3 ever made emitted no Langfuse span -- against
    # CLAUDE.md's "every adapter call emits a Langfuse span (metadata only)", and it
    # meant uc3's STT spend was the one vendor leg invisible to observability while
    # still being billed. The memory sink stays because per-call cost attribution
    # needs a slice nobody else writes to; the default sink is added back beside it.
    sink = MemorySink()
    return SarvamSTT(
        runtime=AdapterRuntime("sarvam", "stt", sink=TeeSink(sink, default_sink()))
    ), sink


async def transcribe_call(
    session: AsyncSession,
    call_id: uuid.UUID,
    audio: str,
    *,
    stt: Any = None,
    sink: Any = None,
    translit_client: Any = None,
    language: str = "auto",
) -> int:
    """Transcribe one claimed call and persist its turns. Returns the turn count.

    `audio` is a **local path or file:// URI**, not an object-store URI: the
    adapter boundary takes local media only, so the caller materializes the
    object first (`storage.fetch_object`).

    `language="auto"` is deliberate: PRD E3's corpus is code-mixed, and asking
    Saaras to detect the language beats trusting a filename convention.

    An injected `stt` may bring its own `sink`; without one the cost is recorded
    as zero rather than guessed, and `stt_cost_inr` says so.
    """
    if stt is None:
        stt, sink = stt_with_sink()

    call = await session.get(Call, call_id)
    if call is None:
        raise LookupError(f"no call {call_id}")

    # Only this call's spend: a sweep reuses one sink across recordings, so
    # start counting from where it already was.
    before = sink_size(sink)
    # One recording is uc3's unit of work, so it is the honest boundary for the
    # per-session cap. Without a scope every vendor call in a nightly batch charges
    # "no session at all" and the only thing between one pathological recording --
    # a ten-hour file, a retry storm -- and the whole day's budget is the day cap
    # itself. The scope is task-local, so concurrent calls never charge each other.
    with budget.session_scope(str(call_id)):
        segments = await stt.batch(audio, language=language, diarize=True)
        romans = await romanise(segments, translit_client)
    await store(
        session,
        call_id,
        segments,
        romans,
        cost_inr=spend(sink, DIARIZE_MODEL, before),
        model=DIARIZE_MODEL,
    )
    return len(segments)


async def ingest_prefix(
    minio: Any,
    *,
    bucket: str = BUCKET,
    prefix: str = PREFIX,
    stt: Any = None,
    sink: Any = None,
    translit_client: Any = None,
    session_factory: Any = None,
) -> dict[str, int]:
    """One sweep: claim everything under the prefix that is not done, and transcribe it.

    Returns counts rather than rows so a Celery task can serialise the result.
    A recording that fails to transcribe marks its call `failed` and does not
    stop the sweep -- one unreadable file must not hold up a night's batch --
    and the next sweep picks it up again, because `claim` retries anything that
    is not `transcribed`.

    One adapter for the whole sweep, not one per recording: the circuit breaker
    counts failures per instance, so a fresh adapter each time would mean a dead
    vendor is retried from scratch twenty times and the breaker never opens.
    """
    if stt is None:
        stt, sink = stt_with_sink()
    recordings = discover(minio, bucket=bucket, prefix=prefix)
    counts = {
        "seen": len(recordings),
        "claimed": 0,
        "retried": 0,
        "transcribed": 0,
        "skipped": 0,
        "failed": 0,
    }

    for recording in recordings:
        async with session_factory() as session:
            call_id, retry = await claim(session, recording)
            if call_id is None:
                counts["skipped"] += 1
                continue
            counts["retried" if retry else "claimed"] += 1
            await session.commit()

        try:
            with storage.fetch_object(minio, recording.key, bucket=bucket) as path:
                async with session_factory() as session:
                    await transcribe_call(
                        session,
                        call_id,
                        str(path),
                        stt=stt,
                        sink=sink,
                        translit_client=translit_client,
                    )
                    await session.commit()
            counts["transcribed"] += 1
        except Exception:
            # Why it failed matters to whoever works the queue in the morning,
            # and a status alone does not say.
            log.exception("uc3 ingest failed for %s", recording.key)
            async with session_factory() as failing:
                call = await failing.get(Call, call_id)
                if call is not None:
                    call.status = "failed"
                    await failing.commit()
            counts["failed"] += 1
    return counts


@celery_app.task(name="uc3.sweep", **TASK)
def sweep_task(bucket: str = BUCKET, prefix: str = PREFIX) -> dict[str, int]:
    """The nightly beat entry point."""

    from comms_surveillance.storage import client

    async def run() -> dict[str, int]:
        db = engine()
        factory = _factory(db)
        try:
            return await ingest_prefix(
                client(), bucket=bucket, prefix=prefix, session_factory=factory
            )
        finally:
            await db.dispose()

    return asyncio.run(run())


def _factory(db: Any) -> Any:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(db, expire_on_commit=False)


async def ingest_golden(
    source: Any = None, *, prefix: str = "recordings/golden/"
) -> dict[str, int]:
    """Push the 20 golden WAVs through the real pipeline (`make ingest-golden-audio`).

    Real MinIO, real Postgres, real Saaras. The point is to prove the whole path
    works on audio whose ground-truth diarization we already hold, so P1's
    `attribution_accuracy` can be scored against what actually landed in
    `transcript_segments` rather than against a fixture.
    """
    from pathlib import Path

    from comms_surveillance.storage import client, push_golden

    golden = Path(
        source or Path(__file__).parents[2] / "platform/eval/golden/uc3_surveillance/audio"
    )
    minio = client()
    keys = push_golden(golden, minio=minio, prefix=prefix)
    if not keys:
        # Silently sweeping an empty prefix and reporting success is how a
        # broken path gets mistaken for a passing run.
        raise FileNotFoundError(f"no WAVs under {golden}; nothing to ingest")

    db = engine()
    try:
        return await ingest_prefix(minio, prefix=prefix, session_factory=_factory(db))
    finally:
        await db.dispose()


def main() -> None:
    import asyncio
    import json

    print(json.dumps(asyncio.run(ingest_golden()), indent=2))


if __name__ == "__main__":
    main()


@celery_app.task(name="uc3.chain_verify", **TASK)
def chain_verify_task() -> dict[str, Any]:
    """Nightly re-walk of the audit chains (PRD E8).

    Reads only, so it is safe to run at any time and safe to re-run; the alert
    is the metric `uc3_audit_chain_breaks`, emitted whether or not a break was
    found so that a silent job is distinguishable from a clean one.
    """
    import asyncio

    from comms_surveillance.audit import run_chain_verify

    async def run() -> dict[str, Any]:
        db = engine()
        try:
            return await run_chain_verify(_factory(db))
        finally:
            await db.dispose()

    return asyncio.run(run())
