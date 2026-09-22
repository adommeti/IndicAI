"""A demonstration module set for the reviewer console, seeded from the golden set.

The reviewer console (`ui/`) opens on `/?module=<id>` and needs a module with segments
and stage rows behind it. Producing those the real way means `POST /modules` and then
`POST /modules/{id}/localize`, which runs the five-stage pipeline against Sarvam and
Claude -- the right thing to demonstrate, and the wrong thing to require before a
console can be looked at. This module gives the console something to show without a
vendor call, and `--localize` then runs the real pipeline over the same modules so the
machine output lands beside the human references.

## What is real here and what is not

Real:

- the three modules and their sixty segments, from
  `platform/eval/golden/uc2_training/samples/` -- the same segments `make eval-uc2`
  scores;
- the translations, from `reference_translations.<language>.jsonl`: human-authored
  reference renderings, `status: draft`, for ten of each module's twenty segments. The
  other ten are seeded with no translation, which the console shows as untranslated.
  That is the honest state of the golden set, not a gap this module papers over;
- the quiz items, from `quiz_items.jsonl`;
- which segments are LOCKED, resolved against `approved_renderings.yaml` by the same
  `resolve_locked_id` the API uses on upload. A golden segment marked locked whose
  English matches no approved statement is seeded UNLOCKED, exactly as `POST /modules`
  would refuse it, rather than pushed down the pipeline with no defined target text.

Not real, and labelled on every row: `localizations.created_by` is `demo-seed`, and
`meta` carries `{"demo": true, "origin": "golden reference"}` with an empty `model`
and `prompt_version`, because no model produced the text. `backtranslate` rows are not
seeded at all -- a QA score is a judgement, and the console renders its absence as
"not scored" rather than this module inventing one.

Re-running is a no-op: module ids are derived from the golden ids (`module_uuid`), so
a second `make seed-uc2` finds them present and skips. `--localize` writes new stage
versions on top; `latest()` in the pipeline picks the highest version per segment, so
the real output supersedes the seed where both exist and the reference stands where
the pipeline has not run.

Usage: `make seed-uc2`, or `uv run python -m training_localizer.demo_seed [--dry-run]
[--localize]`.
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from indic_platform.db.models import Module, QuizItem, Segment
from indic_platform.eval.runners import run_uc2
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from training_localizer import auth
from training_localizer.pipeline import LANGUAGES, write_stage
from training_localizer.terminology import Glossary, load_glossary, resolve_locked_id

#: Module ids are derived from this plus the golden id, so they are stable across runs
#: and a test can seed under a namespace of its own that it is allowed to delete.
NAMESPACE = "demo-seed:uc2:"

#: Written to `localizations.created_by` on every seeded stage row.
CREATED_BY = "demo-seed"

#: The stages a reference translation stands in for. `translate` because that is what
#: the text is; `post_edit` because that is the stage the reviewer console reads. Not
#: `adapt` (the reference was not adapted from anything) and not `backtranslate` (see
#: the module docstring).
SEEDED_STAGES = ("translate", "post_edit")

#: Titles for the golden modules, which carry only an id. Descriptive, not inventive:
#: each is the module's own first segment paraphrased.
TITLES = {
    "sec-101": "Security awareness (annual, India)",
    "sec-102": "Handling client data and personal data",
    "comp-201": "Market conduct: insider trading and conflicts of interest",
}

#: Where `--localize` finds the running API. The host-side port from docker-compose.
DEFAULT_API = "http://127.0.0.1:8002"

#: The same allow-list the app's own dev bypass uses (`training_localizer.auth`), so a
#: stack where the console opens without SSO is exactly the set of stacks that may hold
#: demonstration rows, and neither list can drift from the other.
NON_PROD_ENVS = auth.NON_PROD_ENVS


class SeedRefused(RuntimeError):
    """The environment is not one where demonstration rows may be written."""


def guard_environment() -> None:
    """An allow-list, so `ENV=production`, `ENV=prd` and an unset ENV all fail closed."""
    env = os.environ.get("ENV", "").strip().lower()
    if env not in NON_PROD_ENVS:
        raise SeedRefused(
            f"refusing to seed demo rows with ENV={env!r}: set ENV to one of "
            f"{sorted(NON_PROD_ENVS)}"
        )


def module_uuid(golden_id: str, namespace: str = NAMESPACE) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, namespace + golden_id)


@dataclass(frozen=True)
class SeededSegment:
    seg_id: int
    start_ms: int
    end_ms: int
    source_text: str
    locked: bool
    #: Set only when `locked` and the English resolved to an approved statement.
    locked_id: str | None


@dataclass(frozen=True)
class SeededModule:
    golden_id: str
    id: uuid.UUID
    title: str
    segments: tuple[SeededSegment, ...]
    #: (language, seg_id) -> reference text. Only where the golden set has one.
    translations: dict[tuple[str, int], str] = field(default_factory=dict)
    quiz: tuple[dict[str, Any], ...] = ()

    def translated(self, language: str) -> dict[int, str]:
        return {seg: text for (lang, seg), text in self.translations.items() if lang == language}


def build_dataset(
    *, namespace: str = NAMESPACE, glossary: Glossary | None = None
) -> list[SeededModule]:
    """Shape every golden module. Pure: no database, no network."""
    lex = glossary if glossary is not None else load_glossary()
    by_module: dict[str, list[run_uc2.Segment]] = {}
    for segment in run_uc2.load_segments():
        by_module.setdefault(segment.module_id, []).append(segment)
    references = run_uc2.load_references()
    quiz = run_uc2.load_quiz()

    out: list[SeededModule] = []
    for golden_id in sorted(by_module):
        segments = []
        for s in sorted(by_module[golden_id], key=lambda x: x.seg_id):
            locked_id = resolve_locked_id(s.source_text, lex) if s.locked else None
            segments.append(
                SeededSegment(
                    seg_id=s.seg_id,
                    start_ms=s.start_ms,
                    end_ms=s.end_ms,
                    source_text=s.source_text,
                    # A locked segment with no approved rendering has no defined target
                    # text; the upload API refuses it, and this seeds it unlocked.
                    locked=bool(s.locked and locked_id),
                    locked_id=locked_id,
                )
            )
        known = {s.seg_id for s in segments}
        translations = {
            (r.language, r.seg_id): r.reference_text
            for r in references
            if r.module_id == golden_id and r.seg_id in known and r.reference_text.strip()
        }
        items = tuple(
            {
                "language": q.language,
                "item_id": q.item_id,
                "seg_id": q.seg_id,
                "question": q.question,
                "options": list(q.options),
                "answer": q.answer,
                "rationale": q.rationale,
            }
            for q in quiz
            if q.module_id == golden_id and q.seg_id in known
        )
        out.append(
            SeededModule(
                golden_id=golden_id,
                id=module_uuid(golden_id, namespace),
                title=TITLES.get(golden_id, golden_id),
                segments=tuple(segments),
                translations=translations,
                quiz=items,
            )
        )
    return out


def stage_meta(glossary_version: str) -> dict[str, Any]:
    """What a seeded stage row records about itself.

    The same keys a pipeline row carries, with the two that name a producer left
    empty: nothing generated this text, and a reader filtering rows by `model` must
    not find a vendor here.
    """
    return {
        "demo": True,
        "origin": "golden reference translation",
        "reference_status": "draft",
        "model": "",
        "prompt_version": "",
        "glossary_version": glossary_version,
        "change_log": [],
    }


async def seed(
    session: AsyncSession, dataset: Sequence[SeededModule], *, glossary: Glossary | None = None
) -> dict[str, int]:
    """Write the dataset. Stage rows go through the pipeline's own `write_stage`.

    The guard is checked here, on the write path, not only in `main`: this module
    ships inside the uc2 image and anything that can import it can call this.
    """
    guard_environment()
    lex = glossary if glossary is not None else load_glossary()
    written = {"modules": 0, "segments": 0, "localizations": 0, "quiz_items": 0, "skipped": 0}
    for module in dataset:
        if await session.get(Module, module.id) is not None:
            written["skipped"] += 1
            continue
        session.add(
            Module(id=module.id, title=module.title, source_lang="en-IN", status="uploaded")
        )
        await session.flush()
        for s in module.segments:
            session.add(
                Segment(
                    module_id=module.id,
                    seg_id=s.seg_id,
                    start_ms=s.start_ms,
                    end_ms=s.end_ms,
                    source_text=s.source_text,
                    locked=s.locked,
                )
            )
            written["segments"] += 1
        for language in LANGUAGES:
            texts = module.translated(language)
            if not texts:
                continue
            meta = {seg_id: stage_meta(lex.version) for seg_id in texts}
            for stage in SEEDED_STAGES:
                await write_stage(
                    session,
                    module_id=module.id,
                    language=language,
                    stage=stage,
                    version=1,
                    texts=texts,
                    meta=meta,
                    created_by=CREATED_BY,
                )
                written["localizations"] += len(texts)
        for item in module.quiz:
            session.add(QuizItem(module_id=module.id, approved=False, **item))
            written["quiz_items"] += 1
        written["modules"] += 1
    await session.flush()
    return written


def summarise(dataset: Sequence[SeededModule]) -> dict[str, Any]:
    return {
        "modules": [
            {
                "golden_id": m.golden_id,
                "module_id": str(m.id),
                "segments": len(m.segments),
                "locked": sum(1 for s in m.segments if s.locked),
                "translated": {lang: len(m.translated(lang)) for lang in LANGUAGES},
                "quiz_items": len(m.quiz),
                "console": f"/?module={m.id}",
            }
            for m in dataset
        ]
    }


def localize(dataset: Sequence[SeededModule], *, api: str = DEFAULT_API) -> list[dict[str, Any]]:
    """Ask the running API to run the real pipeline over each seeded module.

    Goes through the API rather than calling `localize.delay` from here, so the work
    is queued and executed exactly as a reviewer pressing the button would have it --
    same broker, same worker, same container environment. Costs money: Sarvam for the
    translation and Claude for adapt, post-edit, QA and quiz, roughly Rs 20 a module.

    A module the API refused or could not be reached for is returned with an `error`
    key rather than raised, so one refusal does not hide the other modules' task ids;
    `main` turns any such entry into a non-zero exit.
    """
    results = []
    for module in dataset:
        request = urllib.request.Request(
            f"{api}/modules/{module.id}/localize", method="POST", data=b""
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                results.append(json.loads(response.read()))
        except urllib.error.HTTPError as exc:
            results.append(
                {"module_id": str(module.id), "error": exc.code, "detail": exc.read().decode()}
            )
        except urllib.error.URLError as exc:
            results.append({"module_id": str(module.id), "error": str(exc.reason)})
    return results


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the uc2 reviewer console for a demo.")
    parser.add_argument("--dry-run", action="store_true", help="shape and print; write nothing")
    parser.add_argument(
        "--localize",
        action="store_true",
        help="after seeding, run the real pipeline over the modules through the API (costs money)",
    )
    parser.add_argument("--api", default=os.environ.get("UC2_API_URL", DEFAULT_API))
    args = parser.parse_args(argv)

    dataset = build_dataset()
    if args.dry_run:
        print(json.dumps(summarise(dataset), indent=2))
        return 0
    try:
        guard_environment()
    except SeedRefused as refused:
        print(str(refused))
        return 2
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set; `make bootstrap` writes it to .env.stack")
        return 2

    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            written = await seed(session, dataset)
            await session.commit()
    except OperationalError:
        print(f"could not connect to the database at {make_url(url).render_as_string()}")
        print("start it with `make stack-core` and apply migrations with `make migrate`")
        return 2
    finally:
        await engine.dispose()

    report: dict[str, Any] = {"written": written, **summarise(dataset)}
    if args.localize:
        print(
            f"localize: {len(dataset)} module(s) x {len(LANGUAGES)} languages through "
            f"{args.api} -- Sarvam + Claude, roughly Rs 20 per module",
            file=sys.stderr,
        )
        report["localize"] = localize(dataset, api=args.api)
    print(json.dumps(report, indent=2))
    not_queued = [entry for entry in report.get("localize", []) if "error" in entry]
    if not_queued:
        # A 401 here means the API is running without the dev bypass; a connection
        # error means it is not running. Either way nothing was queued, and saying so
        # with an exit code is the difference between "make demo-localize" doing
        # nothing quietly and the operator finding out.
        print(
            f"localize: {len(not_queued)} of {len(dataset)} module(s) were not queued "
            "(see the `error` entries above; a 401 means uc2-api has no dev bypass, "
            "see docs/DEMO.md)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
