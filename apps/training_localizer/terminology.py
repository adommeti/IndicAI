"""Glossary and LOCKED-statement enforcement for the post-edit stage.

PRD D7 puts the 100% terminology-adherence gate in `post_edit`, a Claude call.
A stochastic model cannot *guarantee* a 100% gate, so this module is the
deterministic half: Claude proposes (it has the judgement to re-word around a
term), and `enforce` disposes. Every deterministic repair is appended to the
same change log the PRD asks for, tagged `enforced: true`, so a reviewer can see
which edits the model made and which the enforcer had to impose.

The split is deliberate and testable: `enforce` alone already satisfies the gate
(`test_enforce_alone_meets_the_gate`), so the pipeline reaches 100% adherence
even when the model is unavailable or wrong.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

TERMS = Path(__file__).parent / "terminology"


@dataclass(frozen=True)
class Change:
    """One post-edit change, in the shape PRD D7's post_edit.md returns."""

    from_text: str
    to_text: str
    reason: str
    enforced: bool = False

    def as_json(self) -> dict[str, Any]:
        return {
            "from": self.from_text,
            "to": self.to_text,
            "reason": self.reason,
            "enforced": self.enforced,
        }


@dataclass(frozen=True)
class Glossary:
    keep_english: tuple[str, ...]
    renderings: tuple[dict[str, Any], ...]
    statements: dict[str, dict[str, Any]]
    version: str
    drafts: int = field(default=0)

    def rendering_for(self, term: str, language: str) -> str | None:
        for entry in self.renderings:
            if entry["term"] == term:
                return str(entry[language])
        return None


def mentions(term: str, text: str) -> bool:
    """Whole-word, case-insensitive presence of an English term.

    Shared with the eval runner's rule on purpose: the pipeline must be scored
    by the same notion of "the term is present" that it enforces.
    """
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None


def load_glossary(directory: Path = TERMS) -> Glossary:
    """Load both terminology files and derive a content version.

    `version` is a hash of the two files, so every localization row records
    exactly which terminology produced it (PRD D6) without a manual bump.
    """
    glossary_text = (directory / "glossary.yaml").read_text()
    renderings_text = (directory / "approved_renderings.yaml").read_text()
    glossary = yaml.safe_load(glossary_text)
    renderings = yaml.safe_load(renderings_text)
    digest = hashlib.sha256((glossary_text + renderings_text).encode()).hexdigest()[:16]
    entries = list(glossary["approved_renderings"])
    statements = {s["id"]: s for s in renderings["statements"]}
    drafts = sum(1 for e in entries if e.get("status") != "approved")
    drafts += sum(1 for s in statements.values() if s.get("status") != "approved")
    return Glossary(
        keep_english=tuple(e["term"] for e in glossary["keep_english"]),
        renderings=tuple(entries),
        statements=statements,
        version=digest,
        drafts=drafts,
    )


def obligations(source_text: str, language: str, glossary: Glossary) -> list[tuple[str, str]]:
    """(term, required target rendering) for every glossary term in the source."""
    return [
        (entry["term"], str(entry[language]))
        for entry in glossary.renderings
        if mentions(entry["term"], source_text)
    ]


def keep_english_terms(source_text: str, glossary: Glossary) -> list[str]:
    return [term for term in glossary.keep_english if mentions(term, source_text)]


def enforce(
    *,
    source_text: str,
    translated: str,
    language: str,
    glossary: Glossary,
    locked_id: str | None = None,
) -> tuple[str, list[Change]]:
    """Make the translation satisfy the glossary, and say what had to change.

    Three rules, in PRD D7's order of authority:

    1. A LOCKED segment must EQUAL its approved rendering. Nothing is patched
       into it -- it is replaced wholesale, because a locked statement is a
       legal text, not a phrase to repair.
    2. Every glossary term present in the English source must appear in the
       target with its approved rendering.
    3. Every `keep_english` term present in the source must survive in Latin
       script.

    Rules 2 and 3 cannot always be satisfied by editing in place: if Mayura
    dropped a term entirely there is no wrong text to swap out. In that case the
    enforcer appends the required form and records the change as an append, so a
    reviewer sees a segment that reads awkwardly rather than one that silently
    lost an obligation. Appending is the conservative failure: a clumsy sentence
    is caught by the fidelity judge and a human, a missing obligation is not.
    """
    changes: list[Change] = []

    if locked_id is not None:
        statement = glossary.statements.get(locked_id)
        if statement is None:
            raise KeyError(f"Unknown locked statement: {locked_id}")
        approved = str(statement[language]).strip()
        if translated.strip() != approved:
            changes.append(
                Change(
                    from_text=translated.strip(),
                    to_text=approved,
                    reason=f"locked segment must equal approved rendering {locked_id}",
                    enforced=True,
                )
            )
        return approved, changes

    text = translated
    for term, required in obligations(source_text, language, glossary):
        if required in text:
            continue
        text = f"{text.rstrip()} {required}".strip() if text.strip() else required
        changes.append(
            Change(
                from_text="",
                to_text=required,
                reason=f"approved rendering for '{term}' was absent",
                enforced=True,
            )
        )
    for term in keep_english_terms(source_text, glossary):
        if mentions(term, text):
            continue
        text = f"{text.rstrip()} {term}".strip() if text.strip() else term
        changes.append(
            Change(
                from_text="",
                to_text=term,
                reason=f"'{term}' is keep_english and was not retained",
                enforced=True,
            )
        )
    return text, changes


def satisfied(
    *,
    source_text: str,
    produced: str,
    language: str,
    glossary: Glossary,
    locked_id: str | None = None,
) -> bool:
    """Would the eval runner score this segment as adherent? Used by tests."""
    if locked_id is not None:
        return produced.strip() == str(glossary.statements[locked_id][language]).strip()
    return all(
        required in produced for _, required in obligations(source_text, language, glossary)
    ) and all(mentions(term, produced) for term in keep_english_terms(source_text, glossary))


def resolve_locked_id(source_text: str, glossary: Glossary) -> str | None:
    """Which approved statement this locked segment is, by its English text.

    PRD D4 step 1 lets the system mark locked segments "by matching
    approved_renderings.yaml", so the link is the English statement itself
    rather than a column. Matching is exact on stripped text: a locked segment
    that has drifted from the approved English is not the approved statement,
    and silently treating it as one would let an edited obligation inherit an
    approved translation.
    """
    needle = source_text.strip()
    for statement_id, statement in glossary.statements.items():
        if str(statement["en"]).strip() == needle:
            return statement_id
    return None
