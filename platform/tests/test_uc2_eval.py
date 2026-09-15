"""Tests for the UC2 eval harness (uc2/P1).

The load-bearing one is `test_baseline_reports_zero_adherence`: it pins the
acceptance criterion that an untranslated baseline scores 0%, which is what
proves the terminology check discriminates rather than passing everything.
"""

import pytest
import yaml
from indic_platform.eval.runners import run_uc2
from indic_platform.eval.runners.run_uc2 import (
    FidelityVerdict,
    QuizItem,
    Segment,
    evaluate,
    load_quiz,
    load_references,
    load_segments,
    load_terminology,
    score_quiz,
)


def test_golden_set_shape() -> None:
    segments = load_segments()
    references = load_references()
    quiz = load_quiz()
    assert len(segments) == 60, "3 modules x 20 segments"
    assert len(references) == 90, "30 reference segments x 3 languages"
    assert len(quiz) == 30
    assert len({(s.module_id, s.seg_id) for s in segments}) == 60
    assert len({q.item_id for q in quiz}) == 30
    assert all(s.end_ms > s.start_ms for s in segments)
    assert sum(1 for s in segments if s.locked) == 9


def test_every_locked_segment_resolves_to_a_statement() -> None:
    _, renderings = load_terminology()
    ids = {s["id"] for s in renderings["statements"]}
    for segment in load_segments():
        if segment.locked:
            assert segment.locked_id in ids, f"{segment.module_id}#{segment.seg_id}"


def test_references_carry_replaceable_draft_structure() -> None:
    """A reviewer replaces `reference_text` and flips `status`; nothing else moves."""
    for ref in load_references():
        assert ref.status == "draft"
        assert ref.reference_text.strip()
        assert ref.origin
        assert ref.source_text.strip()


def test_glossary_meets_the_prompt_minimums() -> None:
    glossary, renderings = load_terminology()
    assert len(glossary["keep_english"]) + len(glossary["approved_renderings"]) >= 40
    assert len(glossary["approved_renderings"]) >= 15
    assert len(renderings["statements"]) == 5
    for entry in glossary["approved_renderings"]:
        for language in run_uc2.LANGUAGES:
            assert entry[language].strip()
    for statement in renderings["statements"]:
        for language in run_uc2.LANGUAGES:
            assert statement[language].strip()


def test_baseline_reports_zero_adherence() -> None:
    """uc2/P1 acceptance: the untouched source scores 0%, proving the check works."""
    report = evaluate(strict=False)
    assert report.metrics["terminology_adherence"] == 0.0
    assert report.metrics["terminology_expectations"] > 0
    assert report.quality_gates["terminology_adherence"] is False
    assert report.passed, "the harness itself still ran"


def test_baseline_keeps_english_terms_trivially() -> None:
    """Why keep_english is reported apart from adherence: the baseline aces it."""
    report = evaluate(strict=False)
    assert report.metrics["keep_english_retention"] == 1.0


def perfect(segment: Segment, language: str) -> str:
    """A translator that satisfies every target-language obligation."""
    glossary, renderings = load_terminology()
    if segment.locked and segment.locked_id:
        statement = next(s for s in renderings["statements"] if s["id"] == segment.locked_id)
        return str(statement[language])
    parts = [
        entry[language]
        for entry in glossary["approved_renderings"]
        if run_uc2.mentions(entry["term"], segment.source_text)
    ]
    parts += [term for term in run_uc2.keep_english_expectations(segment, glossary)]
    return " ".join(parts) or "x"


def test_perfect_translator_reaches_full_adherence() -> None:
    report = evaluate(translate=perfect, strict=False)
    assert report.metrics["terminology_adherence"] == 1.0
    assert report.metrics["keep_english_retention"] == 1.0
    assert report.quality_gates["terminology_adherence"] is True


def test_locked_segments_require_exact_equality() -> None:
    """A near-miss on a LOCKED statement is a failure, not a pass."""

    def nearly(segment: Segment, language: str) -> str:
        text = perfect(segment, language)
        return text + " ." if segment.locked else text

    report = evaluate(translate=nearly, strict=False)
    assert report.metrics["terminology_adherence"] < 1.0
    assert any(d["check"] == "terminology:locked" for d in report.details)


def test_dropping_a_keep_english_term_is_caught() -> None:
    def translated_mfa(segment: Segment, language: str) -> str:
        return perfect(segment, language).replace("MFA", "बहु-कारक")

    report = evaluate(translate=translated_mfa, strict=False)
    assert report.metrics["keep_english_retention"] < 1.0


