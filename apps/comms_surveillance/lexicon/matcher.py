"""Stage 0: the deterministic lexicon matcher (PRD E5).

This is the recall floor and, as the PRD puts it, "the piece an auditor can
read". It decides nothing on its own -- it produces spans that Stage 1 and
Stage 2 take as input and that a human ultimately judges.

Three properties the rest of the pipeline depends on:

**Pure.** `Lexicon.scan` performs no I/O: no file reads, no network, no clock,
no randomness. The YAML is read once by `load`, and a loaded `Lexicon` is
immutable. That is what makes Stage 0 reproducible across a re-run months later
and what lets the benchmark mean anything.

**Token-aware.** `guarantee` must not fire inside `guaranteed` when the entry
said `guarantee`, and more importantly must not fire inside an unrelated longer
word. Matching is bounded by word boundaries computed from Unicode word
characters, so it works the same in Devanagari, Telugu, Tamil and Latin.

**Fast.** One alternation per category compiled once, rather than several
hundred separate searches per segment. The acceptance bar is 50 ms per
transcript; a compiled alternation over ~1000 terms does the whole golden set
in a fraction of that.

Scanning runs over the native-script text *and* its Roman rendering, because
the same phrase arrives as Devanagari from STT and as Roman from a chat export.
Each hit records which field it came from: evidence quoted to a reviewer must
come from the native `text`, and a hit found only in the transliteration points
at its segment rather than pretending to be a verbatim quote.
"""

import hashlib
import re
import subprocess
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

LEXICON_DIR = Path(__file__).parent
CATEGORIES = (
    "guaranteed_returns",
    "mnpi_insider",
    "personal_trading",
    "off_channel_comms",
    "conduct",
    "confidential_data",
)

# Word characters for boundary purposes. `\w` covers Devanagari, Telugu and
# Tamil *letters* but NOT their combining marks (Unicode category M*): the
# matra in `अप्रकाशितों` is not `\w`, so a plain `\w` boundary treats
# `अप्रकाशित` as a complete token there and the term fires on a fragment. Any
# mark therefore counts as a word character too.
_WORD_RE = re.compile(r"\w", re.UNICODE)


# Devanagari through Sinhala: every script in SCRIPTS, plus their marks.
_INDIC = range(0x0900, 0x0E00)

# A token: word characters plus Indic letters and their combining marks, which
# `\w` alone leaves out.
TOKEN = re.compile(r"[\w\u0900-\u0DFF]+", re.UNICODE)


def _is_word_char(char: str) -> bool:
    return bool(_WORD_RE.match(char)) or unicodedata.category(char).startswith("M")


def _is_indic(char: str) -> bool:
    return ord(char) in _INDIC


# Words that turn a promise into a disclaimer. Anchored to word boundaries so
# that "maano" does not count as "no" and "Techno" does not count as "no".
NEGATORS = re.compile(
    r"\b(?:not|no|never|cannot|can't|cant|don't|dont|doesn't|doesnt|won't|wont|nothing|neither|nor)\b",
    re.IGNORECASE,
)
# How far back to look. Long enough for "past performance is not a guarantee of
# future returns", short enough not to reach the previous clause.
NEGATION_WINDOW = 40
# A negation does not carry across a sentence or clause boundary. The comma is
# included deliberately: in "there is no way you lose, guaranteed returns" the
# negation belongs to the previous clause and the promise is real. The cost is
# that "we do not, under any circumstances, guarantee returns" will fire -- an
# error in the direction of flagging rather than missing, which is the right
# way round for surveillance.
CLAUSE_END = re.compile(r"[.!?;,\n।]")


@dataclass(frozen=True)
class Entry:
    """One Compliance-owned lexicon entry."""

    id: str
    category: str
    severity: str
    weight: float
    note: str
    terms: tuple[str, ...]
    regex: str | None
    # When true, a hit is suppressed if the words just before it negate it.
    # Set on entries whose phrasing is also the standard risk disclaimer:
    # "guarantee of returns" is a promise, "not a guarantee of returns" is the
    # sentence every compliant manager is required to say. Declared per entry
    # in the YAML so Compliance can see and change which terms behave this way.
    negatable: bool = False


