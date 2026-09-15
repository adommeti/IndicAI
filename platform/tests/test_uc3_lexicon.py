"""Stage 0: the lexicon matcher.

The acceptance criteria this file carries: script variants match, regex entries
match, the matcher is pure (no I/O), and it runs a transcript in under 50 ms.
"""

import re
import time
from pathlib import Path

import pytest
import yaml
from comms_surveillance.lexicon import matcher
from comms_surveillance.lexicon.matcher import Segment
from comms_surveillance.transliterate import offline

LEXICON = Path(matcher.__file__).parent
GOLDEN = Path(matcher.__file__).parents[3] / "platform/eval/golden/uc3_surveillance"


@pytest.fixture(scope="module")
def lexicon() -> matcher.Lexicon:
    return matcher.load()


# --- the lexicon files themselves ----------------------------------------------


def test_every_category_has_a_draft_file_with_enough_entries(lexicon: matcher.Lexicon) -> None:
    """The prompt's floor is 25 entries per category, and DRAFT is not optional.

    Compliance owns these files (PRD E7); a file that does not say so invites
    someone to treat engineering's guesses as policy.
    """
    assert set(lexicon.categories()) == set(matcher.CATEGORIES)
    for category in matcher.CATEGORIES:
        payload = yaml.safe_load((LEXICON / f"{category}.yaml").read_text())
        assert payload["status"] == "draft"
        assert payload["owner"] == "compliance"
        assert len(payload["entries"]) >= 25, category


def test_every_entry_is_usable(lexicon: matcher.Lexicon) -> None:
    for entry in lexicon.entries:
        assert entry.terms or entry.regex, entry.id
        assert entry.severity in {"low", "medium", "high"}, entry.id
        assert 0.0 < entry.weight <= 1.0, entry.id
        assert entry.note, entry.id


def test_entry_ids_are_unique(lexicon: matcher.Lexicon) -> None:
    ids = [e.id for e in lexicon.entries]
    assert len(ids) == len(set(ids))


def test_all_three_native_scripts_are_represented(lexicon: matcher.Lexicon) -> None:
    """A lexicon with only English terms would be the v1 blind spot again."""
    ranges = {
        "devanagari": range(0x0900, 0x0980),
        "telugu": range(0x0C00, 0x0C80),
        "tamil": range(0x0B80, 0x0C00),
    }
    for category in matcher.CATEGORIES:
        terms = [t for e in lexicon.entries if e.category == category for t in e.terms]
        for name, block in ranges.items():
            assert any(any(ord(c) in block for c in term) for term in terms), (
                f"{category} has no {name} terms"
            )


# --- script variants ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "language", "category"),
    [
        ("Tell the client the return is guaranteed", "en-IN", "guaranteed_returns"),
        ("क्लाइंट से कह दो रिटर्न की गारंटी है", "hi-IN", "guaranteed_returns"),
        ("క్లయింట్‌కి చెప్పండి రాబడి గ్యారంటీ", "te-IN", "guaranteed_returns"),
        ("வாடிக்கையாளரிடம் வருமானம் உறுதி என்று சொல்லுங்கள்", "ta-IN", "guaranteed_returns"),
        ("Client ko bolo return guaranteed hai", "hi-Latn", "guaranteed_returns"),
        ("मैंने अप्रकाशित नतीजे देखे हैं", "hi-IN", "mnpi_insider"),
        ("ప్రచురించని ఫలితాలు చూశాను", "te-IN", "mnpi_insider"),
        ("வெளியிடப்படாத முடிவுகள்", "ta-IN", "mnpi_insider"),
        ("मेरी पत्नी के डीमैट में डालो", "hi-IN", "personal_trading"),
        ("వాట్సాప్‌లో పంపండి", "te-IN", "off_channel_comms"),
        ("मीतı", "hi-IN", None),
    ],
)
def test_the_same_meaning_hits_in_every_script(
    lexicon: matcher.Lexicon, text: str, language: str, category: str | None
) -> None:
    hits = lexicon.scan([Segment(text=text)])
    found = matcher.categories_hit(hits)
    if category is None:
        assert not found
    else:
        assert category in found, f"{text!r} -> {found}"


