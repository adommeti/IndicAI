"""Tests for the UC2 eval harness (uc2/P1).

The load-bearing one is `test_baseline_reports_zero_adherence`: it pins the
acceptance criterion that an untranslated baseline scores 0%, which is what
proves the terminology check discriminates rather than passing everything.
"""

import pytest
import yaml
from indic_platform.eval.aio import close_batch
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


def term_stuffer(segment: Segment, language: str) -> str:
    """Satisfies every terminology obligation and nothing else.

    Deliberately NOT a good translation: outside LOCKED segments it returns the
    required strings concatenated, i.e. word salad. It exists to prove the
    terminology check reaches 100% when the obligations are met, and it doubles
    as the demonstration that adherence alone is a *stuffable* metric --- see
    `test_terminology_adherence_alone_is_stuffable`. Fidelity is the check that
    catches this, which is why B6 gates on both."""
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


def test_terminology_adherence_alone_is_stuffable() -> None:
    """The documented limit of the metric: word salad containing every required
    string scores 100% adherence. Adherence proves terms were not mistranslated;
    it does not prove the sentence means anything. B6 pairs it with fidelity >= 4
    for exactly this reason, and this test exists so nobody reads a green
    adherence number as "the translation is good"."""
    report = evaluate(translate=term_stuffer, strict=False)
    assert report.metrics["terminology_adherence"] == 1.0

    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        # A real judge scores concatenated glossary terms as meaning-lost.
        return FidelityVerdict(score=1, reason="word salad", lost_or_changed=["everything"])

    stuffed = evaluate(translate=term_stuffer, judge=judge, strict=False, fidelity_source="sut")
    assert stuffed.quality_gates["terminology_adherence"] is True
    assert stuffed.quality_gates["fidelity_mean"] is False, "fidelity catches the stuffing"
    assert stuffed.passed is True, "baseline mode reports, it does not gate"
    assert (
        evaluate(translate=term_stuffer, judge=judge, strict=True, fidelity_source="sut").passed
        is False
    ), "strict mode blocks it"


def test_term_stuffer_reaches_full_adherence() -> None:
    report = evaluate(translate=term_stuffer, strict=False)
    assert report.metrics["terminology_adherence"] == 1.0
    assert report.metrics["keep_english_retention"] == 1.0
    assert report.quality_gates["terminology_adherence"] is True


def test_locked_segments_require_exact_equality() -> None:
    """A near-miss on a LOCKED statement is a failure, not a pass."""

    def nearly(segment: Segment, language: str) -> str:
        text = term_stuffer(segment, language)
        return text + " ." if segment.locked else text

    report = evaluate(translate=nearly, strict=False)
    assert report.metrics["terminology_adherence"] < 1.0
    assert any(d["check"] == "terminology:locked" for d in report.details)


def test_dropping_a_keep_english_term_is_caught() -> None:
    def translated_mfa(segment: Segment, language: str) -> str:
        return term_stuffer(segment, language).replace("MFA", "बहु-कारक")

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


def test_b6_metrics_p1_cannot_produce_are_declared_with_a_reason() -> None:
    """eval.md: every B6 metric is measured or reported unmeasured with a reason."""
    report = evaluate(strict=False)
    reasons = {d["metric"]: d["reason"] for d in report.details if d["check"] == "unmeasured"}
    for metric in ("quiz_pass_rate_vs_english_control", "reviewer_edit_rate"):
        assert metric in report.unmeasured
        assert metric not in report.metrics, "never a passing placeholder"
        assert metric not in report.quality_gates
        assert reasons[metric].strip()
    assert reasons["fidelity_mean"].strip()


def test_fidelity_is_unmeasured_without_a_judge() -> None:
    report = evaluate(strict=False)
    assert "fidelity_mean" in report.unmeasured
    assert "fidelity_mean" not in report.metrics
    assert "fidelity_mean" not in report.quality_gates


def test_fidelity_is_measured_with_an_injected_judge() -> None:
    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        return FidelityVerdict(score=5 if produced != source else 1, reason="stub")

    report = evaluate(judge=judge, strict=False, fidelity_source="sut")
    assert report.metrics["fidelity_items"] == 90
    assert report.metrics["fidelity_mean"] == 1.0, "baseline leaves the source untouched"
    assert report.quality_gates["fidelity_mean"] is False
    assert "fidelity_mean" not in report.unmeasured

    report_ok = evaluate(translate=term_stuffer, judge=judge, strict=False, fidelity_source="sut")
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


