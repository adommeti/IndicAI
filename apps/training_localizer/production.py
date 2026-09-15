"""Production stages: dub, tts_summary, captions, package (PRD D4 steps 8-9).

These run after the human gate. Everything here reads `approved` rows — never
`post_edit` — because the whole point of the reviewer step is that what ships is
what a person signed off.

That is also what makes the re-run rule work: editing one segment writes a new
`approved` version, and only these four tasks need to run again. Nothing here
calls `adapt`, `translate`, `post_edit` or the judge, and
`test_a_reviewer_edit_reproduces_without_retranslating` asserts exactly that.

Dubbing follows ADR 0003 path (a): the approved script goes up as SRT to the
job's `inputs/source/` slot, so the reviewer's text is what gets spoken.
"""

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from indic_platform.db.models import Artifact, Module
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from training_localizer import stages
from training_localizer.captions import Cue, parse_srt_timings, timing_fit, to_srt, to_vtt
from training_localizer.storage import Stored, object_key
from training_localizer.terminology import load_glossary

# Bulbul's stock voice per language. Voice cloning stays off until an instructor
# consents (PRD D8, G2 Q6), and ADR 0003 records that `voice_id` is required
# when cloning is false because cloning is the API default.
DEFAULT_VOICE = {"hi-IN": "shreya", "te-IN": "shreya", "ta-IN": "shreya"}

# Dubbing polling: capped exponential backoff. A three-minute module takes
# minutes, not seconds, so start slow rather than hammering the job endpoint.
POLL_INITIAL_S = 10.0
POLL_MAX_S = 120.0
POLL_TIMEOUT_S = 3600.0


class Storage(Protocol):
    def put(self, data: bytes, *, key: str, content_type: str) -> Stored: ...


@dataclass(frozen=True)
class ApprovedSegment:
    seg_id: int
    start_ms: int
    end_ms: int
    text: str
    locked: bool
    version: int


def cues(segments: list[ApprovedSegment]) -> list[Cue]:
    return [
        Cue(seg_id=s.seg_id, start_ms=s.start_ms, end_ms=s.end_ms, text=s.text) for s in segments
    ]


async def load_approved(
    db: AsyncSession, module_id: uuid.UUID, language: str
) -> list[ApprovedSegment]:
    """The reviewer-approved script, or nothing.

    Production refuses to run on a partially approved module rather than
    shipping a mix of approved and machine text, which is the kind of artifact
    nobody can later say was reviewed.
    """
    from training_localizer.pipeline import latest, load_segments

    glossary = load_glossary()
    segments = {s.seg_id: s for s in await load_segments(db, module_id, glossary)}
    approved = await latest(db, module_id, language, "approved")
    missing = sorted(set(segments) - set(approved))
    if missing:
        raise LookupError(
            f"{len(missing)} segment(s) are not approved for {language}: {missing[:10]}"
        )
    return [
        ApprovedSegment(
            seg_id=seg_id,
            start_ms=segments[seg_id].start_ms,
            end_ms=segments[seg_id].end_ms,
            text=row.text,
            locked=segments[seg_id].locked,
            version=row.version,
        )
        for seg_id, row in sorted(approved.items())
    ]


# --- captions -----------------------------------------------------------------


def build_captions(segments: list[ApprovedSegment], *, language: str) -> tuple[str, str]:
    """(WebVTT for the player, SRT for the dubbing job's input slot)."""
    track = cues(segments)
    return to_vtt(track, language=language), to_srt(track)


async def captions_task(
    db: AsyncSession,
    *,
    module_id: uuid.UUID,
    language: str,
    storage: Storage,
) -> dict[str, Any]:
    segments = await load_approved(db, module_id, language)
    vtt, srt = build_captions(segments, language=language)
    stored_vtt = storage.put(
        vtt.encode(),
        key=object_key(str(module_id), language, "captions", ".vtt"),
        content_type="text/vtt",
    )
    stored_srt = storage.put(
        srt.encode(),
        key=object_key(str(module_id), language, "script", ".srt"),
        content_type="application/x-subrip",
    )
    await write_artifact(
        db,
        module_id=module_id,
        language=language,
        kind="captions",
        stored=stored_vtt,
        meta={"cues": len(segments), "approved_versions": [s.version for s in segments]},
    )
    await write_artifact(
        db,
        module_id=module_id,
        language=language,
        kind="script_srt",
        stored=stored_srt,
        meta={"cues": len(segments), "purpose": "dubbing input (ADR 0003 path a)"},
    )
    return {"stage": "captions", "language": language, "cues": len(segments)}


