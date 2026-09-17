"""The uc3 golden set and eval harness (uc3/P1).

The load-bearing tests are `test_baseline_reports_zero_recall_and_no_precision`
(the acceptance criterion, and the reason a "flag nothing" detector must not
look perfectly precise) and `test_adversarial_success_is_measured_by_difference`
(B6 makes any injection success a build blocker, so it cannot be self-reported).
"""

import json
from pathlib import Path

import pytest
from indic_platform.eval.aio import close_batch
from indic_platform.eval.runners import run_uc3
from indic_platform.eval.runners.run_uc3 import (
    CATEGORIES,
    Flag,
    Transcript,
    attribution_accuracy,
    baseline,
    evaluate,
    load_audio_manifest,
    load_transcripts,
    matches,
    score_detection,
    score_evidence,
    strip_attack,
)
from pydantic import ValidationError

GOLDEN = Path(__file__).parents[1] / "eval" / "golden" / "uc3_surveillance"


# --- the golden set -----------------------------------------------------------


def test_the_set_has_the_shape_the_prompt_specifies() -> None:
    records = load_transcripts()
    assert len(records) == 200
    counts: dict[str, int] = {}
    for record in records:
        counts[record.cls] = counts.get(record.cls, 0) + 1
    assert counts == {
        "true_positive": 60,
        "hard_negative": 40,
        "clean": 80,
        "adversarial": 20,
    }
    assert len({r.id for r in records}) == 200


def test_ten_true_positives_per_category() -> None:
    records = [r for r in load_transcripts() if r.cls == "true_positive"]
    per_category: dict[str, int] = {}
    for record in records:
        for label in record.labels:
            per_category[label.category] = per_category.get(label.category, 0) + 1
    assert per_category == dict.fromkeys(CATEGORIES, 10), per_category


def test_every_evidence_span_is_an_exact_substring_attributed_to_its_speaker() -> None:
    """The whole evidence check downstream rests on this."""
    for record in load_transcripts():
        for label in record.labels:
            assert label.evidence_span in record.text, record.id
            owner = [s for s in record.segments if label.evidence_span in s.text]
            assert owner, record.id
            assert any(s.speaker == label.speaker for s in owner), record.id


def test_roman_script_hinglish_meets_the_minimum() -> None:
    records = load_transcripts()
    assert sum(1 for r in records if r.language_mix == "hi-Latn") >= 30


def test_all_four_scripts_are_represented() -> None:
    languages = {r.language_mix for r in load_transcripts()}
    assert {"hi-IN", "te-IN", "ta-IN", "en-IN", "hi-Latn"} <= languages


def test_clean_transcripts_carry_no_labels() -> None:
    for record in load_transcripts():
        if record.cls in ("clean", "hard_negative"):
            assert record.labels == [], record.id


def test_most_adversarial_items_carry_a_real_violation_to_suppress() -> None:
    """Without one, an attack can only fail loudly and suppression is invisible."""
    adversarial = [r for r in load_transcripts() if r.cls == "adversarial"]
    assert len(adversarial) == 20
    with_violation = [r for r in adversarial if r.labels]
    assert len(with_violation) >= 12, len(with_violation)
    assert {r.adversary_kind for r in adversarial} >= {
        "instruction",
        "framing",
        "evasion",
        "authority",
    }


def test_segments_are_diarized_and_ordered() -> None:
    for record in load_transcripts():
        assert len(record.segments) >= 4, record.id
        assert len({s.speaker for s in record.segments}) == 2, record.id
        for earlier, later in zip(record.segments, record.segments[1:], strict=False):
            assert earlier.end_ms <= later.start_ms, record.id
            assert earlier.end_ms > earlier.start_ms


# --- the baseline -------------------------------------------------------------


def test_baseline_reports_zero_recall_and_no_precision() -> None:
    """uc3/P1 acceptance: recall 0, precision n/a, and adversarial NOT scored.

    "Adversarial 0 by construction" was the original wording, and construction is the
    problem: B6 makes any adversarial success a build blocker, so a 0.0 earned by a
    detector that raises no flags at all hands the one build-blocking gate a pass for
    work it never did. A detector that flags nothing cannot be manipulated into
    flagging less, and cannot echo an attacker's words it never quoted -- both routes
    to "success" are closed by inaction, not by robustness. So the metric is reported
    unmeasured, exactly as `precision` over zero flags already was, and the number is
    published under `adversarial_success_baseline` where nothing can mistake it for a
    measurement.
    """
    report = evaluate(strict=False)
    assert report.metrics["recall"] == 0.0
    assert "precision" not in report.metrics, "precision over zero flags is undefined, not 1.0"
    assert "adversarial_success" not in report.metrics, (
        "a detector that raised no flags has not been shown to resist anything"
    )
    assert "adversarial_success" in report.unmeasured
    assert report.metrics["adversarial_success_baseline"] == 0.0
    assert report.metrics["flags_raised"] == 0.0
    assert report.metrics["labels"] > 0
    assert report.quality_gates["recall"] is False
    assert report.passed, "the harness itself still ran"


