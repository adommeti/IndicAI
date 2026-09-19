"""The demo seeder (`comms_surveillance.demo_seed`).

Two things are being pinned here, and only the second one needs a database.

The first is honesty. These rows go into a hash-chained compliance trail and
they were not produced by a model, so the tests that matter most are the ones
asserting that no seeded row names a vendor model, that every row says `demo`
in its own payload, and that the seeder refuses to run anywhere but a known
non-production environment. A demo dataset that is indistinguishable from real
detector output is not a demo dataset, it is contamination.

The second is that a seeded queue is a *real* queue: written through
`audit.append`, verifying clean afterwards, and read back by the same function
the reviewer console calls.
"""

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from comms_surveillance import audit, demo_renderings, demo_seed
from comms_surveillance.lexicon import matcher
from indic_platform.eval.runners.run_uc3 import Transcript
from sqlalchemy.dialects import postgresql

#: A fixed clock, so every assertion below is about the seeder rather than about
#: what day the test ran.
AS_OF = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)

ROOT = Path(__file__).resolve().parents[2]

#: The integration tests seed under their own prefix and delete it afterwards.
#: Cleaning up by `demo/uc3/%` would delete the demo queue out from under
#: whoever last ran `make seed-uc3` against the same database.
FIXTURE = "uc3-demo-seed-test/"


@pytest.fixture(autouse=True)
def _non_prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "test")


def _dataset(count: int = 24, **kwargs: Any) -> list[demo_seed.SeededCall]:
    return demo_seed.build_dataset(count=count, as_of=AS_OF, **kwargs)


# --- honesty -------------------------------------------------------------------


def test_no_seeded_row_claims_a_vendor_model_produced_it() -> None:
    """The load-bearing assertion of this whole module.

    `model` on an `analysis_runs` row is what an auditor reads to decide who is
    accountable for a finding. A fabricated row carrying `claude-sonnet-5` would
    put text nobody generated into an evidence chain under a name that implies
    somebody did -- and the chain would verify clean, because the hash covers
    the lie as faithfully as it covers the truth.

    `english_rendering_source` is excluded from the sweep and checked separately
    below: it names a model on purpose, because the translation beside a span
    really was produced by one. A finding and a gloss are different claims and
    only one of them is fabricated here.
    """
    vendor = re.compile(r"claude|sonnet|haiku|saaras|bulbul|mayura|gpt", re.IGNORECASE)
    assert not vendor.search(demo_seed.SEED_MODEL)
    assert not vendor.search(demo_seed.SEED_PROMPT_VERSION)
    for call in _dataset():
        payload = demo_seed.run_output(call)
        assert payload["demo"] is True
        assert payload["source"].startswith("golden:")
        assert "demo_seed" in payload["note"]
        # Scanned field by field rather than over `str(payload)`: the payload
        # embeds transcript text, and a golden call that happened to contain the
        # word "claude" would fail a whole-document sweep for no reason.
        for flag in payload["flags"]:
            assert not vendor.search(flag["reasoning"]), call.golden_id


def test_the_english_rendering_is_a_recorded_translation_not_an_invention() -> None:
    """A gloss beside a Telugu evidence span is a claim about what was said.

    It has to be traceable to something that produced it, so every one comes from
    the checked-in file and the row says which function and model made it. A
    translation this repository wrote by hand would be untraceable English inside
    an evidence record.
    """
    store = demo_renderings.load()
    assert store, "demo/renderings.json is missing; regenerate with demo_renderings --write"
    assert demo_renderings.provenance()["renderer"] == (
        "comms_surveillance.detector.render_english"
    )
    # Per span, not once for the file: a later run translates only what is
    # missing, so a file-level stamp would relabel every older translation with
    # whatever model happened to run last.
    for entry in store.values():
        assert entry["english"] and entry["model"] and entry["generated_at"]

    dataset = _dataset(count=48)
    # A floor on renderings actually applied, checked before the loop below.
    # Without it the loop's central assertion is satisfied by `"" == ""` and
    # passes when every single lookup misses -- which it did, and which is
    # exactly the inert assertion this file exists to avoid.
    applied = [flag for call in dataset for flag in call.flags if flag.rendering_model]
    assert len(applied) >= 20, f"only {len(applied)} flags carry a translation"

    seen_non_english = False
    for call in dataset:
        for flag in call.flags:
            if call.language in demo_renderings.NATIVE_ENGLISH:
                # An English call renders itself, so no translator is named.
                assert flag.english_rendering == flag.evidence_span
                assert flag.rendering_model == ""
                continue
            seen_non_english = True
            entry = store.get(demo_renderings.span_key(flag.evidence_span), {})
            # Absent is allowed -- one Hinglish turn came back untranslated and
            # was rejected rather than stored -- but never invented.
            assert flag.english_rendering == entry.get("english", "")
            assert flag.rendering_model == entry.get("model", "")
        source = demo_seed.run_output(call)["english_rendering_source"]
        if any(flag.rendering_model for flag in call.flags):
            assert source["renderer"] == "comms_surveillance.detector.render_english"
            assert source["models"] == sorted(
                {f.rendering_model for f in call.flags if f.rendering_model}
            )
        else:
            assert source == {}
    assert seen_non_english, "the sample had no non-English flag, so this proved nothing"


