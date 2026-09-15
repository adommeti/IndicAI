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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
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

# Word characters for boundary purposes, Unicode-aware: Python's `\w` already
# covers Devanagari, Telugu and Tamil letters, so one definition serves every
# script in the corpus.
_WORD = re.compile(r"\w", re.UNICODE)


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


def _boundary_ok(haystack: str, start: int, end: int) -> bool:
    """Is this span a whole token rather than a fragment of a longer word?

    Checked around the span rather than baked into the pattern so that a term
    which itself starts or ends with punctuation still behaves.
    """
    before = haystack[start - 1] if start > 0 else ""
    after = haystack[end] if end < len(haystack) else ""
    first, last = haystack[start], haystack[end - 1]
    if _WORD.match(first) and before and _WORD.match(before):
        return False
    return not (_WORD.match(last) and after and _WORD.match(after))


@dataclass(frozen=True)
class Lexicon:
    """A loaded, immutable lexicon. Construct with `load`; call `scan`."""

    entries: tuple[Entry, ...]
    version: str
    _by_term: dict[str, Entry] = field(default_factory=dict, repr=False)
    _literal: dict[str, re.Pattern[str]] = field(default_factory=dict, repr=False)
    _patterns: tuple[tuple[Entry, re.Pattern[str]], ...] = field(default=(), repr=False)

    def categories(self) -> tuple[str, ...]:
        return tuple(sorted({e.category for e in self.entries}))

    def scan_text(self, haystack: str, *, field_name: str, index: int, speaker: str) -> list[Hit]:
        """Every hit in one string. Pure: no I/O, no mutation of self."""
        if not haystack:
            return []
        folded = haystack.casefold()
        hits: list[Hit] = []

        for category, pattern in self._literal.items():
            for match in pattern.finditer(folded):
                start, end = match.span()
                if not _boundary_ok(folded, start, end):
                    continue
                entry = self._by_term.get(match.group(0))
                if entry is None or entry.category != category:
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
                        # Sliced from the original, so the reviewer sees the
                        # text as spoken rather than casefolded.
                        matched=haystack[start:end],
                        segment_index=index,
                        speaker=speaker,
                        kind="term",
                        lexicon_version=self.version,
                    )
                )

        for entry, pattern in self._patterns:
            for match in pattern.finditer(haystack):
                start, end = match.span()
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

        A segment whose Roman rendering is identical to its native text -- which
        is what `verbatim` produces for English and Roman-script Hinglish -- is
        scanned once, so those transcripts do not score double.
        """
        hits: list[Hit] = []
        for index, segment in enumerate(segments):
            hits.extend(
                self.scan_text(
                    segment.text, field_name="text", index=index, speaker=segment.speaker
                )
            )
            if segment.text_roman and segment.text_roman != segment.text:
                hits.extend(
                    self.scan_text(
                        segment.text_roman,
                        field_name="text_roman",
                        index=index,
                        speaker=segment.speaker,
                    )
                )
        return sorted(hits, key=lambda h: (h.segment_index, h.start, h.category, h.entry_id))


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
        root = _repo_root()
        relative = directory.relative_to(root)
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


def _repo_root() -> Path:
    return LEXICON_DIR.parents[2]


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
    per_category: dict[str, list[str]] = {}
    for entry in entries:
        for term in entry.terms:
            folded = term.casefold()
            # First definition wins, and a duplicate across categories is a
            # content bug Compliance should see rather than a silent override.
            by_term.setdefault(folded, entry)
            per_category.setdefault(entry.category, []).append(folded)

    literal = {
        category: re.compile(
            "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
        )
        for category, terms in per_category.items()
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
        _literal=literal,
        _patterns=patterns,
    )
