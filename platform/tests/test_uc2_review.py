"""Reviewer-view assembly and the approval rules (uc2/P3).

The load-bearing test is `test_a_locked_mismatch_cannot_be_approved_without_a_reason`:
PRD D4's LOCKED control is only a control if the API refuses, and refuses in a
way that records who overrode it and why.
"""

from typing import Any

import pytest
from indic_platform.eval.runners.run_uc2 import load_segments
from training_localizer.review import (
    MIN_OVERRIDE_REASON,
    OverrideRequired,
    ReviewRow,
    check_approval,
    glossary_state,
    locked_mismatch,
    review_rows,
    review_summary,
)
from training_localizer.terminology import Glossary, load_glossary, resolve_locked_id


@pytest.fixture
def glossary() -> Glossary:
    return load_glossary()


def module_segments(module_id: str = "sec-101") -> list[dict[str, Any]]:
    glossary = load_glossary()
    return [
        {
            "seg_id": s.seg_id,
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "source_text": s.source_text,
            "locked": s.locked,
            "locked_id": s.locked_id or resolve_locked_id(s.source_text, glossary),
        }
        for s in load_segments()
        if s.module_id == module_id
    ]


# --- LOCKED enforcement -------------------------------------------------------


def test_a_locked_mismatch_cannot_be_approved_without_a_reason(glossary: Glossary) -> None:
    """uc2/P3 acceptance: LOCKED mismatch cannot be approved without an override."""
    with pytest.raises(OverrideRequired) as caught:
        check_approval(
            text="कुछ और पाठ जो स्वीकृत नहीं है।",
            locked_id="lock-mfa-mandatory",
            language="hi-IN",
            glossary=glossary,
            override_reason=None,
        )
    assert caught.value.locked_id == "lock-mfa-mandatory"
    assert caught.value.expected == str(glossary.statements["lock-mfa-mandatory"]["hi-IN"])


@pytest.mark.parametrize(
    "reason", ["", "   ", "ok", "no", "fine.", "a" * (MIN_OVERRIDE_REASON - 1)]
)
def test_a_token_override_reason_is_not_an_override(reason: str, glossary: Glossary) -> None:
    """ "ok" is not an audit record for setting aside a compliance statement."""
    with pytest.raises(OverrideRequired):
        check_approval(
            text="बदला हुआ पाठ",
            locked_id="lock-report-24h",
            language="ta-IN",
            glossary=glossary,
            override_reason=reason,
        )


def test_an_adequate_override_is_recorded_not_just_allowed(glossary: Glossary) -> None:
    reason = "Legal approved a revised wording on 2026-09-15, ticket LEG-441."
    audit = check_approval(
        text="बदला हुआ पाठ",
        locked_id="lock-report-24h",
        language="hi-IN",
        glossary=glossary,
        override_reason=reason,
    )
    assert audit["locked_override"] is True
    assert audit["override_reason"] == reason
    assert audit["locked_id"] == "lock-report-24h"
    assert audit["glossary_version"] == glossary.version


def test_the_approved_rendering_itself_needs_no_override(glossary: Glossary) -> None:
    approved = str(glossary.statements["lock-mfa-mandatory"]["te-IN"])
    audit = check_approval(
        text=approved,
        locked_id="lock-mfa-mandatory",
        language="te-IN",
        glossary=glossary,
        override_reason=None,
    )
    assert audit["locked_override"] is False


def test_an_unlocked_segment_is_never_blocked(glossary: Glossary) -> None:
    audit = check_approval(
        text="anything the reviewer wants",
        locked_id=None,
        language="hi-IN",
        glossary=glossary,
        override_reason=None,
    )
    assert audit["locked_override"] is False


def test_a_note_on_an_unlocked_segment_is_kept(glossary: Glossary) -> None:
    """Reviewers leave notes; dropping one silently loses the only thing they wrote."""
    audit = check_approval(
        text="fine",
        locked_id=None,
        language="hi-IN",
        glossary=glossary,
        override_reason="Reworded for the Mumbai cohort, see thread.",
    )
    assert audit["locked_override"] is False
    assert audit["override_reason"].startswith("Reworded")


def test_an_unknown_locked_statement_is_a_mismatch_not_a_pass(glossary: Glossary) -> None:
    assert locked_mismatch("whatever", "lock-does-not-exist", "hi-IN", glossary) is True


# --- the review table ---------------------------------------------------------


def rows_for(
    language: str = "hi-IN",
    *,
    post_edit: dict[int, dict[str, Any]] | None = None,
    backtranslate: dict[int, dict[str, Any]] | None = None,
    approved: dict[int, dict[str, Any]] | None = None,
) -> list[ReviewRow]:
    return review_rows(
        segments=module_segments(),
        post_edit=post_edit or {},
        backtranslate=backtranslate or {},
        approved=approved or {},
        language=language,
        glossary=load_glossary(),
    )