def test_a_roman_rendering_catches_what_the_native_text_does_not() -> None:
    """The reason every segment is stored twice (uc3/P2).

    A Devanagari transcript whose Roman rendering contains a term the
    native-script list happens to miss must still hit, and the hit must say it
    came from the transliteration so nobody quotes it as verbatim evidence.
    """
    lexicon = matcher.load()
    native = "यह गोपनीय है"  # not in any list
    hits = lexicon.scan([Segment(text=native, text_roman="send it to my gmail")])
    assert hits, "the Roman rendering should have matched"
    assert all(h.field == "text_roman" for h in hits)
    assert "confidential_data" in matcher.categories_hit(hits)


def test_identical_native_and_roman_text_is_not_counted_twice() -> None:
    """English and Roman Hinglish store `text_roman == text` (`verbatim`)."""
    lexicon = matcher.load()
    text = "Do not put this on email, message me on WhatsApp instead"
    once = lexicon.scan([Segment(text=text, text_roman=text)])
    bare = lexicon.scan([Segment(text=text)])
    assert len(once) == len(bare)
    assert all(h.field == "text" for h in once)


# --- tokenization ---------------------------------------------------------------


def test_a_term_does_not_fire_inside_a_longer_word() -> None:
    lexicon = matcher.load()
    # "no loss" is an entry; "no lossless" must not match it.
    assert not [
        h
        for h in lexicon.scan([Segment(text="the codec is no lossless format")])
        if h.kind == "term"
    ]
    assert [h for h in lexicon.scan([Segment(text="there is no loss at all")]) if h.kind == "term"]


def test_matching_is_case_insensitive_but_evidence_keeps_the_original_case() -> None:
    lexicon = matcher.load()
    hits = lexicon.scan([Segment(text="Send It To My GMAIL right now")])
    assert hits
    assert any(h.matched == "my GMAIL" or "GMAIL" in h.matched for h in hits)


def test_the_longest_term_wins_at_a_position() -> None:
    lexicon = matcher.load()
    hits = [
        h
        for h in lexicon.scan([Segment(text="message me on WhatsApp instead")])
        if h.kind == "term"
    ]
    assert hits
    # oc-002 ("message me on whatsapp instead") rather than oc-001 ("whatsapp").
    assert max(len(h.matched) for h in hits) > len("whatsapp")


# --- regex entries ---------------------------------------------------------------


def test_a_regex_entry_catches_a_shape_no_word_list_could() -> None:
    """gr-001's pattern spans words the literal list does not enumerate."""
    lexicon = matcher.load()
    hits = [
        h
        for h in lexicon.scan([Segment(text="the fund is guaranteed to return twelve percent")])
        if h.kind == "regex"
    ]
    assert hits
    assert any(h.category == "guaranteed_returns" for h in hits)


def test_the_proximity_regexes_need_both_halves() -> None:
    """mn-024 fires on a non-public marker near a trade verb, not on either alone."""
    lexicon = matcher.load()

    def mn_regex(text: str) -> list[matcher.Hit]:
        return [
            h
            for h in lexicon.scan([Segment(text=text)])
            if h.kind == "regex" and h.entry_id in {"mn-024", "mn-025"}
        ]

    assert mn_regex("it is not public yet so sell before then")
    assert not mn_regex("the quarterly report is not public yet")
    assert not mn_regex("sell fifty thousand shares at market")


def test_a_broken_regex_is_rejected_at_load(tmp_path: Path) -> None:
    """`load` compiles every pattern, so a bad one must fail loudly there.

    Asserting that the shipped regexes compile is not a test -- `load()` in the
    fixture would already have raised. What is worth pinning is that a broken
    entry cannot be loaded and silently ignored.
    """
    for category in matcher.CATEGORIES:
        (tmp_path / f"{category}.yaml").write_text((LEXICON / f"{category}.yaml").read_text())
    path = tmp_path / "conduct.yaml"
    path.write_text(
        path.read_text() + "\n  - id: cd-999\n    note: broken\n    regex: '([unclosed'\n"
    )
    with pytest.raises(re.error):
        matcher.load(tmp_path, version="test")