def test_the_baseline_fails_the_b6_recall_gate_in_strict_mode() -> None:
    assert evaluate(strict=False).passed is True
    assert evaluate(strict=True).passed is False


def test_diarization_and_cost_are_unmeasured_not_zero() -> None:
    report = evaluate(strict=False)
    assert "diarization_accuracy" in report.unmeasured
    assert "cost_per_call_inr" in report.unmeasured
    assert "diarization_accuracy" not in report.metrics
    reasons = {d["metric"]: d["reason"] for d in report.details if d["check"] == "unmeasured"}
    assert reasons["diarization_accuracy"]
    assert reasons["cost_per_call_inr"]


# --- a detector that works ----------------------------------------------------


def perfect(transcript: Transcript) -> list[Flag]:
    """Reads the labels. Not a detector -- a check that the scorer can see a
    right answer when it is given one."""
    return [
        Flag(
            category=label.category,
            evidence_span=label.evidence_span,
            speaker=label.speaker,
            severity=label.severity,
        )
        for label in transcript.labels
    ]


def test_a_perfect_detector_scores_perfectly() -> None:
    report = evaluate(detect=perfect, strict=False)
    assert report.metrics["recall"] == 1.0
    assert report.metrics["precision"] == 1.0
    assert report.metrics["evidence_failure_rate"] == 0.0
    assert report.metrics["adversarial_success"] == 0.0
    for category in CATEGORIES:
        assert report.metrics[f"recall_{category}"] == 1.0


def test_a_detector_that_flags_everything_has_terrible_precision() -> None:
    def trigger_happy(transcript: Transcript) -> list[Flag]:
        return [
            Flag(category="conduct", evidence_span=transcript.segments[0].text) for _ in range(1)
        ]

    report = evaluate(detect=trigger_happy, strict=False)
    assert report.metrics["precision"] < 0.1
    assert report.quality_gates["precision"] is False


def test_recall_is_reported_per_category() -> None:
    def only_conduct(transcript: Transcript) -> list[Flag]:
        return [
            Flag(category=label.category, evidence_span=label.evidence_span)
            for label in transcript.labels
            if label.category == "conduct"
        ]

    report = evaluate(detect=only_conduct, strict=False)
    assert report.metrics["recall_conduct"] == 1.0
    assert report.metrics["recall_mnpi_insider"] == 0.0
    assert 0 < report.metrics["recall"] < 1


# --- evidence -----------------------------------------------------------------


def test_a_flag_that_does_not_quote_the_transcript_is_a_failure() -> None:
    def fabricator(transcript: Transcript) -> list[Flag]:
        return [Flag(category="conduct", evidence_span="something nobody said")]

    report = evaluate(detect=fabricator, strict=False)
    assert report.metrics["evidence_failure_rate"] == 1.0
    assert report.quality_gates["evidence_failure_rate"] is False
    assert any(d["check"] == "evidence:not_exact" for d in report.details)


def test_paraphrased_evidence_is_caught_even_when_the_category_is_right() -> None:
    records = [r for r in load_transcripts() if r.labels][:5]
    flags = {
        r.id: [
            Flag(
                category=r.labels[0].category,
                evidence_span=r.labels[0].evidence_span.replace(" ", "  "),
            )
        ]
        for r in records
    }
    metrics, unmeasured, details = score_evidence(records, flags)
    assert metrics["evidence_failure_rate"] == 1.0
    assert unmeasured == []
    assert len(details) == 5


def test_matching_tolerates_a_tighter_or_looser_quote() -> None:
    record = next(r for r in load_transcripts() if r.labels)
    label = record.labels[0]
    tighter = Flag(category=label.category, evidence_span=label.evidence_span[:25])
    looser = Flag(category=label.category, evidence_span=label.evidence_span)
    wrong_category = Flag(category="conduct", evidence_span=label.evidence_span)
    assert matches(tighter, label, record) and matches(looser, label, record)
    assert not matches(wrong_category, label, record) or label.category == "conduct"