def test_a_missing_rendering_is_left_empty_rather_than_guessed() -> None:
    """With no renderings file the seeder must degrade, not improvise.

    Empty is what `detector.render_english` itself returns when its call fails,
    and the console renders it as "no English rendering was produced". The row
    then claims no translator, because none ran.
    """
    dataset = demo_seed.build_dataset(count=24, as_of=AS_OF, renderings={})
    # The mirror of the floor above: with the store empty, nothing may be
    # rendered. Together the two pin the difference between "the lookup found
    # nothing" and "there was nothing to find".
    assert any(
        call.language not in demo_renderings.NATIVE_ENGLISH and call.flags for call in dataset
    ), "no non-English call with a flag in the sample, so this would prove nothing"
    for call in dataset:
        if call.language in demo_renderings.NATIVE_ENGLISH:
            continue
        assert all(flag.english_rendering == "" for flag in call.flags)
        assert all(flag.rendering_model == "" for flag in call.flags)
        assert demo_seed.run_output(call)["english_rendering_source"] == {}


def test_every_seeded_disposition_is_marked_as_fabricated() -> None:
    """A disposition is a human verdict, so every seeded one is made up.

    Including the `false_positive` ones, which say nothing about how precise the
    detector is. The note travels on the row rather than living only in the
    module docstring, so it is visible in the detail pane a customer is looking
    at.
    """
    assert "Not a real review decision" in demo_seed.DEMO_NOTE
    assert "precision" in demo_seed.DEMO_NOTE
    for reviewer in demo_seed.REVIEWERS:
        # RFC 2606 reserves `.test`; it can never resolve to a real subject.
        assert reviewer.endswith("example.test")


def test_seeding_is_refused_outside_a_known_non_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ALLOW-list, so a typo fails closed.

    These rows cannot be removed once written -- the app role holds no DELETE --
    so a production run is permanent. `ENV=production` and `ENV=prd` are the
    plausible typos that a `!= "prod"` guard would have waved through.
    """
    for env in ("prod", "production", "prd", "PROD", "", " "):
        monkeypatch.setenv("ENV", env)
        with pytest.raises(demo_seed.SeedRefused):
            demo_seed.guard_environment()
    for env in ("dev", "local", "test", "ci"):
        monkeypatch.setenv("ENV", env)
        demo_seed.guard_environment()


async def test_seed_itself_refuses_a_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard has to be on the write path, not only on the argument parser.

    This module ships inside the production uc3 image, so the CLI is not the
    only way to reach `seed`. It must refuse before it touches a session --
    which is why passing `None` for one is safe here and is the assertion: a
    `seed` that got as far as the database would raise AttributeError, not
    SeedRefused.
    """
    monkeypatch.setenv("ENV", "prod")
    with pytest.raises(demo_seed.SeedRefused):
        await demo_seed.seed(None, _dataset(count=2), lexicon_version="test")  # type: ignore[arg-type]


def test_prompt_version_is_empty_because_no_prompt_ran() -> None:
    """Naming a prompt version would imply that prompt produced the output.

    `policy_version` and `lexicon_version`, by contrast, describe files that
    really were read -- the policy document and the lexicon that Stage 0 really
    scanned with -- so those are recorded truthfully.
    """
    assert demo_seed.SEED_PROMPT_VERSION == ""


