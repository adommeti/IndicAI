"""Reviewer-facing assembly and the approval rules (PRD D4 step 7).

The reviewer UI shows, per segment: source, translation, back-translation, QA
score, glossary hits and the post-edit change log. Assembling that view is a
pure function over stage rows (`review_rows`), so it is testable without a
database and the UI can be driven from a fixture.

Approval is where the LOCKED rule finally bites. A locked segment whose text
does not equal its approved rendering cannot be approved by clicking approve:
the API requires an explicit override reason, stored with the reviewer id. That
is the difference between a control and a suggestion.
"""

from dataclasses import dataclass
from typing import Any

from training_localizer.terminology import Glossary, mentions, obligations

# An override has to say something. Ten characters is not a high bar; it exists
# so "ok" and "." cannot stand as the audit record for overriding a compliance
# statement's approved translation.
MIN_OVERRIDE_REASON = 10

FLAG_SCORE = 2  # D7: fidelity <= 2 is auto-flagged


@dataclass(frozen=True)
class ReviewRow:
    seg_id: int
    start_ms: int
    end_ms: int
    source_text: str
    locked: bool
    locked_id: str | None
    translation: str
    backtranslation: str | None
    qa_score: int | None
    qa_reason: str | None
    glossary_hits: list[str]
    glossary_misses: list[str]
    change_log: list[dict[str, Any]]
    approved: bool
    approved_by: str | None
    locked_mismatch: bool
    stage_version: int

    @property
    def flagged(self) -> bool:
        """The "flagged only" filter: low fidelity, a glossary miss, or a
        locked segment that does not match its approved rendering."""
        return (
            (self.qa_score is not None and self.qa_score <= FLAG_SCORE)
            or bool(self.glossary_misses)
            or self.locked_mismatch
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "seg_id": self.seg_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "source_text": self.source_text,
            "locked": self.locked,
            "locked_id": self.locked_id,
            "translation": self.translation,
            "backtranslation": self.backtranslation,
            "qa_score": self.qa_score,
            "qa_reason": self.qa_reason,
            "glossary_hits": self.glossary_hits,
            "glossary_misses": self.glossary_misses,
            "change_log": self.change_log,
            "approved": self.approved,
            "approved_by": self.approved_by,
            "locked_mismatch": self.locked_mismatch,
            "flagged": self.flagged,
            "stage_version": self.stage_version,
        }


def glossary_state(
    source_text: str, produced: str, language: str, glossary: Glossary
) -> tuple[list[str], list[str]]:
    """Which required terms this segment satisfies, and which it does not.

    Misses are what the reviewer is being asked to look at, so they are computed
    the same way the eval scores them -- the UI must not show a green tick for
    something `make eval-uc2` would count as a failure.
    """
    hits: list[str] = []
    misses: list[str] = []
    for term, required in obligations(source_text, language, glossary):
        (hits if required in produced else misses).append(term)
    for term in glossary.keep_english:
        if mentions(term, source_text):
            (hits if mentions(term, produced) else misses).append(term)
    return hits, misses


def locked_mismatch(
    produced: str, locked_id: str | None, language: str, glossary: Glossary
) -> bool:
    if locked_id is None:
        return False
    statement = glossary.statements.get(locked_id)
    if statement is None:
        return True  # an unknown statement is a mismatch, never a silent pass
    return produced.strip() != str(statement[language]).strip()


def review_rows(
    *,
    segments: list[dict[str, Any]],
    post_edit: dict[int, dict[str, Any]],
    backtranslate: dict[int, dict[str, Any]],
    approved: dict[int, dict[str, Any]],
    language: str,
    glossary: Glossary,
) -> list[ReviewRow]:
    """Build the review table from the stage rows of one module-language.

    `approved` wins where it exists: once a reviewer has saved a segment, that
    is the text under review, not the machine output it replaced.
    """
    rows: list[ReviewRow] = []
    for segment in sorted(segments, key=lambda s: int(s["seg_id"])):
        seg_id = int(segment["seg_id"])
        edited = approved.get(seg_id)
        machine = post_edit.get(seg_id, {})
        text = str(edited["text"]) if edited else str(machine.get("text", ""))
        back = backtranslate.get(seg_id, {})
        meta = dict(machine.get("meta") or {})
        hits, misses = glossary_state(str(segment["source_text"]), text, language, glossary)
        rows.append(
            ReviewRow(
                seg_id=seg_id,
                start_ms=int(segment["start_ms"]),
                end_ms=int(segment["end_ms"]),
                source_text=str(segment["source_text"]),
                locked=bool(segment["locked"]),
                locked_id=segment.get("locked_id"),
                translation=text,
                backtranslation=str(back["text"]) if back else None,
                qa_score=(dict(back.get("meta") or {}).get("qa_score") if back else None),
                qa_reason=(dict(back.get("meta") or {}).get("qa_reason") if back else None),
                glossary_hits=hits,
                glossary_misses=misses,
                change_log=list(meta.get("change_log") or []),
                approved=edited is not None,
                approved_by=str(edited["created_by"]) if edited else None,
                locked_mismatch=locked_mismatch(text, segment.get("locked_id"), language, glossary),
                stage_version=int((edited or machine).get("version", 0)),
            )
        )
    return rows


class OverrideRequired(Exception):
    """A locked segment does not match its approved rendering.

    Carries the approved text so the API can tell the reviewer what it expected
    rather than only that they were refused.
    """

    def __init__(self, locked_id: str, expected: str) -> None:
        super().__init__(f"Locked segment {locked_id} does not match its approved rendering")
        self.locked_id = locked_id
        self.expected = expected


def check_approval(
    *,
    text: str,
    locked_id: str | None,
    language: str,
    glossary: Glossary,
    override_reason: str | None,
) -> dict[str, Any]:
    """Decide whether this approval may proceed, and what to record.

    Returns the audit fields for the `approved` row. Raises `OverrideRequired`
    when the segment is locked, mismatched, and no adequate reason was given --
    the one path in this app where a reviewer must type something before a
    control can be set aside.
    """
    mismatch = locked_mismatch(text, locked_id, language, glossary)
    reason = (override_reason or "").strip()
    if mismatch:
        if len(reason) < MIN_OVERRIDE_REASON:
            expected = ""
            if locked_id and locked_id in glossary.statements:
                expected = str(glossary.statements[locked_id][language])
            raise OverrideRequired(locked_id or "unknown", expected)
        return {
            "locked_override": True,
            "override_reason": reason,
            "locked_id": locked_id,
            "glossary_version": glossary.version,
        }
    if reason and not mismatch:
        # A reason with nothing to override is recorded, not rejected: reviewers
        # leave notes, and dropping one silently would lose the only thing they
        # chose to write down.
        return {
            "locked_override": False,
            "override_reason": reason,
            "glossary_version": glossary.version,
        }
    return {"locked_override": False, "glossary_version": glossary.version}


def review_summary(rows: list[ReviewRow]) -> dict[str, Any]:
    return {
        "segments": len(rows),
        "approved": sum(1 for r in rows if r.approved),
        "flagged": sum(1 for r in rows if r.flagged),
        "locked": sum(1 for r in rows if r.locked),
        "locked_mismatches": sum(1 for r in rows if r.locked_mismatch),
        "glossary_misses": sum(len(r.glossary_misses) for r in rows),
        "fidelity_mean": (
            sum(r.qa_score for r in rows if r.qa_score is not None)
            / len([r for r in rows if r.qa_score is not None])
            if any(r.qa_score is not None for r in rows)
            else None
        ),
    }