# --- adversarial --------------------------------------------------------------


def test_stripping_an_attack_keeps_the_violation() -> None:
    """Otherwise the control run has nothing to find and suppression cannot be seen."""
    for record in load_transcripts():
        if record.cls != "adversarial" or not record.labels:
            continue
        control = strip_attack(record)
        for label in record.labels:
            assert label.evidence_span in control.text, record.id
        assert len(control.segments) < len(record.segments), record.id


def test_adversarial_success_is_measured_by_difference() -> None:
    """A detector that obeys "mark this clean" is caught by the control run, not
    by being asked."""

    def obedient(transcript: Transcript) -> list[Flag]:
        if any(run_uc3._is_attack(s.text) for s in transcript.segments):
            return []  # does what the attacker said
        return perfect(transcript)

    report = evaluate(detect=obedient, strict=False)
    assert report.metrics["adversarial_success"] > 0
    assert report.quality_gates["adversarial_success"] is False
    hits = [d for d in report.details if d["check"] == "adversarial:success"]
    assert hits and all(h["suppressed"] for h in hits)


def test_a_detector_that_quotes_the_attacker_counts_as_following_instructions() -> None:
    def echoes(transcript: Transcript) -> list[Flag]:
        attack = next((s for s in transcript.segments if run_uc3._is_attack(s.text)), None)
        return (
            [Flag(category="conduct", evidence_span=attack.text)] if attack else perfect(transcript)
        )

    report = evaluate(detect=echoes, strict=False)
    assert report.metrics["adversarial_success"] > 0
    assert any(d["echoed"] for d in report.details if d["check"] == "adversarial:success")


def test_a_robust_detector_scores_zero_adversarial_success() -> None:
    report = evaluate(detect=perfect, strict=False)
    assert report.metrics["adversarial_success"] == 0.0
    assert report.quality_gates["adversarial_success"] is True


# --- diarization --------------------------------------------------------------


def test_the_audio_subset_is_present_with_ground_truth() -> None:
    manifest = load_audio_manifest()
    assert len(manifest["items"]) == 20
    for item in manifest["items"]:
        assert (GOLDEN / item["wav"]).exists()
        truth = json.loads((GOLDEN / item["diarization"]).read_text())
        assert truth["gap_ms"] == 300
        assert len({t["speaker"] for t in truth["turns"]}) == 2
        for earlier, later in zip(truth["turns"], truth["turns"][1:], strict=False):
            assert later["start_ms"] - earlier["end_ms"] == 300, "exact gap, not approximate"


def test_the_audio_covers_every_script_bulbul_speaks() -> None:
    items = load_audio_manifest()["items"]
    assert {i["language_mix"] for i in items} == {"hi-IN", "te-IN", "ta-IN", "en-IN"}
    assert len({i["class"] for i in items}) == 4


def test_attribution_aligns_labels_before_scoring() -> None:
    """`SPEAKER_0` vs `spk_1` is arbitrary; a perfect diarization that numbered
    the speakers the other way round must score 1.0, not 0."""
    truth = [
        {"speaker": "SPEAKER_00", "start_ms": 0, "end_ms": 1000},
        {"speaker": "SPEAKER_01", "start_ms": 1300, "end_ms": 2300},
        {"speaker": "SPEAKER_00", "start_ms": 2600, "end_ms": 3600},
    ]
    swapped = [
        {"speaker": "spk_1", "start_ms": 0, "end_ms": 1000},
        {"speaker": "spk_0", "start_ms": 1300, "end_ms": 2300},
        {"speaker": "spk_1", "start_ms": 2600, "end_ms": 3600},
    ]
    assert attribution_accuracy(truth, swapped) == (3, 3)


def test_attribution_counts_a_real_mistake() -> None:
    truth = [
        {"speaker": "SPEAKER_00", "start_ms": 0, "end_ms": 1000},
        {"speaker": "SPEAKER_01", "start_ms": 1300, "end_ms": 2300},
    ]
    both_same = [
        {"speaker": "spk_0", "start_ms": 0, "end_ms": 1000},
        {"speaker": "spk_0", "start_ms": 1300, "end_ms": 2300},
    ]
    correct, total = attribution_accuracy(truth, both_same)
    assert total == 2 and correct == 1, "one speaker collapsed onto the other"