def test_a_term_defined_twice_is_rejected_at_load(tmp_path: Path) -> None:
    """A duplicate binds to whichever entry loaded first, so an auditor tracing
    a hit lands on the wrong note. It has to be a load error, not a preference."""
    for category in matcher.CATEGORIES:
        (tmp_path / f"{category}.yaml").write_text((LEXICON / f"{category}.yaml").read_text())
    path = tmp_path / "conduct.yaml"
    path.write_text(
        path.read_text()
        + "\n  - id: cd-998\n    note: duplicate\n    terms:\n      - do not go to hr\n"
    )
    with pytest.raises(ValueError, match="duplicate lexicon terms"):
        matcher.load(tmp_path, version="test")


# --- purity ------------------------------------------------------------------------


def test_scanning_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """`scan` must not touch the filesystem, the network, or a subprocess.

    Stage 0 has to be reproducible from the lexicon version alone. Anything
    read at scan time is something that can differ between the run that raised
    a flag and the run an auditor does a year later.
    """
    lexicon = matcher.load()  # the load is allowed to do I/O; nothing after it is
    import builtins
    import io
    import os
    import random
    import socket
    import subprocess
    import time as time_module

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("scan reached outside itself")

    # `builtins.open` alone is not enough: `io.open` and `os.open` are separate
    # objects. A lazy import would go through none of them either -- that hole
    # is closed by `test_the_matcher_has_no_lazy_imports` below, because
    # patching `__import__` breaks the interpreter for everyone including
    # pytest's own teardown.
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(io, "open", forbidden)
    monkeypatch.setattr(os, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    # The docstring claims no clock and no randomness, so assert those too.
    monkeypatch.setattr(time_module, "time", forbidden)
    monkeypatch.setattr(random, "random", forbidden)

    hits = lexicon.scan(
        [
            Segment(text="Tell the client the return is guaranteed", speaker="SPEAKER_00"),
            Segment(text="मैंने अप्रकाशित नतीजे देखे हैं", speaker="SPEAKER_01"),
        ]
    )
    assert hits


def test_repeated_scans_of_the_same_lexicon_agree() -> None:
    """Catches state accumulating between scans -- a cache keyed wrongly, a
    list appended to on the Lexicon. Frozen dataclasses do not prevent a
    mutable field from growing."""
    lexicon = matcher.load()
    a = [Segment(text="send it to my gmail")]
    b = [Segment(text="मैंने अप्रकाशित नतीजे देखे हैं")]
    first_a, first_b = lexicon.scan(a), lexicon.scan(b)
    for _ in range(5):
        assert lexicon.scan(a) == first_a
        assert lexicon.scan(b) == first_b
    indexed = sum(len(v) for v in lexicon._index.values())
    assert indexed == len({term.casefold() for e in lexicon.entries for term in e.terms})


def test_the_same_input_gives_the_same_output() -> None:
    lexicon = matcher.load()
    segments = [
        Segment(text="Do not put this on email, nothing gets recorded there"),
        Segment(text="मेरी पत्नी के डीमैट में डालो"),
    ]
    assert lexicon.scan(segments) == lexicon.scan(segments)


# --- version -----------------------------------------------------------------------


def test_the_version_is_stable_and_content_derived(tmp_path: Path) -> None:
    """PRD E7: every flag records `lexicon_version`.

    Outside a clean checkout it is a content hash, so editing a file changes it
    -- which is the property that makes the recorded version meaningful.
    """
    for category in matcher.CATEGORIES:
        (tmp_path / f"{category}.yaml").write_text((LEXICON / f"{category}.yaml").read_text())
    first = matcher.lexicon_version(tmp_path)
    assert first == matcher.lexicon_version(tmp_path), "must be stable for identical content"
    assert first.startswith("sha256:")

    path = tmp_path / "conduct.yaml"
    path.write_text(path.read_text() + "\n# an edit by Compliance\n")
    assert matcher.lexicon_version(tmp_path) != first


def test_the_git_branch_of_lexicon_version_is_a_tree_sha(tmp_path: Path) -> None:
    """The prompt specifies a git SHA; the content hash is only the fallback.

    Builds a throwaway repo so the git path is actually executed, and checks the
    dirty-directory fallthrough that stops a stale SHA labelling a flag.
    """
    import subprocess

    repo = tmp_path / "repo"
    lex = repo / "apps" / "comms_surveillance" / "lexicon"
    lex.mkdir(parents=True)
    for category in matcher.CATEGORIES:
        (lex / f"{category}.yaml").write_text((LEXICON / f"{category}.yaml").read_text())

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "lexicon")

    committed = matcher.lexicon_version(lex)
    assert not committed.startswith("sha256:"), "a clean checkout must report the tree SHA"
    assert len(committed) == 12
    expected = git("rev-parse", "HEAD:apps/comms_surveillance/lexicon").stdout.strip()
    assert committed == expected[:12]

    # An uncommitted edit must not keep reporting HEAD's SHA.
    (lex / "conduct.yaml").write_text((lex / "conduct.yaml").read_text() + "\n# edit\n")
    dirty = matcher.lexicon_version(lex)
    assert dirty != committed
    assert dirty.startswith("sha256:")