def test_review_rows_carry_every_column_the_prompt_names() -> None:
    """source | translation | back-translation | QA score | glossary hits | change log."""
    segments = module_segments()
    first = segments[0]["seg_id"]
    rows = rows_for(
        post_edit={
            first: {
                "text": "अनुवादित पाठ GMO",
                "meta": {"change_log": [{"from": "", "to": "GMO", "reason": "keep_english"}]},
                "version": 1,
                "created_by": "uc2.post_edit",
            }
        },
        backtranslate={
            first: {
                "text": "Translated text",
                "meta": {"qa_score": 4, "qa_reason": "same meaning"},
                "version": 1,
                "created_by": "uc2.backtranslate_qa",
            }
        },
    )
    row = rows[0]
    assert row.source_text and row.translation == "अनुवादित पाठ GMO"
    assert row.backtranslation == "Translated text"
    assert row.qa_score == 4 and row.qa_reason == "same meaning"
    assert row.change_log[0]["to"] == "GMO"
    assert "GMO" in row.glossary_hits
    assert set(row.as_json()) >= {
        "source_text",
        "translation",
        "backtranslation",
        "qa_score",
        "glossary_hits",
        "glossary_misses",
        "change_log",
        "locked",
        "locked_mismatch",
        "flagged",
    }


def test_an_approved_edit_replaces_the_machine_text_in_the_view() -> None:
    segments = module_segments()
    first = segments[0]["seg_id"]
    rows = rows_for(
        post_edit={first: {"text": "machine", "meta": {}, "version": 1, "created_by": "uc2"}},
        approved={
            first: {"text": "reviewer text", "meta": {}, "version": 3, "created_by": "asha@example"}
        },
    )
    assert rows[0].translation == "reviewer text"
    assert rows[0].approved is True
    assert rows[0].approved_by == "asha@example"
    assert rows[0].stage_version == 3


def test_flagged_filter_catches_low_score_glossary_miss_and_locked_mismatch() -> None:
    segments = module_segments()
    plain = next(s for s in segments if not s["locked"])
    locked = next(s for s in segments if s["locked"])

    low = rows_for(
        post_edit={plain["seg_id"]: {"text": "x", "meta": {}, "version": 1, "created_by": "u"}},
        backtranslate={
            plain["seg_id"]: {"text": "b", "meta": {"qa_score": 2}, "version": 1, "created_by": "u"}
        },
    )
    assert next(r for r in low if r.seg_id == plain["seg_id"]).flagged is True

    # A locked segment left as the machine's raw text mismatches its rendering.
    mismatch = rows_for(
        post_edit={locked["seg_id"]: {"text": "गलत", "meta": {}, "version": 1, "created_by": "u"}}
    )
    row = next(r for r in mismatch if r.seg_id == locked["seg_id"])
    assert row.locked_mismatch is True and row.flagged is True


def test_a_clean_segment_is_not_flagged(glossary: Glossary) -> None:
    from training_localizer.terminology import enforce

    segments = module_segments()
    plain = next(s for s in segments if not s["locked"])
    text, _ = enforce(
        source_text=plain["source_text"],
        translated="कुछ अनुवाद",
        language="hi-IN",
        glossary=glossary,
    )
    rows = rows_for(
        post_edit={plain["seg_id"]: {"text": text, "meta": {}, "version": 1, "created_by": "u"}},
        backtranslate={
            plain["seg_id"]: {"text": "b", "meta": {"qa_score": 5}, "version": 1, "created_by": "u"}
        },
    )
    row = next(r for r in rows if r.seg_id == plain["seg_id"])
    assert row.glossary_misses == []
    assert row.flagged is False


def test_glossary_state_agrees_with_the_enforcer(glossary: Glossary) -> None:
    """The UI must not show a tick for what the eval counts as a miss."""
    from training_localizer.terminology import enforce, satisfied

    for segment in module_segments():
        if segment["locked"]:
            continue
        text, _ = enforce(
            source_text=segment["source_text"],
            translated="zzz",
            language="te-IN",
            glossary=glossary,
        )
        _, misses = glossary_state(segment["source_text"], text, "te-IN", glossary)
        assert misses == [], segment["seg_id"]
        assert satisfied(
            source_text=segment["source_text"],
            produced=text,
            language="te-IN",
            glossary=glossary,
        )