def test_timing_fit_discriminates_on_length() -> None:
    timing = run_uc2.load_timing()
    segments = load_segments()

    def right_length(segment: Segment, language: str) -> str:
        cps = timing["languages"][language]["chars_per_second"]
        return "x" * int(segment.duration_s * cps)

    def far_too_long(segment: Segment, language: str) -> str:
        cps = timing["languages"][language]["chars_per_second"]
        return "x" * int(segment.duration_s * cps * 3)

    assert run_uc2.score_timing(segments, right_length, timing)["timing_fit_rate"] == 1.0
    assert run_uc2.score_timing(segments, far_too_long, timing)["timing_fit_rate"] == 0.0


def test_timing_config_is_measured_not_guessed() -> None:
    timing = run_uc2.load_timing()
    assert timing["measured"] is True
    assert timing["measured_from"]
    for language in run_uc2.LANGUAGES:
        assert 5.0 < timing["languages"][language]["chars_per_second"] < 30.0


def test_quiz_validity_catches_structural_faults() -> None:
    segments = load_segments()
    good = load_quiz()[0]
    bad = [
        good.model_copy(update={"item_id": 101, "seg_id": 999}),
        good.model_copy(update={"item_id": 102, "options": ["a", "b", "c"]}),
        good.model_copy(update={"item_id": 103, "answer": 9}),
        good.model_copy(update={"item_id": 104, "rationale": "   "}),
        good.model_copy(update={"item_id": 105, "options": ["a", "a", "b", "c"]}),
    ]
    metrics, details = score_quiz(bad, segments)
    assert metrics["quiz_validity"] == 0.0
    assert len(details) == 5


def test_quiz_rejects_an_item_that_gives_away_its_answer() -> None:
    segments = load_segments()
    leaky = QuizItem(
        module_id="sec-101",
        language="en-IN",
        item_id=200,
        seg_id=11,
        question="Is the window within 24 hours?",
        options=["within 24 hours", "a week", "a month", "never"],
        answer=0,
        rationale="The stem repeats the correct option.",
    )
    metrics, details = score_quiz([leaky], segments)
    assert metrics["quiz_validity"] == 0.0
    assert "question contains the correct option verbatim" in details[0]["problems"]  # type: ignore[operator]


def test_reference_quiz_items_all_pass() -> None:
    metrics, details = score_quiz(load_quiz(), load_segments())
    assert metrics["quiz_validity"] == 1.0, details


def test_fidelity_is_unmeasured_without_a_judge() -> None:
    report = evaluate(strict=False)
    assert "fidelity_mean" in report.unmeasured
    assert "fidelity_mean" not in report.metrics
    assert "fidelity_mean" not in report.quality_gates


def test_fidelity_is_measured_with_an_injected_judge() -> None:
    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        return FidelityVerdict(score=5 if produced != source else 1, reason="stub")

    report = evaluate(judge=judge, strict=False)
    assert report.metrics["fidelity_items"] == 90
    assert report.metrics["fidelity_mean"] == 1.0, "baseline leaves the source untouched"
    assert report.quality_gates["fidelity_mean"] is False
    assert "fidelity_mean" not in report.unmeasured

    report_ok = evaluate(translate=perfect, judge=judge, strict=False)
    assert report_ok.metrics["fidelity_mean"] == 5.0
    assert report_ok.quality_gates["fidelity_mean"] is True


def test_strict_mode_gates_on_b6_but_baseline_mode_does_not() -> None:
    assert evaluate(strict=False).passed is True
    assert evaluate(strict=True).passed is False


def test_stage_line_flags_provisional_provenance() -> None:
    report = evaluate(strict=False)
    assert "provisional" in report.stage
    assert "draft references" in report.stage


def test_judge_prompts_are_versioned_and_temperature_zero() -> None:
    prompts = run_uc2.Path(__file__).parents[1] / "eval" / "prompts"
    for name in ("uc2_backtranslate.md", "uc2_qa_judge.md"):
        text = (prompts / name).read_text()
        front = yaml.safe_load(text.split("---")[1])
        assert front["version"] >= 1
        assert front["temperature"] == 0
        assert front["model"] == "claude-haiku-4-5"


@pytest.mark.parametrize(
    "term,text,expected",
    [
        ("MFA", "MFA is mandatory", True),
        ("MFA", "mfa is mandatory", True),
        ("MFA", "MFAX is not the term", False),
        ("VPN", "your VPN credentials", True),
        ("policy", "policymaker", False),
    ],
)
def test_mentions_matches_whole_words_case_insensitively(
    term: str, text: str, expected: bool
) -> None:
    assert run_uc2.mentions(term, text) is expected
