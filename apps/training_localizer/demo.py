"""An in-memory training localizer, for driving the reviewer UI without a stack.

`api.py` needs Postgres. This module serves the same two review endpoints from a
dict, seeded from the uc2/P1 golden set, so the UI can be exercised and timed in
a session with no Docker.

What it does NOT stub is the part under test: `review.review_rows` and
`review.check_approval` are the same functions the real API calls, so the
Playwright run measures the real review assembly, the real glossary scoring and
the real LOCKED rule. Only persistence is a dict.

Run it with `uv run python -m training_localizer.demo` (port 8000 by default).
It has no authentication and is never mounted by `api.py`; it exists for the
uc2/P3 timing measurement and for screenshots.
"""

import os
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from indic_platform.eval.runners.run_uc2 import load_segments
from pydantic import BaseModel, ConfigDict, Field

from training_localizer.review import (
    OverrideRequired,
    check_approval,
    review_rows,
    review_summary,
)
from training_localizer.terminology import enforce, load_glossary, resolve_locked_id

DEMO_MODULE = uuid.UUID("00000000-0000-4000-8000-000000000001")
REVIEWER = "demo.reviewer@example.test"

app = FastAPI(title="training localizer (demo)")
# The client sends `credentials: "include"` because the real deployment carries
# an SSO cookie, and a browser rejects a credentialed request against a wildcard
# origin -- the fetch fails before any handler sees it. Echo the caller's origin
# instead, restricted to loopback: this module is a local harness, not something
# to expose.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(127\.0\.0\.1|localhost)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class Store:
    """One module-language's stage rows, in memory."""

    def __init__(self, module: str = "sec-101", language: str = "hi-IN") -> None:
        self.glossary = load_glossary()
        self.language = language
        self.segments: list[dict[str, Any]] = [
            {
                "seg_id": s.seg_id,
                "start_ms": s.start_ms,
                "end_ms": s.end_ms,
                "source_text": s.source_text,
                "locked": s.locked,
                "locked_id": s.locked_id or resolve_locked_id(s.source_text, self.glossary),
            }
            for s in load_segments()
            if s.module_id == module
        ]
        self.post_edit: dict[int, dict[str, Any]] = {}
        self.backtranslate: dict[int, dict[str, Any]] = {}
        self.approved: dict[int, dict[str, Any]] = {}
        self.version = 0
        self._seed()

    def _seed(self) -> None:
        """Plausible pipeline output: enforced translations, a judge score, and
        deliberately, two locked segments left as raw machine text so the LOCKED
        mismatch path is visible without having to break something by hand."""
        for index, segment in enumerate(self.segments):
            locked_id = segment["locked_id"]
            leave_broken = segment["locked"] and index % 2 == 0
            if locked_id and not leave_broken:
                text = str(self.glossary.statements[locked_id][self.language])
                changes: list[dict[str, Any]] = []
            else:
                produced, enforced = enforce(
                    source_text=segment["source_text"],
                    translated=f"[{self.language}] {segment['source_text']}",
                    language=self.language,
                    glossary=self.glossary,
                    locked_id=None,  # deliberately unenforced so a mismatch shows
                )
                text = produced
                changes = [c.as_json() for c in enforced]
            self.post_edit[segment["seg_id"]] = {
                "text": text,
                "meta": {"change_log": changes, "glossary_version": self.glossary.version},
                "version": 1,
                "created_by": "uc2.post_edit",
            }
            score = 2 if index % 7 == 3 else 5 if index % 3 else 4
            self.backtranslate[segment["seg_id"]] = {
                "text": f"Back-translation of segment {segment['seg_id']}.",
                "meta": {
                    "qa_score": score,
                    "qa_reason": "meaning preserved" if score > 2 else "an obligation is unclear",
                },
                "version": 1,
                "created_by": "uc2.backtranslate_qa",
            }

    def rows(self) -> list[Any]:
        return review_rows(
            segments=self.segments,
            post_edit=self.post_edit,
            backtranslate=self.backtranslate,
            approved=self.approved,
            language=self.language,
            glossary=self.glossary,
        )


STORE = Store()
QUIZ: list[dict[str, Any]] = [
    {
        "item_id": index + 1,
        "language": "en-IN",
        "seg_id": STORE.segments[index]["seg_id"],
        "question": f"What should you do in the situation described in segment {index + 1}?",
        "options": ["Report it", "Ignore it", "Forward it to a colleague", "Reply with details"],
        "answer": 0,
        "rationale": "Reporting is the behaviour the module asks for.",
        "approved": False,
    }
    for index in range(5)
]


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str
    text: str = Field(min_length=1)
    override_reason: str | None = None


class QuizApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str
    approved: bool = True


@app.get("/api/modules/{module_id}/review")
def review(module_id: str, language: str = "hi-IN") -> dict[str, Any]:
    if language != STORE.language:
        raise HTTPException(404, "The demo store holds one language")
    rows = STORE.rows()
    return {
        "module_id": module_id,
        "language": language,
        "reviewer": REVIEWER,
        "summary": review_summary(rows),
        "segments": [row.as_json() for row in rows],
        "quiz_items": QUIZ,
    }


@app.put("/api/modules/{module_id}/segments/{seg_id}/approve")
def approve(module_id: str, seg_id: int, body: ApproveIn) -> dict[str, Any]:
    segment = next((s for s in STORE.segments if s["seg_id"] == seg_id), None)
    if segment is None:
        raise HTTPException(404, "Unknown segment")
    try:
        audit = check_approval(
            text=body.text,
            locked_id=segment["locked_id"],
            language=body.language,
            glossary=STORE.glossary,
            override_reason=body.override_reason,
        )
    except OverrideRequired as exc:
        raise HTTPException(
            409,
            {
                "error": "locked_override_required",
                "locked_id": exc.locked_id,
                "expected": exc.expected,
                "hint": (
                    "This is a LOCKED compliance statement. Restore the approved rendering, "
                    "or supply override_reason of at least 10 characters."
                ),
            },
        ) from exc
    STORE.version += 1
    STORE.approved[seg_id] = {
        "text": body.text,
        "meta": {**audit, "reviewer": REVIEWER},
        "version": STORE.version,
        "created_by": REVIEWER,
    }
    return {"seg_id": seg_id, "version": STORE.version, **audit}


@app.put("/api/modules/{module_id}/quiz/{item_id}/approve")
def approve_quiz(module_id: str, item_id: int, body: QuizApproveIn) -> dict[str, Any]:
    for item in QUIZ:
        if item["item_id"] == item_id:
            item["approved"] = body.approved
            return {"item_id": item_id, "approved": body.approved}
    raise HTTPException(404, "Unknown quiz item")


@app.post("/api/demo/reset")
def reset() -> dict[str, str]:
    """Playwright calls this so a timing run always starts from zero approvals."""
    global STORE
    STORE = Store()
    for item in QUIZ:
        item["approved"] = False
    return {"status": "reset", "module_id": str(DEMO_MODULE)}


def main() -> None:
    import uvicorn

    uvicorn.run(
        app, host="127.0.0.1", port=int(os.environ.get("DEMO_PORT", "8000")), log_level="warning"
    )


if __name__ == "__main__":
    main()
