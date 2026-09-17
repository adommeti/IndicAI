"""UC2 training-localization eval harness (PRD D10).

Measures four things over the `uc2_training` golden set:

  terminology_adherence  exact match against the target-language obligations —
                         approved glossary renderings and LOCKED statements.
  fidelity_mean          back-translation fidelity 1-5 from a judge (D7), and
                         the B6 gate. Only `--fidelity-source sut` produces it,
                         because B6 asks about the system. At P1 there is no
                         pipeline, so the default `--fidelity-source references`
                         judges the reference translations instead and publishes
                         that under `fidelity_mean_references`, leaving
                         `fidelity_mean` unmeasured with its reason. The two are
                         separate keys on purpose: a reader of docs/eval/uc2.json
                         must not be able to mistake a number about draft golden
                         data for a number about UC2.
  timing_fit_rate        estimated spoken length within tolerance of the segment
                         duration, from characters-per-second per language.
  quiz_validity          structural checks on the reference quiz items.

PRD D7 also ships these two rubrics under `apps/training_localizer/prompts/`.
The copies here are the eval-side originals (.claude/rules/eval.md versions judge
rubrics under platform/eval/prompts/); when uc2/P2 creates the app-side pair,
`test_eval_and_app_judge_prompts_agree` keeps the two texts identical.

The system under test is injected (`--translate module:function`), so the runner
works before `apps/training_localizer` exists. The default is `baseline`, which
returns the English source untouched: it must score 0% adherence, which is what
proves the adherence check discriminates at all.

`keep_english` retention is reported SEPARATELY from adherence on purpose. An
untranslated baseline satisfies every keep_english rule trivially — the terms are
already there in Latin script — so folding it into the headline metric would put
a floor under the baseline and hide a broken check.
"""

import argparse
import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from indic_platform.eval.aio import LoopRunner, close_batch
from indic_platform.eval.report import Report
from indic_platform.text import mentions
from pydantic import BaseModel, Field

GOLDEN = Path(__file__).parents[1] / "golden" / "uc2_training"
TERMS = Path(__file__).parents[3] / "apps" / "training_localizer" / "terminology"
TIMING = Path(__file__).parents[2] / "config" / "timing.yaml"
LANGUAGES = ("hi-IN", "te-IN", "ta-IN")

B6_GATES = {
    "terminology_adherence": 1.0,  # B6 + D10: gate 100%
    "fidelity_mean": 4.0,  # B6 + D10: gate >= 4.0 mean
    "timing_fit_rate": 0.90,  # D10: target >= 90%
    "quiz_validity": 1.0,  # no PRD threshold; structural faults are never acceptable
}

# Named by B6/D10 for UC2, not producible from a golden set alone. Reported as
# unmeasured with the reason rather than omitted or given a passing placeholder.
NOT_MEASURABLE_AT_P1 = {
    "quiz_pass_rate_vs_english_control": "B6 business metric; needs the pilot cohort (uc2/P5)",
    "reviewer_edit_rate": "D10 target <= 20%; needs the reviewer UI and real edits (uc2/P3)",
    "fidelity_human_correlation": (
        "D10; needs human-approved reference translations, which are still draft"
    ),
}


class Segment(BaseModel):
    module_id: str
    seg_id: int
    start_ms: int
    end_ms: int
    source_text: str
    locked: bool = False
    locked_id: str | None = None

    @property
    def duration_s(self) -> float:
        return (self.end_ms - self.start_ms) / 1000.0


class Reference(BaseModel):
    module_id: str
    seg_id: int
    language: str
    source_text: str
    reference_text: str
    status: str
    origin: str
    locked: bool = False


class QuizItem(BaseModel):
    module_id: str
    language: str
    item_id: int
    seg_id: int
    question: str
    options: list[str]
    answer: int
    rationale: str
    status: str = "draft"