@dataclass(frozen=True)
class Hit:
    """One match, with everything a reviewer or a later stage needs to place it."""

    category: str
    entry_id: str
    severity: str
    weight: float
    # Which field it was found in: "text" (native, quotable as evidence) or
    # "text_roman" (a search aid -- never quoted verbatim to a reviewer).
    field: str
    start: int
    end: int
    matched: str
    segment_index: int
    speaker: str
    # "term" for a literal, "regex" for a pattern. Auditors ask.
    kind: str
    # The lexicon that produced this hit (PRD E7). Carried on the hit rather
    # than looked up later, so a persisted flag is self-describing: whoever
    # reads it a year on can resolve exactly which YAML raised it.
    lexicon_version: str


@dataclass(frozen=True)
class Segment:
    """The matcher's view of a transcript turn: no database, no adapter types."""

    text: str
    text_roman: str = ""
    speaker: str = ""


def negated(haystack: str, start: int) -> bool:
    """Is the hit at `start` preceded by a negation in the same clause?

    Applied to the text rather than baked into each pattern, because a
    fixed-width lookbehind can only encode one surface form: it misses
    "not  a guarantee" (two spaces), "cannot guarantee", "we do not
    guarantee", and it never protects the literal term list at all.
    """
    window = haystack[max(0, start - NEGATION_WINDOW) : start]
    clause = CLAUSE_END.split(window)[-1]
    return bool(NEGATORS.search(clause))


def _boundary_ok(haystack: str, start: int, end: int) -> bool:
    """Is this span a whole token rather than a fragment of a longer word?

    Checked around the span rather than baked into the pattern so that a term
    which itself starts or ends with punctuation still behaves.
    """
    before = haystack[start - 1] if start > 0 else ""
    after = haystack[end] if end < len(haystack) else ""
    first, last = haystack[start], haystack[end - 1]

    # The start is always strict: a term must begin where a token begins, or it
    # is matching the middle of an unrelated word.
    if _is_word_char(first) and before and _is_word_char(before):
        return False

    # The end is strict for Latin but not for Indic scripts, and the difference
    # is linguistic rather than a convenience. Tamil and Telugu are
    # agglutinative: case endings attach directly to the stem, so "டீமேட்"
    # (demat) legitimately appears as "டீமேட்டில்" (in the demat) and
    # "अप्रकाशित" as "अप्रकाशितों". A word list holds stems; requiring a whole-
    # token match there would silently drop every inflected form -- which cost
    # a real true positive when this was first written strictly. English has no
    # such suffixation, so "no loss" must still not fire inside "no lossless".
    if _is_indic(last):
        return True
    return not (_is_word_char(last) and after and _is_word_char(after))