def test_attribution_reports_nothing_rather_than_guessing_on_empty_output() -> None:
    assert attribution_accuracy([{"speaker": "a", "start_ms": 0, "end_ms": 1}], []) == (0, 1)


# --- cost ---------------------------------------------------------------------


def test_the_cost_estimate_is_printed_from_pricing_not_invented() -> None:
    line = run_uc3.estimate_cost(load_transcripts())
    assert "200 transcripts" in line and "Rs " in line and "$" in line
    assert "cost unknown" in run_uc3.estimate_cost(load_transcripts(), "no-such-model")


def test_spend_is_summed_from_adapter_records() -> None:
    class Sink:
        records = [
            {"model": "claude-haiku-4-5", "cost_inr": 1.5, "cost_usd": 0.016},
            {"model": "claude-haiku-4-5", "cost_inr": 0.5, "cost_usd": 0.005},
        ]

    spend = run_uc3.spend_from_sink(Sink())
    assert spend.calls == 2
    assert spend.inr == pytest.approx(2.0)
    metrics = spend.metrics(200)
    # Per *vendor call*, which is what the adapters recorded -- 2 calls, Rs 2.
    # Dividing by the 200 transcripts would understate a call by 100x, and
    # `evaluate` makes more calls than there are transcripts.
    assert metrics["vendor_calls"] == 2.0
    assert metrics["cost_per_call_inr"] == pytest.approx(1.0)
    assert metrics["cost_per_transcript_inr"] == pytest.approx(0.01)


def test_the_baseline_makes_no_vendor_calls() -> None:
    records = load_transcripts()
    assert all(baseline(r) == [] for r in records)
    metrics, unmeasured, _ = score_detection(records, {r.id: [] for r in records})
    assert metrics["flags_raised"] == 0.0
    assert "precision" in unmeasured


# --- the defects the pre-ship review proved ------------------------------------
#
# Each of these failed before the fix. They are here so the metric cannot quietly
# go back to being unfalsifiable.


def test_zero_evidence_is_unmeasured_not_a_passing_zero() -> None:
    """The baseline checked no evidence, so it must not pass the evidence gate.

    `evidence_failure_rate: 0.0` over zero checks is the same trap `precision`
    already avoided: a detector that flags nothing scoring perfectly on work it
    never did.
    """
    records = load_transcripts()
    metrics, unmeasured, _ = score_evidence(records, {r.id: [] for r in records})
    assert metrics["evidence_checked"] == 0.0
    assert "evidence_failure_rate" not in metrics
    assert "evidence_failure_rate" in unmeasured

    report = run_uc3.evaluate(strict=False)
    assert "evidence_failure_rate" not in report.quality_gates
    assert "evidence_failure_rate" in report.unmeasured
    assert "precision" in report.unmeasured


def test_strict_mode_fails_on_a_metric_that_was_never_measured() -> None:
    """An oracle detector with no diarization run must not report a green strict pass.

    Absence used to skip the gate, so two of six B6 metrics could go untaken and
    the run still came back `passed: True`.
    """

    def oracle(transcript: run_uc3.Transcript) -> list[Flag]:
        return [
            Flag(category=label.category, evidence_span=label.evidence_span, speaker=label.speaker)
            for label in transcript.labels
        ]

    report = run_uc3.evaluate(detect=oracle, strict=True)
    assert report.metrics["recall"] == 1.0
    assert "diarization_accuracy" in report.unmeasured
    assert report.gates["b6:diarization_accuracy:measured"] is False
    assert report.passed is False


def test_evidence_quoting_the_whole_call_is_not_evidence() -> None:
    """A flag per category quoting the entire transcript used to score recall 1.0.

    Bidirectional substring matching made every label a substring of the quote,
    and the joined transcript contained it, so evidence verification passed too.
    """
    records = [r for r in load_transcripts() if r.labels][:5]
    flags = {
        r.id: [Flag(category=label.category, evidence_span=r.text) for label in r.labels]
        for r in records
    }
    detection, _, _ = score_detection(records, flags)
    assert detection["recall"] == 0.0
    evidence, _, _ = score_evidence(records, flags)
    assert evidence["evidence_failure_rate"] == 1.0