def test_summary_counts_what_the_reviewer_has_left_to_do() -> None:
    segments = module_segments()
    rows = rows_for(
        post_edit={
            s["seg_id"]: {"text": "x", "meta": {}, "version": 1, "created_by": "u"}
            for s in segments
        },
        backtranslate={
            segments[0]["seg_id"]: {
                "text": "b",
                "meta": {"qa_score": 5},
                "version": 1,
                "created_by": "u",
            }
        },
        approved={
            segments[0]["seg_id"]: {
                "text": "x",
                "meta": {},
                "version": 1,
                "created_by": "asha@example",
            }
        },
    )
    summary = review_summary(rows)
    assert summary["segments"] == 20
    assert summary["approved"] == 1
    assert summary["locked"] == 4, "sec-101 has 4 locked segments"
    assert summary["locked_mismatches"] == 4, "all four are still the placeholder text"
    assert summary["fidelity_mean"] == 5.0


def test_summary_reports_no_fidelity_rather_than_zero_when_the_judge_has_not_run() -> None:
    assert review_summary(rows_for())["fidelity_mean"] is None


# --- API surface --------------------------------------------------------------


def client(authed: bool = True) -> Any:
    from fastapi.testclient import TestClient
    from training_localizer.api import app, authenticated_owner

    if authed:
        app.dependency_overrides[authenticated_owner] = lambda: "asha@example.test"
    else:
        app.dependency_overrides.clear()
    return TestClient(app)


def clear_overrides() -> None:
    from training_localizer.api import app

    app.dependency_overrides.clear()


MODULE = "00000000-0000-4000-8000-000000000001"


def test_review_and_approve_fail_closed_without_sso() -> None:
    api = client(authed=False)
    assert api.get(f"/modules/{MODULE}/review").status_code == 401
    assert (
        api.put(
            f"/modules/{MODULE}/segments/1/approve",
            json={"language": "hi-IN", "text": "x"},
        ).status_code
        == 401
    )
    assert (
        api.put(f"/modules/{MODULE}/quiz/1/approve", json={"language": "en-IN"}).status_code == 401
    )


def test_approve_rejects_a_body_it_does_not_understand() -> None:
    api = client()
    try:
        for body in (
            {"language": "fr-FR", "text": "x"},
            {"language": "hi-IN", "text": ""},
            {"language": "hi-IN", "text": "x", "reviewer": "someone-else"},
            {"text": "x"},
        ):
            assert api.put(f"/modules/{MODULE}/segments/1/approve", json=body).status_code == 422, (
                body
            )
    finally:
        clear_overrides()


def test_the_client_cannot_name_its_own_reviewer() -> None:
    """Identity comes from the SSO dependency; a body field must not override it."""
    api = client()
    try:
        response = api.put(
            f"/modules/{MODULE}/segments/1/approve",
            json={"language": "hi-IN", "text": "x", "reviewer": "ceo@example.test"},
        )
        assert response.status_code == 422
    finally:
        clear_overrides()


# --- the demo harness the E2E drives ------------------------------------------


def test_the_demo_store_seeds_a_locked_mismatch_to_review() -> None:
    """The Playwright timing run needs a module with something actually wrong in
    it; if the seed ever came out clean the E2E would prove nothing."""
    from training_localizer.demo import Store

    store = Store()
    rows = store.rows()
    assert len(rows) == 20
    assert sum(1 for r in rows if r.locked_mismatch) == 3
    assert sum(1 for r in rows if r.flagged) >= 3
    assert sum(1 for r in rows if r.locked) == 4


def test_the_demo_refuses_a_locked_mismatch_the_same_way_the_api_does() -> None:
    from fastapi.testclient import TestClient
    from training_localizer.demo import app as demo_app

    api = TestClient(demo_app)
    review_payload = api.get(f"/api/modules/{MODULE}/review?language=hi-IN").json()
    mismatch = next(s for s in review_payload["segments"] if s["locked_mismatch"])

    refused = api.put(
        f"/api/modules/{MODULE}/segments/{mismatch['seg_id']}/approve",
        json={"language": "hi-IN", "text": mismatch["translation"]},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "locked_override_required"

    token = api.put(
        f"/api/modules/{MODULE}/segments/{mismatch['seg_id']}/approve",
        json={"language": "hi-IN", "text": mismatch["translation"], "override_reason": "ok"},
    )
    assert token.status_code == 409, "a token reason is not an override"

    accepted = api.put(
        f"/api/modules/{MODULE}/segments/{mismatch['seg_id']}/approve",
        json={
            "language": "hi-IN",
            "text": mismatch["translation"],
            "override_reason": "Legal approved revised wording, ticket LEG-441.",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["locked_override"] is True
    api.post("/api/demo/reset")
