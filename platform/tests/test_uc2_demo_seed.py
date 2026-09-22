"""The uc2 demo seeder (`training_localizer.demo_seed`).

The offline tests pin the shape: stable ids, every translation attached to a segment
that exists, LOCKED only where the glossary can say what locked means, and rows that
name no producer. The integration tests seed a real database under a namespace of
their own and read it back through the same function the reviewer console's API calls.
"""

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from training_localizer import demo_seed
from training_localizer.pipeline import LANGUAGES

#: The integration tests seed under this namespace and delete only what carries it.
#: Deleting the real `demo-seed:uc2:` modules would empty the console out from under
#: whoever last ran `make seed-uc2` against the same database.
FIXTURE = "uc2-demo-seed-test:"


@pytest.fixture(autouse=True)
def _non_prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "test")


# --- shaping -------------------------------------------------------------------


def test_ids_are_stable_and_namespaced() -> None:
    """A re-run must find its own modules; a test must never find the demo's."""
    assert demo_seed.module_uuid("sec-101") == demo_seed.module_uuid("sec-101")
    assert demo_seed.module_uuid("sec-101") != demo_seed.module_uuid("sec-102")
    assert demo_seed.module_uuid("sec-101") != demo_seed.module_uuid("sec-101", FIXTURE)
    dataset = demo_seed.build_dataset()
    assert [m.golden_id for m in dataset] == ["comp-201", "sec-101", "sec-102"]
    assert all(m.id == demo_seed.module_uuid(m.golden_id) for m in dataset)


def test_every_golden_segment_and_quiz_item_is_seeded() -> None:
    for module in demo_seed.build_dataset():
        assert len(module.segments) == 20, module.golden_id
        assert [s.seg_id for s in module.segments] == list(range(1, 21))
        assert len(module.quiz) == 10, module.golden_id


def test_translations_exist_only_where_the_golden_set_has_a_reference() -> None:
    """Ten of twenty per language: the honest state of the golden set, not a gap
    filled with a placeholder. The other ten show as untranslated in the console."""
    dataset = demo_seed.build_dataset()
    for module in dataset:
        for language in LANGUAGES:
            texts = module.translated(language)
            assert len(texts) == 10, (module.golden_id, language)
            known = {s.seg_id for s in module.segments}
            assert set(texts) <= known
            assert all(text.strip() for text in texts.values())
    # And a translation is a real translation, not the source echoed back.
    sec = next(m for m in dataset if m.golden_id == "sec-101")
    source = {s.seg_id: s.source_text for s in sec.segments}
    for (language, seg_id), text in sec.translations.items():
        assert text != source[seg_id], (language, seg_id)


def test_locked_only_where_an_approved_rendering_resolves() -> None:
    """The same rule `POST /modules` applies: a locked segment with no approved
    rendering has no defined target text, so it is seeded unlocked."""
    from training_localizer.terminology import load_glossary, resolve_locked_id

    glossary = load_glossary()
    locked_seen = 0
    for module in demo_seed.build_dataset():
        for segment in module.segments:
            resolved = resolve_locked_id(segment.source_text, glossary)
            if segment.locked:
                locked_seen += 1
                assert segment.locked_id == resolved and resolved is not None
            else:
                assert segment.locked_id is None
    assert locked_seen > 0, "no locked segment survived, so the LOCKED path is undemonstrated"


def test_seeded_locked_segments_match_their_approved_renderings() -> None:
    """A seeded LOCKED row is the golden reference, and the golden reference for every
    locked segment equals the approved rendering -- so the console shows the LOCKED
    path clean rather than flagging a mismatch nobody produced. Asserted, not assumed:
    a golden-set edit that broke this would show up here and not in a demo."""
    from training_localizer.review import locked_mismatch
    from training_localizer.terminology import load_glossary

    glossary = load_glossary()
    checked = 0
    for module in demo_seed.build_dataset():
        for segment in module.segments:
            if not segment.locked:
                continue
            for language in LANGUAGES:
                text = module.translations.get((language, segment.seg_id))
                assert text, (module.golden_id, segment.seg_id, language, "locked but unseeded")
                assert not locked_mismatch(text, segment.locked_id, language, glossary)
                checked += 1
    assert checked > 0