@dataclass(frozen=True)
class Lexicon:
    """A loaded, immutable lexicon. Construct with `load`; call `scan`."""

    entries: tuple[Entry, ...]
    version: str
    _by_term: dict[str, Entry] = field(default_factory=dict, repr=False)
    # First token of a term -> its candidate terms, longest first. Replaces a
    # single 972-alternative regex, which the engine walks alternative by
    # alternative at every position: that cost 55 ms on a 15k-character call
    # where this costs about one. The lexicon only grows from here, and an
    # alternation gets linearly slower with every term Compliance adds while an
    # index does not.
    _index: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    _patterns: tuple[tuple[Entry, re.Pattern[str]], ...] = field(default=(), repr=False)

    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({e.category for e in self.entries}))

    def scan_text(self, haystack: str, *, field_name: str, index: int, speaker: str) -> list[Hit]:
        """Every hit in one string. Pure: no I/O, no mutation of self."""
        if not haystack:
            return []
        hits: list[Hit] = []

        # Every offset is computed on the ORIGINAL string. Folding is not
        # length-preserving -- "Straße".casefold() is "strasse", one character
        # longer -- so a span computed on a folded copy and sliced from the
        # original comes back shifted, and a truncated evidence span still
        # passes the verifier.
        for token in TOKEN.finditer(haystack):
            start = token.start()
            for term in self._index.get(token.group(0).casefold(), ()):
                end = start + len(term)
                candidate = haystack[start:end]
                # Equal-length casefold comparison: if folding changes the
                # length the comparison simply fails, which loses an exotic
                # match but can never produce a wrong span.
                if candidate.casefold() != term:
                    continue
                if not _boundary_ok(haystack, start, end):
                    continue
                entry = self._by_term.get(term)
                if entry is None:
                    continue
                if entry.negatable and negated(haystack, start):
                    continue
                hits.append(
                    Hit(
                        category=entry.category,
                        entry_id=entry.id,
                        severity=entry.severity,
                        weight=entry.weight,
                        field=field_name,
                        start=start,
                        end=end,
                        matched=candidate,
                        segment_index=index,
                        speaker=speaker,
                        kind="term",
                        lexicon_version=self.version,
                    )
                )
                # Longest term at this position wins, as the alternation did.
                break

        for entry, pattern in self._patterns:
            for match in pattern.finditer(haystack):
                start, end = match.span()
                if entry.negatable and negated(haystack, start):
                    continue
                hits.append(
                    Hit(
                        category=entry.category,
                        entry_id=entry.id,
                        severity=entry.severity,
                        weight=entry.weight,
                        field=field_name,
                        start=start,
                        end=end,
                        matched=haystack[start:end],
                        segment_index=index,
                        speaker=speaker,
                        kind="regex",
                        lexicon_version=self.version,
                    )
                )
        return hits

    def scan(self, segments: Sequence[Segment]) -> list[Hit]:
        """Every hit across a transcript, native text and Roman rendering both.

        The turns are joined into one buffer per field and scanned once, rather
        than scanned per turn. The regex work is the same either way, but a
        30-minute call is ~450 turns and six categories, so per-turn scanning
        means 2700 `finditer` calls where this needs twelve. Measured on a
        450-turn call that is the difference between 121 ms and comfortably
        inside the 50 ms budget.

        Turns are joined with a newline, which `CLAUSE_END` already treats as a
        boundary, so a negation in one turn cannot reach into the next.

        A segment whose Roman rendering is identical to its native text -- what
        `verbatim` produces for English and Roman-script Hinglish -- is scanned
        once, so those transcripts do not score double.
        """
        hits: list[Hit] = []
        for field_name in ("text", "text_roman"):
            buffer, spans = _join(segments, field_name)
            if not buffer:
                continue
            for hit in self.scan_text(buffer, field_name=field_name, index=-1, speaker=""):
                index, offset = _locate(spans, hit.start)
                if index < 0:
                    continue
                hits.append(
                    replace(
                        hit,
                        start=hit.start - offset,
                        end=hit.end - offset,
                        segment_index=index,
                        speaker=segments[index].speaker,
                    )
                )
        return sorted(hits, key=lambda h: (h.segment_index, h.start, h.category, h.entry_id))


def _join(segments: Sequence[Segment], field_name: str) -> tuple[str, list[tuple[int, int, int]]]:
    """One buffer plus (start, end, segment_index) for mapping offsets back.

    A segment contributes to the `text_roman` buffer only when its rendering
    differs from its native text; otherwise the same words would be scanned and
    counted twice.
    """
    parts: list[str] = []
    spans: list[tuple[int, int, int]] = []
    cursor = 0
    for index, segment in enumerate(segments):
        if field_name == "text":
            value = segment.text
        else:
            value = segment.text_roman if segment.text_roman != segment.text else ""
        if not value:
            continue
        parts.append(value)
        spans.append((cursor, cursor + len(value), index))
        cursor += len(value) + 1  # the joining newline
    return "\n".join(parts), spans


def _locate(spans: Sequence[tuple[int, int, int]], position: int) -> tuple[int, int]:
    """(segment index, buffer offset of that segment) for a buffer position."""
    lo, hi = 0, len(spans) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        start, end, index = spans[mid]
        if position < start:
            hi = mid - 1
        elif position >= end:
            lo = mid + 1
        else:
            return index, start
    return -1, 0


def categories_hit(hits: Iterable[Hit]) -> set[str]:
    return {h.category for h in hits}


def score(hits: Iterable[Hit]) -> float:
    """A single 0-1 number for the triage combiner in E5.

    The maximum weight rather than a sum: ten weak hits are not stronger
    evidence than one strong one, and summing would let a chatty transcript
    outrank a damning sentence.
    """
    weights = [h.weight for h in hits]
    return max(weights) if weights else 0.0


def highest_severity(hits: Iterable[Hit]) -> str | None:
    order = {"low": 0, "medium": 1, "high": 2}
    ranked = sorted(hits, key=lambda h: order.get(h.severity, 0), reverse=True)
    return ranked[0].severity if ranked else None