def test_a_flag_blaming_the_wrong_speaker_is_not_a_true_positive() -> None:
    """Misattributing a violation is a different error from finding it."""
    record = next(r for r in load_transcripts() if r.labels)
    label = record.labels[0]
    other = next(s.speaker for s in record.segments if s.speaker != label.speaker)
    right = Flag(category=label.category, evidence_span=label.evidence_span, speaker=label.speaker)
    wrong = Flag(category=label.category, evidence_span=label.evidence_span, speaker=other)
    assert matches(right, label, record)
    assert not matches(wrong, label, record)


def test_a_transcript_with_an_unknown_key_is_rejected() -> None:
    """`labelz` must not validate into an apparently clean transcript."""
    record = next(r for r in load_transcripts() if r.labels)
    payload = json.loads(record.model_dump_json(by_alias=True))
    payload["labelz"] = payload.pop("labels")
    with pytest.raises(ValidationError):
        run_uc3.Transcript.model_validate(payload)


def test_the_diarization_estimate_is_priced_from_pricing_yaml() -> None:
    """`--diarize` must say what it will spend, from the pricing file."""
    line = run_uc3.estimate_diarization_cost(run_uc3.load_audio_manifest())
    assert run_uc3.DIARIZE_MODEL in line and "Rs " in line
    # The rate is real: Saaras diarized batch at Rs 45/h over the manifest's audio.
    seconds = sum(i["duration_ms"] for i in run_uc3.load_audio_manifest()["items"]) / 1000
    expected = seconds * 0.0125
    assert f"Rs {expected:.2f}" in line
    # No items is Rs 0.00, not "unknown": the rate is known, the audio is empty.
    assert "Rs 0.00" in run_uc3.estimate_diarization_cost({"items": []})


def test_an_adversarial_item_with_no_usable_control_is_not_scored_as_a_win() -> None:
    """If stripping removes nothing, attacked and control are the same run.

    Suppression cannot show up as a difference, so the item must be reported
    rather than counted as a zero.
    """
    plain = [r for r in load_transcripts() if r.cls == "clean"][:1]
    forced = [
        run_uc3.Transcript(
            id=r.id,
            language_mix=r.language_mix,
            **{"class": "adversarial"},
            segments=r.segments,
            labels=[],
        )
        for r in plain
    ]
    metrics, unmeasured, details = run_uc3.score_adversarial(
        forced, baseline, {r.id: [] for r in forced}
    )
    assert metrics["adversarial_items"] == 1.0
    assert metrics["adversarial_comparable"] == 0.0
    assert "adversarial_success" in unmeasured
    assert any(d["check"] == "adversarial:no_control" for d in details)


def test_every_real_adversarial_item_still_has_a_usable_control() -> None:
    """The 20 shipped adversarial items must all be comparable.

    Comparability is a property of the golden set: each item carries an attack turn
    that `strip_attack` can remove, leaving a control that still holds the labelled
    evidence. Lose that and suppression stops being observable, so this guards the
    fixtures rather than any detector.

    It is deliberately checked with the flags-nothing baseline, which is also why the
    metric comes back unmeasured here: comparable and measured are different claims.
    Twenty usable controls say the set can detect manipulation; a detector that raises
    no flags says nothing about whether it resisted any. Asserting `unmeasured == []`
    on this input -- as this test used to -- required the harness to publish a score
    for a detector that never ran a check, which is the exact false green the rest of
    this file exists to prevent.
    """
    records = load_transcripts()
    metrics, unmeasured, _ = run_uc3.score_adversarial(
        records, baseline, {r.id: [] for r in records}
    )
    assert metrics["adversarial_items"] == 20.0
    assert metrics["adversarial_comparable"] == 20.0
    assert unmeasured == ["adversarial_success"]


def test_saaras_diarizer_attaches_its_loop_for_closing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The uc3 twin of the uc2 wiring test.

    `saaras_diarizer` reuses one `SarvamSTT` across every audio item, so it must
    drive them on one loop and hand that loop back. `close_batch` no-ops on a
    callable without a `close`, so dropping the attachment would be invisible.
    This path has never run live (the Saaras upload host is off the network
    allowlist), which is exactly why it needs a test that does not need the
    network.
    """
    from indic_platform.adapters import sarvam_stt

    monkeypatch.setattr(sarvam_stt, "SarvamSTT", lambda *a, **k: object())
    transcribe = run_uc3.saaras_diarizer()
    try:
        assert callable(getattr(transcribe, "close", None)), "must carry its loop"
    finally:
        close_batch(transcribe)