def test_seeded_rows_name_no_producer() -> None:
    """`model` and `prompt_version` are what a reader filters on to find machine
    output. A seeded row must not turn up in that search."""
    meta = demo_seed.stage_meta("abc123")
    assert meta["demo"] is True
    assert meta["model"] == "" and meta["prompt_version"] == ""
    assert meta["glossary_version"] == "abc123"
    assert demo_seed.CREATED_BY == "demo-seed"
    assert "backtranslate" not in demo_seed.SEEDED_STAGES


def test_seeding_is_refused_outside_a_known_non_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env in ("prod", "production", "prd", "", " "):
        monkeypatch.setenv("ENV", env)
        with pytest.raises(demo_seed.SeedRefused):
            demo_seed.guard_environment()
    for env in ("dev", "local", "test", "ci"):
        monkeypatch.setenv("ENV", env)
        demo_seed.guard_environment()


async def test_seed_itself_refuses_a_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the write path, not only in the CLI: passing `None` for the session is the
    assertion -- a `seed` that reached the database would raise something else."""
    monkeypatch.setenv("ENV", "prod")
    with pytest.raises(demo_seed.SeedRefused):
        await demo_seed.seed(None, demo_seed.build_dataset())  # type: ignore[arg-type]


def test_the_summary_tells_the_operator_where_to_look() -> None:
    summary = demo_seed.summarise(demo_seed.build_dataset())
    for entry in summary["modules"]:
        assert entry["console"] == f"/?module={entry['module_id']}"
        assert entry["translated"] == {lang: 10 for lang in LANGUAGES}


# --- persistence ---------------------------------------------------------------


def _skip_without_db() -> None:
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


async def _purge(factory: Any, ids: list[uuid.UUID]) -> None:
    from indic_platform.db.models import Localization, Module, QuizItem, Segment
    from sqlalchemy import delete

    async with factory() as db, db.begin():
        await db.execute(delete(Localization).where(Localization.module_id.in_(ids)))
        await db.execute(delete(QuizItem).where(QuizItem.module_id.in_(ids)))
        await db.execute(delete(Segment).where(Segment.module_id.in_(ids)))
        await db.execute(delete(Module).where(Module.id.in_(ids)))


@pytest.fixture
async def seeded() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    _skip_without_db()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ids = [m.id for m in demo_seed.build_dataset(namespace=FIXTURE)]
    try:
        await _purge(factory, ids)
        yield factory
    finally:
        try:
            await _purge(factory, ids)
        finally:
            await engine.dispose()


@pytest.mark.integration
async def test_the_console_reads_back_what_was_seeded(seeded: Any) -> None:
    """Through `pipeline.load_review`, which is what `GET /modules/{id}/review` calls."""
    from training_localizer.pipeline import load_review
    from training_localizer.review import review_rows, review_summary
    from training_localizer.terminology import load_glossary

    dataset = demo_seed.build_dataset(namespace=FIXTURE)
    async with seeded() as db:
        written = await demo_seed.seed(db, dataset)
        await db.commit()
    assert written["modules"] == 3
    assert written["segments"] == 60
    assert written["localizations"] == 3 * 3 * 10 * len(demo_seed.SEEDED_STAGES)
    assert written["quiz_items"] == 30

    sec = next(m for m in dataset if m.golden_id == "sec-101")
    async with seeded() as db:
        segments, stages = await load_review(db, sec.id, "hi-IN")
    rows = review_rows(
        segments=segments,
        post_edit=stages["post_edit"],
        backtranslate=stages["backtranslate"],
        approved=stages["approved"],
        language="hi-IN",
        glossary=load_glossary(),
    )
    assert len(rows) == 20
    translated = [r for r in rows if r.translation]
    assert len(translated) == 10
    # No QA score was invented: the console shows "not scored", not a number.
    assert all(r.qa_score is None for r in rows)
    assert all(not r.approved for r in rows)
    assert all(not r.locked_mismatch for r in rows)
    # And the response says who wrote the text, so a console reader can tell a
    # reference from machine output without opening the database.
    assert all(r.created_by == demo_seed.CREATED_BY for r in translated)
    assert all(r.as_json()["created_by"] == demo_seed.CREATED_BY for r in translated)
    assert all(r.created_by is None for r in rows if not r.translation)
    summary = review_summary(rows)
    assert summary["segments"] == 20
    assert summary["approved"] == 0
    assert summary["fidelity_mean"] is None
    assert summary["producers"] == [demo_seed.CREATED_BY]

    from training_localizer.pipeline import module_status

    async with seeded() as db:
        status = await module_status(db, sec.id)
    assert status["languages"]["hi-IN"]["post_edit"]["created_by"] == [demo_seed.CREATED_BY]
    assert status["languages"]["hi-IN"]["post_edit"]["version"] == 1
    assert "backtranslate" not in status["languages"]["hi-IN"]


@pytest.mark.integration
async def test_the_real_pipeline_supersedes_the_seed_where_it_runs(seeded: Any) -> None:
    """`--localize` writes a later version on top; `latest()` picks it per segment, so
    machine output replaces the reference where the pipeline ran and the reference
    stands where it has not. Written the way `pipeline.write_stage` writes, since the
    pipeline itself is not run here."""
    from training_localizer.pipeline import latest, next_version, write_stage

    dataset = demo_seed.build_dataset(namespace=FIXTURE)
    sec = next(m for m in dataset if m.golden_id == "sec-101")
    async with seeded() as db:
        await demo_seed.seed(db, dataset)
        await db.commit()
    async with seeded() as db:
        version = await next_version(db, sec.id, "hi-IN", "post_edit")
        assert version == 2
        await write_stage(
            db,
            module_id=sec.id,
            language="hi-IN",
            stage="post_edit",
            version=version,
            texts={1: "machine output for segment one"},
            meta={1: {"model": "test-model", "prompt_version": "t1", "change_log": []}},
            created_by="pipeline",
        )
        await db.commit()
    async with seeded() as db:
        rows = await latest(db, sec.id, "hi-IN", "post_edit")
    assert rows[1].version == 2 and rows[1].created_by == "pipeline"
    others = {seg_id: row for seg_id, row in rows.items() if seg_id != 1}
    assert others and all(row.version == 1 for row in others.values())
    assert all(row.created_by == demo_seed.CREATED_BY for row in others.values())


@pytest.mark.integration
async def test_seeding_twice_adds_nothing(seeded: Any) -> None:
    dataset = demo_seed.build_dataset(namespace=FIXTURE)
    async with seeded() as db:
        await demo_seed.seed(db, dataset)
        await db.commit()
    async with seeded() as db:
        again = await demo_seed.seed(db, dataset)
        await db.commit()
    assert again == {"modules": 0, "segments": 0, "localizations": 0, "quiz_items": 0, "skipped": 3}


@pytest.mark.integration
async def test_stored_stage_rows_say_demo_seed(seeded: Any) -> None:
    from indic_platform.db.models import Localization
    from sqlalchemy import select

    dataset = demo_seed.build_dataset(namespace=FIXTURE)
    async with seeded() as db:
        await demo_seed.seed(db, dataset)
        await db.commit()
    async with seeded() as db:
        rows = (
            await db.scalars(
                select(Localization).where(Localization.module_id.in_([m.id for m in dataset]))
            )
        ).all()
    assert rows
    for row in rows:
        assert row.created_by == demo_seed.CREATED_BY
        assert row.meta["demo"] is True
        assert row.meta["model"] == "" and row.meta["prompt_version"] == ""
        assert row.stage in demo_seed.SEEDED_STAGES