class FidelityVerdict(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str = ""
    lost_or_changed: list[str] = Field(default_factory=list)


# A translator maps (segment, language) -> target text. A judge maps
# (source_text, translated_text, language) -> FidelityVerdict.
Translator = Callable[[Segment, str], str]
Judge = Callable[[str, str, str], FidelityVerdict]


def baseline(segment: Segment, language: str) -> str:
    """The trivial system under test: no translation at all."""
    return segment.source_text


def load_segments() -> list[Segment]:
    segments: list[Segment] = []
    for path in sorted((GOLDEN / "samples").glob("*.jsonl")):
        segments += [
            Segment.model_validate_json(line) for line in path.read_text().splitlines() if line
        ]
    if not segments:
        raise ValueError("uc2 golden set has no segments")
    keys = {(s.module_id, s.seg_id) for s in segments}
    if len(keys) != len(segments):
        raise ValueError("Segment ids must be unique within a module")
    return segments


def load_references() -> list[Reference]:
    refs: list[Reference] = []
    for language in LANGUAGES:
        path = GOLDEN / f"reference_translations.{language}.jsonl"
        refs += [
            Reference.model_validate_json(line) for line in path.read_text().splitlines() if line
        ]
    return refs


def load_quiz() -> list[QuizItem]:
    path = GOLDEN / "quiz_items.jsonl"
    return [QuizItem.model_validate_json(line) for line in path.read_text().splitlines() if line]


def load_terminology() -> tuple[dict[str, Any], dict[str, Any]]:
    glossary = yaml.safe_load((TERMS / "glossary.yaml").read_text())
    renderings = yaml.safe_load((TERMS / "approved_renderings.yaml").read_text())
    return glossary, renderings


def load_timing() -> dict[str, Any]:
    return yaml.safe_load(TIMING.read_text())


# Re-exported so existing callers keep working; the rule itself is shared with
# the pipeline that has to satisfy it (see indic_platform.text).
__all__ = ["mentions"]


def terminology_expectations(
    segment: Segment, language: str, glossary: dict[str, Any], renderings: dict[str, Any]
) -> list[tuple[str, str]]:
    """Target-language obligations for this segment, as (kind, expected string).

    `locked` obligations are exact-equality; `rendering` obligations are
    containment. Both require the target language, so an untranslated baseline
    fails every one of them.
    """
    expectations: list[tuple[str, str]] = []
    if segment.locked and segment.locked_id:
        statement = next(
            (s for s in renderings["statements"] if s["id"] == segment.locked_id), None
        )
        if statement is not None:
            expectations.append(("locked", statement[language]))
        return expectations
    for entry in glossary["approved_renderings"]:
        if mentions(entry["term"], segment.source_text):
            expectations.append(("rendering", entry[language]))
    return expectations


def keep_english_expectations(segment: Segment, glossary: dict[str, Any]) -> list[str]:
    return [e["term"] for e in glossary["keep_english"] if mentions(e["term"], segment.source_text)]


def score_terminology(
    segments: list[Segment],
    translate: Translator,
    glossary: dict[str, Any],
    renderings: dict[str, Any],
) -> tuple[dict[str, float], list[dict[str, object]]]:
    met = total = kept = keep_total = 0
    details: list[dict[str, object]] = []
    for language in LANGUAGES:
        for segment in segments:
            produced = translate(segment, language)
            for kind, expected in terminology_expectations(segment, language, glossary, renderings):
                total += 1
                ok = (
                    produced.strip() == expected.strip()
                    if kind == "locked"
                    else expected in produced
                )
                met += int(ok)
                if not ok:
                    details.append(
                        {
                            "check": f"terminology:{kind}",
                            "module_id": segment.module_id,
                            "seg_id": segment.seg_id,
                            "language": language,
                            "expected": expected,
                        }
                    )
            for term in keep_english_expectations(segment, glossary):
                # Case-insensitive, like the detection side: a term that opens a
                # sentence is capitalised in the source and still kept in English.
                keep_total += 1
                kept += int(mentions(term, produced))
    metrics = {
        "terminology_adherence": met / total if total else 0.0,
        "terminology_expectations": float(total),
        "keep_english_retention": kept / keep_total if keep_total else 0.0,
        "keep_english_expectations": float(keep_total),
    }
    return metrics, details


def score_timing(
    segments: list[Segment], translate: Translator, timing: dict[str, Any]
) -> dict[str, float]:
    """Estimated spoken length within tolerance of the authored segment duration.

    The per-language rates are reported alongside the headline so a reader can see
    when a run is degenerate: the golden durations were authored at a constant
    English rate, so an *untranslated* baseline produces the same length ratio for
    every segment in a language and its per-language rate is 0 or 1, never in
    between. `timing_durations_synthetic` marks that, and `evaluate` turns it into
    a provisional note on the stage line. Real translations vary in length and the
    check discriminates normally (`test_timing_fit_discriminates_on_length`).
    """
    tolerance = float(timing["tolerance"])
    metrics: dict[str, float] = {}
    fits = total = 0
    degenerate = True
    for language in LANGUAGES:
        cps = float(timing["languages"][language]["chars_per_second"])
        lang_fits = 0
        for segment in segments:
            produced = translate(segment, language)
            estimated = len(produced) / cps
            lang_fits += int(abs(estimated - segment.duration_s) <= tolerance * segment.duration_s)
        metrics[f"timing_fit_rate_{language}"] = lang_fits / len(segments) if segments else 0.0
        if segments and 0 < lang_fits < len(segments):
            degenerate = False
        fits += lang_fits
        total += len(segments)
    metrics["timing_fit_rate"] = fits / total if total else 0.0
    metrics["timing_segments"] = float(total)
    metrics["timing_durations_synthetic"] = float(degenerate)
    return metrics


def score_quiz(
    quiz: list[QuizItem], segments: list[Segment]
) -> tuple[dict[str, float], list[dict[str, object]]]:
    known = {(s.module_id, s.seg_id) for s in segments}
    seen_ids: set[tuple[str, str, int]] = set()
    valid = 0
    details: list[dict[str, object]] = []
    for item in quiz:
        problems: list[str] = []
        if (item.module_id, item.seg_id) not in known:
            problems.append("seg_id does not exist in the module")
        if len(item.options) != 4:
            problems.append(f"{len(item.options)} options, expected 4")
        if len(set(item.options)) != len(item.options):
            problems.append("duplicate options")
        if not 0 <= item.answer < len(item.options):
            problems.append("answer index out of range")
        if not item.rationale.strip():
            problems.append("empty rationale")
        # D6 keys quiz_items on (module_id, language, item_id), so the same
        # item_id legitimately recurs across modules and languages.
        key = (item.module_id, item.language, item.item_id)
        if key in seen_ids:
            problems.append("duplicate (module_id, language, item_id)")
        seen_ids.add(key)
        # "no item answerable without the content": the stem must not contain the
        # correct option verbatim, which would give the answer away.
        if 0 <= item.answer < len(item.options):
            correct = item.options[item.answer]
            if correct and correct.lower() in item.question.lower():
                problems.append("question contains the correct option verbatim")
        if problems:
            details.append({"check": "quiz", "item_id": item.item_id, "problems": problems})
        else:
            valid += 1
    return {
        "quiz_validity": valid / len(quiz) if quiz else 0.0,
        "quiz_items": float(len(quiz)),
    }, details


def score_fidelity(
    references: list[Reference],
    translate: Translator,
    judge: Judge | None,
    *,
    source: str = "references",
) -> tuple[dict[str, float], list[str], list[dict[str, object]]]:
    """Judge fidelity over the 90 reference rows.

    `source="references"` judges `reference_text` — this is what the P1 prompt
    asks for ("implement the judge now against the reference translations"), and
    it is the only thing worth judging before a pipeline exists. `source="sut"`
    judges whatever `translate` produced, which is what uc2/P2 onwards wants.
    """
    if judge is None:
        return {}, ["fidelity_mean"], []
    scores: list[int] = []
    details: list[dict[str, object]] = []
    for ref in references:
        if source == "references":
            produced = ref.reference_text
        else:
            produced = translate(
                Segment(
                    module_id=ref.module_id,
                    seg_id=ref.seg_id,
                    start_ms=0,
                    end_ms=1,
                    source_text=ref.source_text,
                    locked=ref.locked,
                ),
                ref.language,
            )
        verdict = judge(ref.source_text, produced, ref.language)
        scores.append(verdict.score)
        if verdict.score < 4:
            details.append(
                {
                    "check": "fidelity",
                    "module_id": ref.module_id,
                    "seg_id": ref.seg_id,
                    "language": ref.language,
                    "score": verdict.score,
                    "reason": verdict.reason,
                    "lost_or_changed": verdict.lost_or_changed,
                }
            )
    # `fidelity_mean` is the B6 gate and B6 asks about the SYSTEM. Judging
    # `source="references"` scores the golden data instead -- at P1 those are
    # draft placeholders -- so that number gets its own key and the B6 metric is
    # reported unmeasured. Publishing it as `fidelity_mean` put
    # `quality_gates.fidelity_mean: true` into docs/eval/uc2.json off draft text,
    # which is the placeholder passing score .claude/rules/eval.md forbids.
    key = "fidelity_mean" if source == "sut" else "fidelity_mean_references"
    return (
        {
            key: sum(scores) / len(scores),
            "fidelity_items": float(len(scores)),
        },
        [] if source == "sut" else ["fidelity_mean"],
        details,
    )


class Backtranslation(BaseModel):
    """D7 asks for "only the English"; a schema gets that through `structured`,
    which is the adapter path that sets temperature 0 (claude.py). `stream_text`
    does not take a temperature, and adding one would change a Protocol shared by
    every adapter — not this prompt's business."""

    english: str


def prompt_body(path: Path) -> str:
    """The prompt text without its YAML front-matter."""
    text = path.read_text()
    return text.split("---", 2)[2].strip() if text.startswith("---") else text.strip()


def claude_judge(model: str, client: Any | None = None) -> Judge:
    """Back-translate, then score source vs. back-translation, per PRD D7.

    Both rubrics are versioned under `platform/eval/prompts/` and both legs go
    through `Claude.structured`, so both are sent at temperature 0 with the system
    prompt cached. `client` is injectable so the path is testable without a key.
    """
    from indic_platform.adapters.claude import Claude

    prompts = Path(__file__).parents[1] / "prompts"
    back_prompt = prompt_body(prompts / "uc2_backtranslate.md")
    judge_prompt = prompt_body(prompts / "uc2_qa_judge.md")
    sut = client if client is not None else Claude()
    # One loop for the whole batch (2 calls per reference). `asyncio.run` per
    # reference closed the loop that `sut`'s connection pool was bound to, so
    # reference 2 failed with an APIConnectionError wrapping "Event loop is
    # closed". See platform/eval/aio.py.
    runner = LoopRunner()

    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        async def run() -> FidelityVerdict:
            back = await sut.structured(
                system=back_prompt.replace("{language}", language),
                user=produced,
                schema=Backtranslation,
                model=model,
            )
            return await sut.structured(
                system=judge_prompt,
                user=f"SOURCE:\n{source}\n\nBACKTRANSLATION:\n{back.english}",
                schema=FidelityVerdict,
                model=model,
            )

        return runner.run(run())

    judge.close = runner.close  # type: ignore[attr-defined]
    return judge


def memoise(translate: Translator) -> Translator:
    """Call the system under test once per (segment, language).

    Terminology, timing and fidelity must describe the *same* text. Against a
    non-deterministic translator, calling through three times would score three
    different outputs and cost three times as much.
    """
    cache: dict[tuple[str, int, str], str] = {}

    def wrapped(segment: Segment, language: str) -> str:
        key = (segment.module_id, segment.seg_id, language)
        if key not in cache:
            cache[key] = translate(segment, language)
        return cache[key]

    return wrapped


def evaluate(
    translate: Translator = baseline,
    judge: Judge | None = None,
    strict: bool = True,
    fidelity_source: str = "references",
    sut: str = "baseline (untranslated source)",
    pre_edit: Translator | None = None,
) -> Report:
    segments = load_segments()
    references = load_references()
    quiz = load_quiz()
    glossary, renderings = load_terminology()
    timing = load_timing()
    translate = memoise(translate)

    metrics: dict[str, float] = {}
    # B6/D10 name UC2 measures this prompt cannot produce. Declaring them keeps the
    # rule in .claude/rules/eval.md honest: every B6 metric is either measured with
    # its item count or reported unmeasured with a reason. Silence would let a
    # reader mistake `quiz_validity` (structural) for the B6 quiz *pass rate*.
    # These lead `details` so the reasons survive its truncation.
    unmeasured: list[str] = list(NOT_MEASURABLE_AT_P1)
    details: list[dict[str, object]] = [
        {"check": "unmeasured", "metric": metric, "reason": reason}
        for metric, reason in NOT_MEASURABLE_AT_P1.items()
    ]

    term_metrics, term_details = score_terminology(segments, translate, glossary, renderings)
    metrics.update(term_metrics)
    details += term_details
    if pre_edit is not None:
        # `terminology_adherence` alone cannot fail against a pipeline whose
        # post-edit stage enforces exactly the predicate scored here: a
        # translator returning "zzz" reaches 1.0. Scoring the text BEFORE the
        # enforcer says what the translation vendor actually did, which is the
        # number that moves. Reported, never gated -- the gate is on the
        # finished text, which is what ships.
        raw_metrics, raw_details = score_terminology(
            segments, memoise(pre_edit), glossary, renderings
        )
        metrics.update(
            {
                "terminology_adherence_pre_edit": raw_metrics["terminology_adherence"],
                "keep_english_retention_pre_edit": raw_metrics["keep_english_retention"],
            }
        )
        details += [{**d, "check": f"{d['check']}:pre_edit"} for d in raw_details]
    metrics.update(score_timing(segments, translate, timing))
    quiz_metrics, quiz_details = score_quiz(quiz, segments)
    metrics.update(quiz_metrics)
    details += quiz_details
    fid_metrics, fid_unmeasured, fid_details = score_fidelity(
        references, translate, judge, source=fidelity_source
    )
    metrics.update(fid_metrics)
    unmeasured += fid_unmeasured
    details += fid_details
    if fid_unmeasured:
        details.insert(
            0,
            {
                "check": "unmeasured",
                "metric": "fidelity_mean",
                "reason": (
                    "no judge injected; pass --live (needs an Anthropic key)"
                    if judge is None
                    else (
                        "the judge scored the reference translations, not the system under "
                        "test, so this run measures the golden data and not UC2. The number "
                        "is reported as fidelity_mean_references; the B6 gate needs "
                        "--fidelity-source sut"
                    )
                ),
            },
        )

    # Provenance: both the references and the timing table are drafts, so the
    # numbers that depend on them are provisional even when they are measured.
    draft_refs = sum(1 for r in references if r.status != "approved")
    draft_terms = sum(
        1 for e in glossary["approved_renderings"] if e.get("status") != "approved"
    ) + sum(1 for s in renderings["statements"] if s.get("status") != "approved")
    provisional = []
    if draft_terms:
        provisional.append(f"terminology ({draft_terms} draft entries)")
    if draft_refs:
        provisional.append(f"fidelity ({draft_refs} draft references)")
    if not timing.get("measured", False):
        provisional.append("timing fit (rate estimated, not measured)")
    if metrics.get("timing_durations_synthetic"):
        # The golden durations were authored at the en-IN rate in this same file,
        # so for an untranslated baseline the length ratio is a per-language
        # constant (en_cps / lang_cps) and the rate says nothing about the text.
        # It discriminates on real translations; it cannot on the baseline.
        provisional.append(
            "timing fit (golden durations authored at the en-IN rate; the baseline "
            "ratio is a per-language constant, not a measurement of the text)"
        )

    quality_gates = {
        name: metrics[name] >= threshold for name, threshold in B6_GATES.items() if name in metrics
    }
    # Harness gates: did the measurement machinery run, not did the system pass.
    gates = {
        "segments_loaded": len(segments) == 60,
        "references_loaded": len(references) == 90,
        "quiz_loaded": len(quiz) == 30,
        "terminology_check_ran": metrics["terminology_expectations"] > 0,
    }
    stage = f"harness P1; SUT {sut}; " + (
        "provisional: " + ", ".join(provisional) if provisional else "measured"
    )
    report = Report(
        app="uc2",
        stage=stage,
        items=len(segments),
        metrics=metrics,
        gates=gates,
        unmeasured=unmeasured,
        quality_gates=quality_gates,
        details=details,
    )
    if strict:
        report.gates = {**gates, **{f"b6:{k}": v for k, v in quality_gates.items()}}
    return report


PRICING = Path(__file__).parents[2] / "config" / "pricing.yaml"
# Both D7 legs send a short rubric plus one segment and return one short field.
# Rounded up from the golden set's own lengths (~4 chars/token, 1.5x for Indic
# script) so the printed number errs high rather than low.
TOKENS_PER_CALL = (600, 200)


def estimate_judge_cost(references: list[Reference], model: str) -> str:
    """`.claude/rules/eval.md`: print an estimated cost before a live batch."""
    pricing = yaml.safe_load(PRICING.read_text())
    rates = pricing["models"].get(model)
    calls = len(references) * 2  # back-translate, then score
    if not rates:
        return f"uc2 judge: {calls} Claude calls on {model}; no pricing entry, cost unknown"
    tokens_in, tokens_out = TOKENS_PER_CALL
    usd = calls * (tokens_in * rates["input_tokens"] + tokens_out * rates["output_tokens"])
    inr = usd * float(pricing["fx_inr_per_usd"])
    return (
        f"uc2 judge: {len(references)} reference segments x 2 Claude calls "
        f"(back-translate + score) on {model} = {calls} calls, "
        f"estimated ${usd:.2f} / Rs {inr:.2f}"
    )


def estimate_pipeline_cost(segments: list[Segment], languages: tuple[str, ...]) -> str:
    """What a full `--translate` pass over the golden set costs in vendor spend.

    uc2/P2's pipeline calls Mayura once per (segment, language) and Claude for
    adapt and post_edit. Printed before the run so a batch is never a surprise.
    """
    pricing = yaml.safe_load(PRICING.read_text())
    chars = sum(len(s.source_text) for s in segments) * len(languages)
    mayura = chars / 1000 * float(pricing["models"]["mayura:v1"]["characters"]) * 1000
    sonnet = pricing["models"].get("claude-sonnet-5")
    calls = len(segments) * len(languages)  # post_edit, one per segment-language
    usd = 0.0
    if sonnet:
        # adapt batches a module at a time; post_edit is per segment. Rounded up.
        usd = calls * (900 * sonnet["input_tokens"] + 300 * sonnet["output_tokens"])
    inr = mayura + usd * float(pricing["fx_inr_per_usd"])
    return (
        f"uc2 pipeline: {len(segments)} segments x {len(languages)} languages = {calls} "
        f"Mayura calls ({chars} chars, Rs {mayura:.2f}) plus Claude adapt/post_edit "
        f"(~${usd:.2f}); total about Rs {inr:.2f}"
    )


def words_per_second_table() -> str:
    """Re-derive platform/config/timing.yaml's `words_per_second` from the golden set.

    timing.yaml points here for reproduction: the English word budget adapt works
    to is the measured target-script expansion divided by the measured speech
    rate, so both halves are checkable rather than asserted.
    """
    import statistics

    timing = load_timing()
    lines = ["language  chars/en-word  chars/s  ->  en words/s"]
    references = load_references()
    for language in LANGUAGES:
        cps = float(timing["languages"][language]["chars_per_second"])
        ratios = [
            len(r.reference_text) / len(r.source_text.split())
            for r in references
            if r.language == language and r.source_text.split()
        ]
        cpw = statistics.median(ratios)
        lines.append(
            f"{language}   {cpw:12.2f}  {cps:7.1f}  ->  {cps / cpw:10.2f} "
            f"(config: {timing['languages'][language]['words_per_second']})"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--translate", help="module:function implementing (Segment, language) -> str"
    )
    parser.add_argument("--judge", help="module:function implementing the Judge contract")
    parser.add_argument(
        "--pre-edit",
        help=(
            "module:function giving the same pipeline's text BEFORE post-edit; "
            "scored alongside as terminology_adherence_pre_edit so the headline "
            "number can be compared against what the translator alone produced"
        ),
    )
    parser.add_argument("--judge-model", default="claude-haiku-4-5")
    parser.add_argument(
        "--fidelity-source",
        choices=("references", "sut"),
        default="references",
        help="What the judge scores: the reference translations (P1) or the translator (P2+)",
    )
    parser.add_argument("--output", type=Path, default=Path("docs/eval"))
    parser.add_argument(
        "--words-per-second",
        action="store_true",
        help="Print the measured words-per-second table behind timing.yaml and exit",
    )
    parser.add_argument(
        "--live", action="store_true", help="Run the Claude judge (costs money); prints an estimate"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strict", action="store_true", help="Enforce the B6 gates (default)")
    mode.add_argument(
        "--baseline", action="store_true", help="Report B6 failures without gating the harness"
    )
    args = parser.parse_args()

    if args.words_per_second:
        print(words_per_second_table())
        return

    translate = baseline
    if args.translate:
        module, function = args.translate.split(":", 1)
        translate = getattr(importlib.import_module(module), function)

    pre_edit: Translator | None = None
    if args.pre_edit:
        module, function = args.pre_edit.split(":", 1)
        pre_edit = getattr(importlib.import_module(module), function)

    judge: Judge | None = None
    if args.judge:
        module, function = args.judge.split(":", 1)
        judge = getattr(importlib.import_module(module), function)
    elif args.live:
        references = load_references()
        print(estimate_judge_cost(references, args.judge_model))
        judge = claude_judge(args.judge_model)

    if args.live and args.translate:
        print(estimate_pipeline_cost(load_segments(), LANGUAGES))

    try:
        report = evaluate(
            translate=translate,
            judge=judge,
            strict=not args.baseline,
            fidelity_source=args.fidelity_source,
            sut=args.translate or "baseline (untranslated source)",
            pre_edit=pre_edit,
        )
    finally:
        # The judge owns an event loop once it is the live one; a run that raises
        # part-way through the batch must still give it back.
        close_batch(judge)
    report.write(args.output)
    print(
        json.dumps(
            {
                "app": report.app,
                "stage": report.stage,
                "items": report.items,
                "metrics": report.metrics,
                "quality_gates": report.quality_gates,
                "unmeasured": report.unmeasured,
                "passed": report.passed,
            },
            indent=2,
        )
    )
    raise SystemExit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