# --- dub ----------------------------------------------------------------------


class Dubber(Protocol):
    async def submit(
        self, video_uri: str, *, target_languages: list[str], voice_map: dict[str, str]
    ) -> str: ...
    async def status(self, job_id: str) -> dict[str, Any]: ...
    async def fetch(self, job_id: str) -> dict[str, Any]: ...


async def poll_until_done(
    dubber: Dubber,
    job_id: str,
    *,
    initial: float = POLL_INITIAL_S,
    maximum: float = POLL_MAX_S,
    budget_s: float = POLL_TIMEOUT_S,
    sleep: Any = asyncio.sleep,
) -> dict[str, Any]:
    """Capped exponential backoff until the job finishes, fails, or we give up.

    `budget_s` is the total time allowed across all polls, not a per-call
    deadline, which is why it is not `asyncio.timeout`. `sleep` is injected so a
    test can exercise the backoff without waiting; the delays it is called with
    are asserted rather than the wall clock.
    """
    waited = 0.0
    delay = initial
    while waited < budget_s:
        state = await dubber.status(job_id)
        status = str(state.get("status", "")).lower()
        if status in {"completed", "success", "succeeded"}:
            return state
        if status in {"failed", "error", "cancelled"}:
            raise RuntimeError(f"Dubbing job {job_id} ended as {status}: {state}")
        await sleep(delay)
        waited += delay
        delay = min(delay * 2, maximum)
    raise TimeoutError(f"Dubbing job {job_id} did not finish within {budget_s}s")


async def dub_task(
    db: AsyncSession,
    *,
    module_id: uuid.UUID,
    language: str,
    video_uri: str,
    dubber: Dubber,
    storage: Storage,
    fetch_bytes: Any,
    sleep: Any = asyncio.sleep,
) -> dict[str, Any]:
    """Submit, poll, store the dubbed MP4, and measure what came back.

    `fetch_bytes(url) -> bytes` is injected rather than imported so the media
    download is testable and so this module never opens a socket of its own.
    """
    segments = await load_approved(db, module_id, language)
    job_id = await dubber.submit(
        video_uri,
        target_languages=[language],
        voice_map={language: DEFAULT_VOICE[language]},
    )
    await poll_until_done(dubber, job_id, sleep=sleep)
    exports = await dubber.fetch(job_id)

    urls = exports.get("data", exports)
    video_url = urls.get("video_url") or urls.get("output_video_url")
    srt_url = urls.get("srt_url") or urls.get("output_srt_url")
    if not video_url:
        raise ValueError(f"Dubbing export for {job_id} carried no video URL: {exports}")

    video = await fetch_bytes(video_url)
    stored = storage.put(
        video,
        key=object_key(str(module_id), language, "dubbed", ".mp4"),
        content_type="video/mp4",
    )

    # ADR 0003: no dubbing response carries timings, so the exported SRT is the
    # only place the produced per-segment durations exist.
    fit: dict[str, Any] = {"timing_fit_rate": None, "error": "no SRT export to measure"}
    if srt_url:
        produced = parse_srt_timings((await fetch_bytes(srt_url)).decode("utf-8", "replace"))
        fit = timing_fit(cues(segments), produced)

    await write_artifact(
        db,
        module_id=module_id,
        language=language,
        kind="dubbed_video",
        stored=stored,
        meta={"job_id": job_id, "voice_id": DEFAULT_VOICE[language], "timing": fit},
    )
    return {"stage": "dub", "language": language, "job_id": job_id, "timing": fit}


# --- tts_summary --------------------------------------------------------------