# --- speed --------------------------------------------------------------------------


def _call_transcript(turns: int) -> list[Segment]:
    """A synthetic call of `turns` turns, with `text_roman` populated.

    Built from real golden turns in Devanagari, because that is the shape the
    wired Stage 0 actually scans: `stage0.segments_of` transliterates every
    native-script segment, so the matcher sees roughly twice the characters.
    Benchmarking without `text_roman` measures half the real work.
    """
    import json
    import random

    records = [
        json.loads(line) for line in (GOLDEN / "transcripts.jsonl").read_text().splitlines() if line
    ]
    hindi = [s["text"] for r in records if r["language_mix"] == "hi-IN" for s in r["segments"]]
    random.seed(0)
    picked = [random.choice(hindi) for _ in range(turns)]
    return [
        Segment(text=text, text_roman=offline(text, "hi-IN")[0], speaker="SPEAKER_00")
        for text in picked
    ]


def test_a_call_transcript_scans_in_under_fifty_milliseconds() -> None:
    """The acceptance bar, measured on a call rather than on a golden fragment.

    The 200 golden transcripts average 192 characters over 5 turns -- they are
    excerpts, not calls. A 30-minute call is roughly 450 turns, and with the
    Roman rendering the matcher sees about 32k characters. That is the number
    this asserts.
    """
    lexicon = matcher.load()
    segments = _call_transcript(450)
    scanned = sum(len(s.text) + len(s.text_roman) for s in segments)
    lexicon.scan(segments)  # warm

    best = min(_time_scan(lexicon, segments) for _ in range(3))
    print(f"\n30-minute call: {len(segments)} turns, {scanned} chars scanned -> {best:.1f} ms")
    assert best < 50.0, f"a 30-minute call took {best:.1f}ms"


def _time_scan(lexicon: matcher.Lexicon, segments: list[Segment]) -> float:
    start = time.perf_counter()
    lexicon.scan(segments)
    return (time.perf_counter() - start) * 1000


def test_the_golden_transcripts_also_scan_well_inside_the_bar() -> None:
    """Worst single golden transcript, which is what the eval runner scans."""
    import json

    lexicon = matcher.load()
    records = [
        json.loads(line) for line in (GOLDEN / "transcripts.jsonl").read_text().splitlines() if line
    ]
    transcripts = [
        [Segment(text=s["text"], speaker=s["speaker"]) for s in r["segments"]] for r in records
    ]
    lexicon.scan(transcripts[0])
    worst = max(_time_scan(lexicon, segments) for segments in transcripts)
    print(f"worst golden transcript: {worst:.2f} ms")
    assert worst < 50.0