class FakeClaude:
    """Stands in for the Claude adapter so the judge path runs without a key.

    It records every call so the test can assert the two D7 legs actually went
    out with the versioned rubrics and the right schemas.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def structured(self, *, system: str, user: str, schema: type, model: str):  # type: ignore[no-untyped-def]
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model})
        if schema is run_uc2.Backtranslation:
            return run_uc2.Backtranslation(english="back-translated English")
        return FidelityVerdict(score=4, reason="ok", lost_or_changed=[])


def test_claude_judge_runs_both_d7_legs_against_a_mocked_adapter() -> None:
    """Regression: `claude_judge` must be constructible and callable, not dead code."""
    fake = FakeClaude()
    judge = run_uc2.claude_judge("claude-haiku-4-5", client=fake)
    try:
        verdict = judge("Report phishing within 24 hours.", "24 घंटे के भीतर सूचित करें।", "hi-IN")
    finally:
        close_batch(judge)

    assert verdict.score == 4
    assert len(fake.calls) == 2, "backtranslate then judge"
    back, score = fake.calls
    assert back["schema"] is run_uc2.Backtranslation
    assert back["user"] == "24 घंटे के भीतर सूचित करें।", "the judge never sees the source"
    assert "hi-IN" in str(back["system"]) and "{language}" not in str(back["system"])
    assert score["schema"] is FidelityVerdict
    assert "SOURCE:" in str(score["user"]) and "BACKTRANSLATION:" in str(score["user"])
    assert "back-translated English" in str(score["user"])
    assert all(call["model"] == "claude-haiku-4-5" for call in fake.calls)


def test_judge_legs_go_through_the_temperature_zero_adapter_path() -> None:
    """Both rubrics declare temperature 0; `structured` is the adapter call that
    sets it, so the judge must not reach for `stream_text`."""
    fake = FakeClaude()
    judge = run_uc2.claude_judge("claude-haiku-4-5", client=fake)
    try:
        judge("Source text.", "लक्ष्य पाठ।", "hi-IN")
    finally:
        close_batch(judge)
    assert not hasattr(fake, "stream_text_called")
    for call in fake.calls:
        assert call["system"], "system prompt is the cached, versioned rubric"


def test_fidelity_default_source_is_the_reference_translations() -> None:
    """P1 asks for the judge implemented against the reference translations."""
    judged: list[str] = []

    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        judged.append(produced)
        return FidelityVerdict(score=5, reason="stub")

    references = load_references()
    metrics, _, _ = run_uc2.score_fidelity(references, run_uc2.baseline, judge)
    assert metrics["fidelity_items"] == 90
    assert judged == [r.reference_text for r in references]
    assert all(r.source_text not in judged for r in references[:5]), "not the English source"

    judged.clear()
    run_uc2.score_fidelity(references, run_uc2.baseline, judge, source="sut")
    assert judged == [r.source_text for r in references], "sut mode judges the translator"


def test_quiz_ids_may_repeat_across_modules() -> None:
    """D6 keys quiz_items on (module_id, language, item_id), not item_id alone."""
    segments = load_segments()
    good = load_quiz()[0]
    other = next(s for s in segments if s.module_id != good.module_id)
    twin = good.model_copy(update={"module_id": other.module_id, "seg_id": other.seg_id})
    metrics, details = score_quiz([good, twin], segments)
    assert metrics["quiz_validity"] == 1.0, details

    same_module = good.model_copy()
    metrics, details = score_quiz([good, same_module], segments)
    assert metrics["quiz_validity"] == 0.5
    assert "duplicate (module_id, language, item_id)" in details[0]["problems"]  # type: ignore[operator]


def test_timing_flags_its_own_degeneracy_on_the_baseline() -> None:
    """The golden durations were authored at the en-IN rate, so an untranslated
    baseline gives a per-language constant ratio and the fit rate is 0 or 1 per
    language, never a measurement of the text. The run must say so."""
    report = evaluate(strict=False)
    assert report.metrics["timing_durations_synthetic"] == 1.0
    per_language = [report.metrics[f"timing_fit_rate_{lang}"] for lang in run_uc2.LANGUAGES]
    assert set(per_language) <= {0.0, 1.0}, "degenerate by construction"
    assert "golden durations authored at the en-IN rate" in report.stage

    timing = run_uc2.load_timing()
    segments = load_segments()

    def varied(segment: Segment, language: str) -> str:
        cps = timing["languages"][language]["chars_per_second"]
        scale = 1.0 if segment.seg_id % 2 else 2.5
        return "x" * int(segment.duration_s * cps * scale)

    metrics = run_uc2.score_timing(segments, varied, timing)
    assert metrics["timing_durations_synthetic"] == 0.0, "real variation is not flagged"
    assert 0.0 < metrics["timing_fit_rate"] < 1.0


def test_translator_is_called_once_per_segment_and_language() -> None:
    """Terminology, timing and fidelity must score the same text, and a real SUT
    costs money per call."""
    calls: list[tuple[str, int, str]] = []

    def counting(segment: Segment, language: str) -> str:
        calls.append((segment.module_id, segment.seg_id, language))
        return segment.source_text

    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        return FidelityVerdict(score=5, reason="stub")

    evaluate(translate=counting, judge=judge, strict=False, fidelity_source="sut")
    assert len(calls) == len(set(calls)) == 180, "60 segments x 3 languages, once each"


def test_live_run_prints_an_inr_and_usd_estimate() -> None:
    """.claude/rules/eval.md: an estimated cost is printed before a live batch."""
    line = run_uc2.estimate_judge_cost(load_references(), "claude-haiku-4-5")
    assert "180 calls" in line and "$" in line and "Rs " in line
    unpriced = run_uc2.estimate_judge_cost(load_references(), "no-such-model")
    assert "cost unknown" in unpriced, "never invent a price"


def test_eval_and_app_judge_prompts_agree() -> None:
    """PRD D7 ships these rubrics under the app too. While the app-side copies do
    not exist (uc2/P2 creates them) this asserts nothing; once they do, it keeps
    the two texts from drifting apart silently."""
    eval_dir = run_uc2.Path(__file__).parents[1] / "eval" / "prompts"
    app_dir = run_uc2.Path(__file__).parents[2] / "apps" / "training_localizer" / "prompts"
    for name in ("uc2_backtranslate.md", "uc2_qa_judge.md"):
        app_copy = app_dir / name.removeprefix("uc2_")
        if not app_copy.exists():
            continue
        assert run_uc2.prompt_body(app_copy) == run_uc2.prompt_body(eval_dir / name), name


def test_pre_edit_adherence_is_the_number_that_can_fail() -> None:
    """`terminology_adherence` alone cannot fail against this pipeline.

    `training_localizer.terminology.enforce` implements exactly the predicate
    `score_terminology` tests, so a translator that returns the literal string
    "zzz" and then enforces scores a perfect 1.0 -- identical to a real run.
    That is why `--pre-edit` exists: it scores the text before the enforcer,
    which is the number that says what the translation vendor actually did.
    """
    from training_localizer.terminology import enforce, load_glossary, resolve_locked_id

    glossary = load_glossary()

    def useless(segment: Segment, language: str) -> str:
        return "zzz"

    def useless_then_enforced(segment: Segment, language: str) -> str:
        text, _ = enforce(
            source_text=segment.source_text,
            translated="zzz",
            language=language,
            glossary=glossary,
            locked_id=segment.locked_id or resolve_locked_id(segment.source_text, glossary),
        )
        return text

    report = evaluate(translate=useless_then_enforced, pre_edit=useless, strict=False)
    assert report.metrics["terminology_adherence"] == 1.0, "enforcement always wins"
    assert report.metrics["terminology_adherence_pre_edit"] == 0.0, (
        "and the pre-edit metric exposes that nothing was translated"
    )
    assert report.metrics["keep_english_retention_pre_edit"] == 0.0
    assert "terminology_adherence_pre_edit" not in report.quality_gates, "reported, not gated"


def test_pre_edit_metrics_are_absent_unless_asked_for() -> None:
    report = evaluate(strict=False)
    assert "terminology_adherence_pre_edit" not in report.metrics


def test_claude_judge_attaches_its_loop_for_closing() -> None:
    """`close_batch` no-ops on a callable with no `close`, so without this test
    dropping `judge.close = runner.close` in run_uc2 would leak an event loop per
    batch with the whole suite still green."""
    judge = run_uc2.claude_judge("claude-haiku-4-5", client=FakeClaude())
    try:
        assert callable(getattr(judge, "close", None)), "the judge must carry its loop"
    finally:
        close_batch(judge)


def test_fidelity_against_references_is_not_reported_as_the_b6_gate() -> None:
    """eval.md: never emit a placeholder passing score.

    `--fidelity-source references` scores the golden data, which at P1 is draft
    placeholder text. Publishing that as `fidelity_mean` put
    `quality_gates.fidelity_mean: true` into docs/eval/uc2.json off draft
    translations -- a B6 PASS for UC2 that nothing about UC2 had earned.
    """

    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        return FidelityVerdict(score=5, reason="stub")

    report = evaluate(judge=judge, strict=False)  # default source: references
    assert report.metrics["fidelity_mean_references"] == 5.0, "the number is still reported"
    assert report.metrics["fidelity_items"] == 90
    assert "fidelity_mean" not in report.metrics
    assert "fidelity_mean" not in report.quality_gates, "B6 gate must not fire on golden data"
    assert "fidelity_mean" in report.unmeasured
    reason = next(
        d["reason"]
        for d in report.details
        if d["check"] == "unmeasured" and d["metric"] == "fidelity_mean"
    )
    assert "sut" in str(reason), "the reason must say how to actually measure it"


def test_strict_reference_run_cannot_pass_on_fidelity_alone() -> None:
    """The failure mode in full: a strict run whose only good number came from
    judging draft references must not report a B6 fidelity pass."""

    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        return FidelityVerdict(score=5, reason="stub")

    report = evaluate(judge=judge, strict=True)
    assert not any(gate.startswith("b6:fidelity_mean") for gate in report.gates)