# --- shaping -------------------------------------------------------------------


def test_the_dataset_is_deterministic() -> None:
    """A queue that reshuffles between runs is a queue no screenshot survives."""
    assert _dataset() == _dataset()


def test_the_requested_count_is_what_comes_back() -> None:
    for count in (1, 5, 24, 48):
        dataset = _dataset(count=count)
        assert len(dataset) == count
        assert len({c.golden_id for c in dataset}) == count


def test_every_seeded_flag_quotes_its_own_call() -> None:
    """Evidence that cannot be found in the transcript is not reviewable.

    `instruction_like_content` is the one exemption, matching
    `detector.verify`: that flag reports that the call addressed the reviewing
    system, and it is still quoted from a real turn here.
    """
    for call in _dataset(count=48):
        body = "\n".join(segment.text for segment in call.segments)
        for flag in call.flags:
            assert flag.evidence_span in body, f"{call.golden_id}/{flag.category}"
            assert any(
                flag.evidence_span in s.text and s.speaker == flag.speaker for s in call.segments
            ), f"{call.golden_id}/{flag.category} is attributed to the wrong speaker"


def test_a_flag_whose_evidence_is_not_in_the_transcript_fails_the_build() -> None:
    """The verifier guard in `build_dataset` is load-bearing, not decorative.

    Without it a bad label -- or a future change that mangles a span -- would
    reach the queue as a flag quoting text that is not in the call, which is the
    one thing `detector.verify` exists to stop.
    """
    broken = Transcript.model_validate(
        {
            "id": "uc3-tp-fabricated-01",
            "language_mix": "en-IN",
            "class": "true_positive",
            "segments": [
                {"speaker": "SPEAKER_00", "start_ms": 0, "end_ms": 1000, "text": "All clear here."}
            ],
            "labels": [
                {
                    "category": "guaranteed_returns",
                    "severity": "high",
                    "evidence_span": "a sentence nobody in this call said",
                    "speaker": "SPEAKER_00",
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="verifier refuses"):
        demo_seed.build_dataset(count=1, as_of=AS_OF, transcripts=[broken])


def test_the_headline_call_shows_an_injection_and_the_violation_it_tried_to_hide() -> None:
    """`uc3-adv-01` is the demo's opening and must never fall out of the sample.

    A speaker tells the reviewing system to mark the call clean, then commits
    the violation. Both appear: the injection as a security signal, the
    violation as a policy finding. If only the injection survived, the demo
    would be showing the exact failure the app exists to prevent.
    """
    head = _dataset()[0]
    assert head.golden_id == "uc3-adv-01"
    categories = {flag.category for flag in head.flags}
    assert "instruction_like_content" in categories
    assert "guaranteed_returns" in categories


def test_the_injection_flag_is_found_in_every_language() -> None:
    """The attack turn is identified by `run_uc3.strip_attack`, not by an English
    marker list of this module's own -- so it holds for Hindi, Telugu, Tamil and
    Hinglish, where a list written here would quietly have been English-only."""
    dataset = _dataset(count=48)
    languages = {
        call.language for call in dataset if any(flag.origin == "injection" for flag in call.flags)
    }
    assert languages == {"en-IN", "hi-IN", "hi-Latn", "ta-IN", "te-IN"}


def test_the_queue_covers_every_policy_category_and_language() -> None:
    dataset = _dataset(count=48)
    assert {call.language for call in dataset} == {"en-IN", "hi-IN", "hi-Latn", "ta-IN", "te-IN"}
    categories = {flag.category for call in dataset for flag in call.flags}
    assert set(matcher.CATEGORIES) <= categories


def test_the_queue_has_open_flags_as_well_as_decided_ones() -> None:
    """A queue with nothing left to review demonstrates a finished job, not a tool."""
    dataset = _dataset(count=48)
    flags = [flag for call in dataset for flag in call.flags]
    assert sum(1 for f in flags if not f.dispositions) >= 5
    assert sum(1 for f in flags if f.dispositions) >= 5
    # At least one flag carries a second, later decision: a changed mind is a new
    # row, never an edit, and that is the argument for an append-only trail.
    assert any(len(f.dispositions) > 1 for f in flags)


def test_nothing_is_timestamped_in_the_future() -> None:
    """A disposition dated after the moment you are looking at it reads as a
    broken clock, not as fresh data.

    Every row hung off a call is timestamped forward of it -- the analysis run
    by minutes, a second disposition by half a day -- so the spread has to start
    far enough back that the newest call's last row still lands in the past.
    """
    assert demo_seed.NEWEST_CALL_AGE_H > demo_seed.LATEST_DISPOSITION_H
    newest = max(call.recorded_at for call in _dataset(count=48))
    assert newest + timedelta(hours=demo_seed.LATEST_DISPOSITION_H) < AS_OF


def test_escalation_reasons_come_from_the_real_combine_rule() -> None:
    """`escalation_reasons` is E5's rule firing, not a string written here."""
    dataset = _dataset(count=48)
    reasons = {reason for call in dataset for reason in call.escalation_reasons}
    assert any(r.startswith("triage:") for r in reasons)
    assert any(call.stage == "triage" for call in dataset)
    assert any(call.stage == "deep_analysis" for call in dataset)
    for call in dataset:
        assert bool(call.escalation_reasons) == (call.stage == "deep_analysis")


def test_a_connection_failure_never_prints_the_password() -> None:
    """The one-line message `main` prints on an unreachable database.

    `render_as_string` hides the password by default, which is the whole reason
    it is used instead of the raw URL -- and a default is exactly the kind of
    thing a later edit turns off without noticing.
    """
    from sqlalchemy.engine import make_url

    url = "postgresql+psycopg://platform:hunter2@db:5432/platform"  # pragma: allowlist secret
    rendered = make_url(url).render_as_string()
    assert "hunter2" not in rendered
    assert "***" in rendered


# --- the exclusion, without a database -----------------------------------------


def test_every_measuring_query_carries_the_demo_predicate() -> None:
    """Compiled SQL, so a statement that loses the filter fails here rather than
    in whatever dashboard someone is looking at.

    Three of them, and they are reached by four different routes: `flag_statement`
    serves `/metrics/precision` twice (by category and over time),
    `qa_sample_statement` serves the false-negative estimate, and
    `api.flag_summaries(qa_sample=True)` builds its own join for the lead's
    stream -- which is exactly the one the first version of this exclusion
    missed.
    """
    from comms_surveillance import api, metrics

    statements = {
        "flag_statement": metrics.flag_statement(None, None),
        "qa_sample_statement": metrics.qa_sample_statement(None),
        "queue_statement(qa_sample=True)": api.queue_statement(qa_sample=True),
    }
    for name, statement in statements.items():
        # Compiled against the real dialect but with the parameters left bound:
        # `literal_binds` cannot render a JSONB value, and the marker is one.
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        assert "analysis_runs" in sql, name
        assert "NOT (analysis_runs.output @>" in sql, f"{name} lost the demo predicate"
        assert metrics.DEMO_MARKER in compiled.params.values(), name


def test_demo_free_refuses_a_statement_that_has_not_joined_the_runs() -> None:
    """Applied without the join it is a cross join, and a silent one.

    The compiled SQL still says `analysis_runs`, so the predicate test above
    would pass while every count was multiplied by the row count of a table
    designed only to grow.
    """
    from comms_surveillance import metrics
    from indic_platform.db.models import Flag
    from sqlalchemy import select

    with pytest.raises(ValueError, match="join analysis_runs"):
        metrics.demo_free(select(Flag.id))
    # And still accepts one that did join, however deeply nested.
    metrics.demo_free(metrics.flag_statement(None, None))


def test_the_reviewer_queue_keeps_the_demo_rows() -> None:
    """The other half of the rule, and the easier one to lose by tidying.

    Excluding demo rows from the queue as well would be consistent and wrong:
    the queue is what the seed exists to fill.
    """
    from comms_surveillance import api, metrics

    compiled = api.queue_statement().compile(dialect=postgresql.dialect())
    assert "@>" not in str(compiled)
    assert "analysis_runs" not in str(compiled)
    assert metrics.DEMO_MARKER not in compiled.params.values()


def test_the_grafana_panels_that_measure_carry_it_too() -> None:
    """The dashboard is a second implementation of the same numbers in raw SQL.

    Nothing stops it drifting from `metrics.py` except this: the API exclusion
    was in place and green while the Grafana precision panel was still
    computing the fabricated figure from the same tables.
    """
    dashboard = json.loads((ROOT / "infra/grafana/dashboards/uc3.json").read_text(encoding="utf-8"))

    def reads_flags(sql: str) -> bool:
        # Loose on purpose: a panel rewritten as `from flags as f` or onto one
        # line must still be caught, or this guard silently stops guarding while
        # the count below still holds.
        return bool(re.search(r"\bfrom\s+flags\b", sql, re.IGNORECASE))

    reading_flags = {
        panel["title"]: panel
        for panel in dashboard["panels"]
        if any(reads_flags(t.get("rawSql") or "") for t in panel.get("targets", []))
    }
    assert sorted(reading_flags) == [
        "Flags raised per day by category",
        "Review queue by latest disposition",
        "Reviewer-decided precision by category",
    ]
    for title, panel in reading_flags.items():
        for target in panel["targets"]:
            sql = target.get("rawSql") or ""
            if not reads_flags(sql):
                continue
            assert "join analysis_runs r on r.id = f.run_id" in sql, title
            assert "not (r.output @> '{\"demo\": true}')" in sql, title
        assert "demo_seed are excluded" in panel["description"], title


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


async def _purge(factory: Any) -> None:
    """Delete this file's rows, child-first, and nothing else's."""
    from indic_platform.db.models import (
        AnalysisRun,
        Call,
        Disposition,
        Flag,
        TranscriptSegment,
    )
    from sqlalchemy import delete, select

    async with factory() as db, db.begin():
        ids = list(
            (await db.scalars(select(Call.id).where(Call.source_key.like(f"{FIXTURE}%")))).all()
        )
        if not ids:
            return
        flag_ids = select(Flag.id).where(Flag.call_id.in_(ids))
        await db.execute(delete(Disposition).where(Disposition.flag_id.in_(flag_ids)))
        await db.execute(delete(Flag).where(Flag.call_id.in_(ids)))
        await db.execute(delete(AnalysisRun).where(AnalysisRun.call_id.in_(ids)))
        await db.execute(delete(TranscriptSegment).where(TranscriptSegment.call_id.in_(ids)))
        await db.execute(delete(Call).where(Call.id.in_(ids)))


async def _seeded_call_ids(db: Any) -> set[uuid.UUID]:
    """The ids of this file's calls, so an assertion cannot reach another suite's."""
    from indic_platform.db.models import Call
    from sqlalchemy import select

    return set((await db.scalars(select(Call.id).where(Call.source_key.like(f"{FIXTURE}%")))).all())


def _mine(rows: list[dict[str, Any]], call_ids: set[uuid.UUID]) -> list[dict[str, Any]]:
    """This file's rows out of a queue response.

    `flag_summaries` renders `call_id` as a string (`api._summary`) while the
    column is a UUID, so the comparison has to convert. Comparing the two
    directly is silently always false -- an empty result that looks like a
    passing filter.
    """
    return [row for row in rows if uuid.UUID(row["call_id"]) in call_ids]


@pytest.fixture
async def seeded() -> AsyncIterator[Any]:
    """A session factory over a database this test may seed and then restore.

    The anchors are deliberately not snapshotted: `audit.append` does not move
    an anchor (only `run_chain_verify(update_anchors=True)` does), and the
    teardown removes exactly the rows the test added, so each chain's head and
    row count come back to what they were. The verifications below therefore run
    with `check_anchor=False` -- while the test's rows are present the stored
    counts legitimately disagree, and asserting on that would be asserting on
    the fixture.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    _skip_without_db()
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await _purge(factory)
        yield factory
    finally:
        try:
            await _purge(factory)
        finally:
            await engine.dispose()


@pytest.mark.integration
async def test_seeding_leaves_every_chain_verifying_clean(seeded: Any) -> None:
    """The whole claim in one test: these are ordinary rows.

    Written through `audit.append` under the same advisory lock as a row from
    the API, so the nightly `uc3.chain_verify` has nothing to say about them. A
    seeder that inserted around the side would break the chain for every row
    written after it -- and the break would surface as a tampering alert on
    somebody else's data.
    """
    dataset = demo_seed.build_dataset(count=12, as_of=AS_OF, key_prefix=FIXTURE)
    async with seeded() as db:
        written = await demo_seed.seed(db, dataset, lexicon_version="test")
        await db.commit()
    assert written["calls"] == 12
    assert written["flags"] > 0
    assert written["dispositions"] > 0

    async with seeded() as db:
        results = await audit.verify_all(db, check_anchor=False)
        # And again the way the nightly `uc3.chain_verify` runs it. Appends only
        # ever extend a chain, so a stored anchor's head must still be reachable
        # and its row count must not exceed what is there -- which is the claim
        # the README makes and which `check_anchor=False` alone does not test.
        anchored = await audit.verify_all(db, check_anchor=True)
    assert results, "verify_all reported on no chains at all"
    for result in results:
        assert result.ok, f"{result.table}: {result.reason}"
    for result in anchored:
        assert result.ok, f"{result.table}: {result.reason}"
        assert result.anchor_ok is not False, f"{result.table}: seeding invalidated its anchor"


@pytest.mark.integration
async def test_seeding_twice_adds_nothing(seeded: Any) -> None:
    """`make seed-uc3` is safe to run again.

    `Call.source_key` is unique, so the second pass would otherwise fail on a
    constraint -- or, worse, succeed under a different key and append a second
    copy of every flag to a chain that cannot be edited afterwards.
    """
    dataset = demo_seed.build_dataset(count=6, as_of=AS_OF, key_prefix=FIXTURE)
    async with seeded() as db:
        await demo_seed.seed(db, dataset, lexicon_version="test")
        await db.commit()
    async with seeded() as db:
        again = await demo_seed.seed(db, dataset, lexicon_version="test")
        await db.commit()
    assert again == {
        "calls": 0,
        "segments": 0,
        "runs": 0,
        "flags": 0,
        "dispositions": 0,
        "skipped": 6,
    }


@pytest.mark.integration
async def test_the_console_reads_back_what_was_seeded(seeded: Any) -> None:
    """Read through `api.flag_summaries` and `api.flag_detail` -- the functions the
    reviewer console actually calls -- rather than by querying the tables, so this
    fails if the seeded shape is not the shape the UI renders.

    Scoped to this test's own calls. CI's database is shared and other suites
    leave rows behind, including a flag with no transcript at all; asserting
    over `flag_summaries()` unfiltered would be asserting on their data.
    """
    from comms_surveillance.api import flag_detail, flag_summaries

    dataset = demo_seed.build_dataset(count=12, as_of=AS_OF, key_prefix=FIXTURE)
    async with seeded() as db:
        await demo_seed.seed(db, dataset, lexicon_version="test")
        await db.commit()
        call_ids = await _seeded_call_ids(db)

    async with seeded() as db:
        # A limit well above anything the suite can leave behind: the scoping
        # below runs in Python, so a seeded row that fell off the SQL page would
        # make this flaky rather than wrong.
        rows = _mine(await flag_summaries(db, limit=5000), call_ids)
        assert rows, "none of the seeded flags came back through the queue endpoint"
        assert len(rows) == sum(len(call.flags) for call in dataset)

        # Worst first, which is what a reviewer's day depends on. Asserted over
        # this test's rows only, so another suite's flag cannot reorder it.
        order = {"high": 0, "medium": 1, "low": 2}
        assert [order[r["severity"]] for r in rows] == sorted(order[r["severity"]] for r in rows)

        decided = [r for r in rows if r["disposition"]]
        assert decided, "no seeded flag came back carrying its latest disposition"

        detail = await flag_detail(db, uuid.UUID(rows[0]["flag_id"]))
        assert detail["transcript"], "a flag with no transcript cannot be reviewed"
        assert detail["evidence_span"] in "\n".join(s["text"] for s in detail["transcript"])
        assert detail["policy_clause"] or detail["category"] == "instruction_like_content"


@pytest.mark.integration
async def test_the_stored_columns_say_demo_not_a_vendor_model(seeded: Any) -> None:
    """The module's central claim, read back from the database.

    Everything else asserts what `build_dataset` and `run_output` return. This
    asserts what is actually in the columns an auditor would query -- which is
    the only version of the claim that survives a change to how `seed` writes.
    """
    from indic_platform.db.models import AnalysisRun, Call
    from sqlalchemy import select

    dataset = demo_seed.build_dataset(count=8, as_of=AS_OF, key_prefix=FIXTURE)
    async with seeded() as db:
        await demo_seed.seed(db, dataset, lexicon_version="test")
        await db.commit()

    async with seeded() as db:
        call_ids = await _seeded_call_ids(db)
        runs = list(
            (await db.scalars(select(AnalysisRun).where(AnalysisRun.call_id.in_(call_ids)))).all()
        )
        assert len(runs) == len(dataset)
        for run in runs:
            assert run.model == demo_seed.SEED_MODEL
            assert run.prompt_version == demo_seed.SEED_PROMPT_VERSION
            assert run.output["demo"] is True
            assert run.output["source"].startswith("golden:")
            # Truthful, and therefore present: these describe files that really
            # were read and settings `combine` really ran with.
            assert run.policy_version and run.lexicon_version
            assert run.input_sha256

        calls = list((await db.scalars(select(Call).where(Call.id.in_(call_ids)))).all())
        for call in calls:
            assert call.stt_model == demo_seed.SEED_MODEL
            # No transcription happened, so no rupee figure is booked. A
            # fabricated one would flow into the cost panel as if measured.
            assert call.stt_cost_inr == 0


@pytest.mark.integration
async def test_the_queue_shows_demo_rows_and_the_qa_sample_stream_does_not(seeded: Any) -> None:
    """The line this whole exclusion is drawn along.

    The reviewer's queue must show seeded flags -- a demonstration with an empty
    queue demonstrates nothing. The lead's QA-sample stream must not: it is a
    measurement, and it feeds `metrics.false_negative_estimate`, where a seeded
    call settling as "the detector missed nothing" is a fabricated datum in a
    rate. Both come out of `flag_summaries`, so the distinction lives in one
    branch of one function and is easy to lose.
    """
    from comms_surveillance.api import flag_summaries

    dataset = demo_seed.build_dataset(count=48, as_of=AS_OF, key_prefix=FIXTURE)
    async with seeded() as db:
        await demo_seed.seed(db, dataset, lexicon_version="test")
        await db.commit()
        call_ids = await _seeded_call_ids(db)

    async with seeded() as db:
        assert _mine(await flag_summaries(db, limit=5000), call_ids)
        sampled = _mine(await flag_summaries(db, qa_sample=True, limit=5000), call_ids)
    assert sampled == [], "a seeded flag reached the lead's QA-sample measurement stream"


@pytest.mark.integration
async def test_seeded_rows_do_not_move_the_precision_metric(seeded: Any) -> None:
    """The defect this seeder would otherwise introduce, pinned.

    A seeded `confirmed` is a fabricated human verdict. Counted, it would put an
    invented precision on the governance dashboard beside the real measured
    figures -- the passing placeholder CLAUDE.md forbids, shown to exactly the
    audience the demo exists for. `metrics.DEMO_MARKER` excludes them; this
    proves the exclusion by difference, so it cannot pass because the database
    happened to be empty.
    """
    from comms_surveillance import metrics

    dataset = demo_seed.build_dataset(count=24, as_of=AS_OF, key_prefix=FIXTURE)
    assert sum(len(f.dispositions) for c in dataset for f in c.flags) > 0, (
        "the sample seeded no dispositions, so this would prove nothing"
    )

    async with seeded() as db:
        before = [
            (row.category, row.confirmed, row.false_positive, row.decided, row.precision)
            for row in await metrics.precision_by_category(db)
        ]
        before_fn = await metrics.false_negative_estimate(db)

    async with seeded() as db:
        await demo_seed.seed(db, dataset, lexicon_version="test")
        await db.commit()

    async with seeded() as db:
        after = [
            (row.category, row.confirmed, row.false_positive, row.decided, row.precision)
            for row in await metrics.precision_by_category(db)
        ]
        after_fn = await metrics.false_negative_estimate(db)
        excluded = await metrics.demo_row_counts(db)

    assert after == before
    assert after_fn == before_fn
    # Excluded, and said out loud: an operator must be able to tell an empty
    # dashboard from one whose every row was filtered.
    assert excluded["analysis_runs"] >= len(dataset)
    assert excluded["flags"] >= sum(len(call.flags) for call in dataset)