def test_the_matcher_stays_under_the_bar_for_an_hour_long_call() -> None:
    """Where the budget actually runs out, stated rather than left to be found.

    An hour of speech (~900 turns) still fits. Somewhere around 1050 turns --
    a call over about seventy minutes -- it does not, and the report says so.
    This test pins the claim that an hour-long call is fine; it is not a claim
    that any length is.
    """
    lexicon = matcher.load()
    segments = _call_transcript(900)
    lexicon.scan(segments)
    best = min(_time_scan(lexicon, segments) for _ in range(3))
    print(f"60-minute call: {len(segments)} turns -> {best:.1f} ms")
    assert best < 50.0, f"an hour-long call took {best:.1f}ms"


def test_the_standard_risk_disclaimer_does_not_fire() -> None:
    """The canonical false positive for this category, and why entries carry
    `negatable`.

    "past performance is not a guarantee of future returns" is the sentence
    every compliant fund manager is required to say. A lexicon that flags it
    buries the reviewer in the one phrase they will see most often. The promise
    it is meant to catch must still fire, so both directions are asserted --
    and against every hit rather than one entry id, so a new entry
    re-introducing the false positive fails this too.
    """
    lexicon = matcher.load()

    quiet = [
        "Historically the fund returned about twelve percent, but past performance "
        "is not a guarantee of future returns.",
        # A fixed-width lookbehind missed all of these.
        "past performance is not  a guarantee of future returns",
        "There is no guarantee of returns in this product.",
        "we cannot guarantee returns",
        "we do not guarantee returns",
        "we don't guarantee returns",
        "there is never any guarantee of returns",
        # The literal term list needs the same protection as the regexes.
        "past performance is not a guaranteed return",
    ]
    for text in quiet:
        assert lexicon.scan([Segment(text=text)]) == [], text

    fires = [
        "I can guarantee returns of twelve percent.",
        "the fund is guaranteed to return twelve percent",
        # A word merely ending in "no" must not suppress a real promise.
        "aap maano guarantee return milega",
        "Techno Capital guarantees returns of twelve percent",
        # The negation belongs to the previous clause.
        "there is no way you lose, guaranteed returns",
    ]
    for text in fires:
        assert lexicon.scan([Segment(text=text)]), text


def test_every_hit_carries_the_lexicon_version() -> None:
    """PRD E7: a flag has to say which lexicon raised it.

    On the hit rather than fetched later, so a row persisted by P5 is
    self-describing when an auditor reads it a year on.
    """
    lexicon = matcher.load()
    hits = lexicon.scan(
        [Segment(text="send it to my gmail"), Segment(text="मैंने अप्रकाशित नतीजे देखे हैं")]
    )
    assert hits
    assert all(h.lexicon_version == lexicon.version for h in hits)


def test_score_and_severity_summarise_a_hit_list() -> None:
    """Both ship public and feed the E5 triage combiner in P4."""
    lexicon = matcher.load()
    hits = lexicon.scan(
        [
            Segment(text="broker commission rates"),  # low weight
            Segment(text="I can guarantee returns of twelve percent"),  # high
        ]
    )
    assert hits
    # The maximum, not the sum: many weak hits must not outrank one strong one.
    assert matcher.score(hits) == max(h.weight for h in hits)
    assert matcher.score([]) == 0.0
    assert matcher.highest_severity(hits) == "high"
    assert matcher.highest_severity([]) is None


def test_the_matcher_has_no_lazy_imports() -> None:
    """The hole `test_scanning_performs_no_io` cannot close by monkeypatching.

    An import inside a function body reads from disk on first call and goes
    through neither `builtins.open` nor `io.open`, so no patch catches it. The
    only reliable check is that the matcher's scanning functions contain no
    import statement at all. `load` and `lexicon_version` are exempt: they are
    the I/O, and everything after them is pure.
    """
    import ast

    source = Path(matcher.__file__).read_text()
    allowed = {"load", "lexicon_version"}
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) or node.name in allowed:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Import | ast.ImportFrom):
                offenders.append(f"{node.name}:{inner.lineno}")
    assert not offenders, f"lazy imports in {offenders}"
