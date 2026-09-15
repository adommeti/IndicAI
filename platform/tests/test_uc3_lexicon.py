"""Stage 0: the lexicon matcher.

The acceptance criteria this file carries: script variants match, regex entries
match, the matcher is pure (no I/O), and it runs a transcript in under 50 ms.
"""

import time
from pathlib import Path

import pytest
import yaml
from comms_surveillance.lexicon import matcher
from comms_surveillance.lexicon.matcher import Segment

LEXICON = Path("apps/comms_surveillance/lexicon")


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


def test_every_declared_regex_compiles(lexicon: matcher.Lexicon) -> None:
    import re

    for entry in lexicon.entries:
        if entry.regex:
            re.compile(entry.regex)


# --- purity ------------------------------------------------------------------------


def test_scanning_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """`scan` must not touch the filesystem, the network, or a subprocess.

    Stage 0 has to be reproducible from the lexicon version alone. Anything
    read at scan time is something that can differ between the run that raised
    a flag and the run an auditor does a year later.
    """
    lexicon = matcher.load()  # the load is allowed to do I/O; nothing after it is
    import builtins
    import socket
    import subprocess

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("scan performed I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    hits = lexicon.scan(
        [
            Segment(text="Tell the client the return is guaranteed", speaker="SPEAKER_00"),
            Segment(text="मैंने अप्रकाशित नतीजे देखे हैं", speaker="SPEAKER_01"),
        ]
    )
    assert hits


def test_scanning_does_not_mutate_the_lexicon_or_its_input() -> None:
    lexicon = matcher.load()
    segments = [Segment(text="send it to my gmail", text_roman="send it to my gmail")]
    before = (len(lexicon.entries), lexicon.version, list(segments))
    lexicon.scan(segments)
    lexicon.scan(segments)
    assert (len(lexicon.entries), lexicon.version, list(segments)) == before


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


def test_a_loaded_lexicon_carries_its_version() -> None:
    assert matcher.load().version


# --- speed --------------------------------------------------------------------------


def test_a_transcript_scans_in_under_fifty_milliseconds() -> None:
    """The acceptance bar. Measured on the golden set, worst transcript."""
    import json

    lexicon = matcher.load()
    records = [
        json.loads(line)
        for line in Path("platform/eval/golden/uc3_surveillance/transcripts.jsonl")
        .read_text()
        .splitlines()
        if line
    ]
    transcripts = [
        [Segment(text=s["text"], speaker=s["speaker"]) for s in r["segments"]] for r in records
    ]
    lexicon.scan(transcripts[0])  # warm the regex cache

    worst = 0.0
    start_all = time.perf_counter()
    for segments in transcripts:
        start = time.perf_counter()
        lexicon.scan(segments)
        worst = max(worst, (time.perf_counter() - start) * 1000)
    mean = (time.perf_counter() - start_all) * 1000 / len(transcripts)

    assert worst < 50.0, f"worst transcript took {worst:.1f}ms"
    print(f"\nlexicon scan: worst {worst:.2f}ms, mean {mean:.2f}ms over {len(transcripts)}")