async def tts_summary_task(
    db: AsyncSession,
    *,
    module_id: uuid.UUID,
    language: str,
    structured: Any,
    speak: Any,
    storage: Storage,
) -> dict[str, Any]:
    """A Claude-written five-minute summary, rendered by Bulbul.

    The summary is written from the APPROVED target-language segments, not from
    the English source: summarising the English and translating the summary
    would put text in front of an employee that no reviewer ever saw.
    """
    segments = await load_approved(db, module_id, language)
    payload = [{"seg_id": s.seg_id, "text": s.text, "locked": s.locked} for s in segments]
    from indic_platform.security.harden import wrap_untrusted
    from pydantic import BaseModel

    class SummaryScript(BaseModel):
        text: str
        rationale: str = ""

    script = await structured(
        system=stages.prompt_body("summary"),
        user=wrap_untrusted(
            json.dumps({"language": language, "segments": payload}, ensure_ascii=False)
        ),
        schema=SummaryScript,
        model=stages.ADAPT_MODEL,
    )
    audio = await speak(script.text, language=language, voice=DEFAULT_VOICE[language])
    stored = storage.put(
        audio,
        key=object_key(str(module_id), language, "summary", ".wav"),
        content_type="audio/wav",
    )
    await write_artifact(
        db,
        module_id=module_id,
        language=language,
        kind="summary_audio",
        stored=stored,
        meta={
            "words": len(script.text.split()),
            "rationale": script.rationale,
            "voice_id": DEFAULT_VOICE[language],
            "model": stages.ADAPT_MODEL,
            "prompt_version": stages.prompt_version("summary"),
        },
    )
    return {
        "stage": "tts_summary",
        "language": language,
        "words": len(script.text.split()),
        "bytes": stored.bytes,
    }


# --- package ------------------------------------------------------------------


async def write_artifact(
    db: AsyncSession,
    *,
    module_id: uuid.UUID,
    language: str,
    kind: str,
    stored: Stored,
    meta: dict[str, Any],
) -> None:
    """Upsert one artifact row. The key is (module, language, kind), so a re-run
    replaces the artifact rather than accumulating versions of it: the manifest
    describes what is delivered now, and the editorial history lives in
    `localizations`."""
    existing = await db.get(Artifact, (module_id, language, kind))
    if existing is None:
        db.add(
            Artifact(
                module_id=module_id,
                language=language,
                kind=kind,
                uri=stored.uri,
                sha256=stored.sha256,
                meta={**meta, "bytes": stored.bytes},
            )
        )
    else:
        existing.uri = stored.uri
        existing.sha256 = stored.sha256
        existing.meta = {**meta, "bytes": stored.bytes}


PROMPT_NAMES = ("adapt", "post_edit", "backtranslate", "qa_judge", "quiz", "summary")


def manifest_versions() -> dict[str, Any]:
    """Everything that shaped the delivered bytes.

    PRD D8 calls the manifest "the audit record of what was delivered in which
    version", so this has to name every input whose change would change the
    output: the terminology, each prompt by content hash, and each model.
    """
    return {
        "glossary": load_glossary().version,
        "prompts": {name: stages.prompt_version(name) for name in PROMPT_NAMES},
        "models": {
            "adapt": stages.ADAPT_MODEL,
            "post_edit": stages.POST_EDIT_MODEL,
            "judge": stages.JUDGE_MODEL,
            "translate": "mayura:v1",
            "dub": "sarvam-dubbing",
            "tts": "bulbul:v3",
        },
    }


async def build_manifest(
    db: AsyncSession, *, module_id: uuid.UUID, language: str
) -> dict[str, Any]:
    """The audit record of what was delivered in which version (PRD D8)."""
    module = await db.get(Module, module_id)
    if module is None:
        raise LookupError("Unknown module")
    segments = await load_approved(db, module_id, language)
    rows = (
        await db.scalars(
            select(Artifact).where(Artifact.module_id == module_id, Artifact.language == language)
        )
    ).all()
    dub = next((r for r in rows if r.kind == "dubbed_video"), None)
    return {
        "module_id": str(module_id),
        "title": module.title,
        "language": language,
        "segments": len(segments),
        "approved_versions": {str(s.seg_id): s.version for s in segments},
        "artifacts": {
            r.kind: {"uri": r.uri, "sha256": r.sha256, "bytes": r.meta.get("bytes")}
            for r in sorted(rows, key=lambda r: r.kind)
        },
        "versions": manifest_versions(),
        "timing": (dub.meta or {}).get("timing") if dub else None,
    }


async def package_task(
    db: AsyncSession, *, module_id: uuid.UUID, language: str, storage: Storage
) -> dict[str, Any]:
    manifest = await build_manifest(db, module_id=module_id, language=language)
    body = json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True).encode()
    stored = storage.put(
        body,
        key=object_key(str(module_id), language, "manifest", ".json"),
        content_type="application/json",
    )
    await write_artifact(
        db,
        module_id=module_id,
        language=language,
        kind="manifest",
        stored=stored,
        meta={"delivered": True},
    )
    module = await db.get(Module, module_id)
    if module is not None:
        module.status = "delivered"
    return {"stage": "package", "language": language, "manifest": manifest, "uri": stored.uri}