# --- loading (the only I/O in this module) ------------------------------------


def lexicon_version(directory: Path = LEXICON_DIR) -> str:
    """A stable identifier for exactly this lexicon content.

    PRD E7 requires every flag to record `lexicon_version`. The git tree SHA is
    the right answer in a checkout -- it changes when and only when the YAML
    changes, and an auditor can resolve it back to the files. Outside a
    checkout (a container that shipped the files without .git) there is no tree
    object, so fall back to a hash of the contents, prefixed so nobody mistakes
    one for the other.
    """
    try:
        # The repo root is asked of git for *this directory*, not derived from
        # where this module happens to live. Deriving it from the module means
        # `lexicon_version(some_other_dir)` silently falls back to the content
        # hash, so the git branch is unreachable for any directory but the
        # built-in one -- and therefore untestable.
        top = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if top.returncode != 0 or not top.stdout.strip():
            raise ValueError("not a git checkout")
        root = Path(top.stdout.strip())
        relative = directory.resolve().relative_to(root.resolve())
        # A tree SHA describes HEAD, not the working copy. If the YAML has been
        # edited and not committed, HEAD's SHA would label a flag with a
        # lexicon that did not produce it -- precisely the audit failure E7
        # exists to prevent -- so a dirty directory falls through to the
        # content hash instead.
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", str(relative)],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=root,
            check=False,
        )
        if dirty.returncode == 0 and not dirty.stdout.strip():
            out = subprocess.run(
                ["git", "rev-parse", f"HEAD:{relative}"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=root,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()[:12]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.yaml")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()[:12]}"


def _entries(payload: dict[str, Any], category: str) -> list[Entry]:
    out = []
    for raw in payload.get("entries") or []:
        terms = tuple(str(t) for t in (raw.get("terms") or []) if str(t).strip())
        pattern = raw.get("regex")
        if not terms and not pattern:
            raise ValueError(f"{category}:{raw.get('id')} has neither terms nor regex")
        out.append(
            Entry(
                id=str(raw["id"]),
                category=category,
                severity=str(raw.get("severity", "medium")),
                weight=float(raw.get("weight", 0.5)),
                note=str(raw.get("note", "")),
                terms=terms,
                regex=str(pattern) if pattern else None,
                negatable=bool(raw.get("negatable", False)),
            )
        )
    return out


def load(directory: Path = LEXICON_DIR, *, version: str | None = None) -> Lexicon:
    """Read the YAML once and compile. Everything after this is pure.

    Terms are compiled into one alternation per category, longest first so that
    `guaranteed return` wins over `return` at the same position.
    """
    entries: list[Entry] = []
    for category in CATEGORIES:
        path = directory / f"{category}.yaml"
        payload = yaml.safe_load(path.read_text())
        if payload.get("category") != category:
            raise ValueError(f"{path.name} declares category {payload.get('category')!r}")
        entries.extend(_entries(payload, category))

    by_term: dict[str, Entry] = {}
    duplicates: list[str] = []
    for entry in entries:
        for term in entry.terms:
            folded = term.casefold()
            if folded in by_term:
                duplicates.append(f"{term!r} in {by_term[folded].id} and {entry.id}")
                continue
            by_term[folded] = entry
    if duplicates:
        # A term defined twice silently binds to whichever entry loaded first,
        # so an auditor tracing a hit lands on the wrong note. Compliance
        # should see this rather than have it resolved quietly.
        raise ValueError("duplicate lexicon terms: " + "; ".join(sorted(duplicates)))

    index: dict[str, list[str]] = {}
    for folded in by_term:
        head = TOKEN.search(folded)
        if head is None:
            continue
        index.setdefault(head.group(0), []).append(folded)
    # Longest first, so "guaranteed return" wins over "return" at a position.
    compiled_index = {
        head: tuple(sorted(terms, key=len, reverse=True)) for head, terms in index.items()
    }
    patterns = tuple(
        (entry, re.compile(entry.regex, re.IGNORECASE | re.UNICODE))
        for entry in entries
        if entry.regex
    )
    return Lexicon(
        entries=tuple(entries),
        version=version or lexicon_version(directory),
        _by_term=by_term,
        _index=compiled_index,
        _patterns=patterns,
    )
