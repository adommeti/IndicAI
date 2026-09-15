"""UC2 training-localization eval harness (PRD D10).

Measures four things over the `uc2_training` golden set:

  terminology_adherence  exact match against the target-language obligations —
                         approved glossary renderings and LOCKED statements.
  fidelity_mean          back-translation fidelity 1-5 from a judge (D7), scored
                         against the reference translations.
  timing_fit_rate        estimated spoken length within tolerance of the segment
                         duration, from characters-per-second per language.
  quiz_validity          structural checks on the reference quiz items.

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
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from indic_platform.eval.report import Report
from pydantic import BaseModel, Field

GOLDEN = Path(__file__).parents[1] / "golden" / "uc2_training"
TERMS = Path(__file__).parents[3] / "apps" / "training_localizer" / "terminology"
TIMING = Path(__file__).parents[2] / "config" / "timing.yaml"
LANGUAGES = ("hi-IN", "te-IN", "ta-IN")

B6_GATES = {
    "terminology_adherence": 1.0,  # D10: gate 100%
    "fidelity_mean": 4.0,  # D10: gate >= 4.0
    "timing_fit_rate": 0.90,  # D10: target >= 90%
    "quiz_validity": 1.0,
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


def mentions(term: str, text: str) -> bool:
    """Whole-word, case-insensitive presence of an English term."""
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None


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
    tolerance = float(timing["tolerance"])
    fits = total = 0
    for language in LANGUAGES:
        cps = float(timing["languages"][language]["chars_per_second"])
        for segment in segments:
            produced = translate(segment, language)
            estimated = len(produced) / cps
            total += 1
            fits += int(abs(estimated - segment.duration_s) <= tolerance * segment.duration_s)
    return {"timing_fit_rate": fits / total if total else 0.0, "timing_segments": float(total)}


def score_quiz(
    quiz: list[QuizItem], segments: list[Segment]
) -> tuple[dict[str, float], list[dict[str, object]]]:
    known = {(s.module_id, s.seg_id) for s in segments}
    seen_ids: set[int] = set()
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
        if item.item_id in seen_ids:
            problems.append("duplicate item_id")
        seen_ids.add(item.item_id)
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
    references: list[Reference], translate: Translator, judge: Judge | None
) -> tuple[dict[str, float], list[str], list[dict[str, object]]]:
    if judge is None:
        return {}, ["fidelity_mean"], []
    scores: list[int] = []
    details: list[dict[str, object]] = []
    for ref in references:
        segment = Segment(
            module_id=ref.module_id,
            seg_id=ref.seg_id,
            start_ms=0,
            end_ms=1,
            source_text=ref.source_text,
            locked=ref.locked,
        )
        produced = translate(segment, ref.language)
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
    return (
        {"fidelity_mean": sum(scores) / len(scores), "fidelity_items": float(len(scores))},
        [],
        details,
    )


def claude_judge(model: str) -> Judge:
    """Back-translate then score, per D7. Both prompts are versioned under
    platform/eval/prompts/ and sent at temperature 0."""
    from indic_platform.adapters.claude import Claude
    from indic_platform.adapters.runtime import Runtime

    prompts = Path(__file__).parents[1] / "prompts"
    back_prompt = prompts / "uc2_backtranslate.md"
    judge_prompt = prompts / "uc2_qa_judge.md"

    def body(path: Path) -> str:
        text = path.read_text()
        return text.split("---", 2)[2].strip() if text.startswith("---") else text.strip()

    client = Claude(runtime=Runtime())

    def judge(source: str, produced: str, language: str) -> FidelityVerdict:
        import asyncio

        async def run() -> FidelityVerdict:
            english = await client.stream_text(
                body(back_prompt).replace("{language}", language),
                [{"role": "user", "content": produced}],
                model=model,
            )
            back = "".join([chunk async for chunk in english])
            return await client.structured(
                body(judge_prompt),
                f"SOURCE:\n{source}\n\nBACKTRANSLATION:\n{back}",
                FidelityVerdict,
                model=model,
            )

        return asyncio.run(run())

    return judge


def evaluate(
    translate: Translator = baseline, judge: Judge | None = None, strict: bool = True
) -> Report:
    segments = load_segments()
    references = load_references()
    quiz = load_quiz()
    glossary, renderings = load_terminology()
    timing = load_timing()

    metrics: dict[str, float] = {}
    details: list[dict[str, object]] = []
    unmeasured: list[str] = []

    term_metrics, term_details = score_terminology(segments, translate, glossary, renderings)
    metrics.update(term_metrics)
    details += term_details
    metrics.update(score_timing(segments, translate, timing))
    quiz_metrics, quiz_details = score_quiz(quiz, segments)
    metrics.update(quiz_metrics)
    details += quiz_details
    fid_metrics, fid_unmeasured, fid_details = score_fidelity(references, translate, judge)
    metrics.update(fid_metrics)
    unmeasured += fid_unmeasured
    details += fid_details

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
        provisional.append("timing fit (estimated, not measured)")

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
    stage = "P1 harness; " + (
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
        details=details[:50],
    )
    if strict:
        report.gates = {**gates, **{f"b6:{k}": v for k, v in quality_gates.items()}}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--translate", help="module:function implementing (Segment, language) -> str"
    )
    parser.add_argument("--judge", help="module:function implementing the Judge contract")
    parser.add_argument("--judge-model", default="claude-haiku-4-5")
    parser.add_argument("--output", type=Path, default=Path("docs/eval"))
    parser.add_argument(
        "--live", action="store_true", help="Run the Claude judge (costs money); prints an estimate"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strict", action="store_true", help="Enforce the B6 gates (default)")
    mode.add_argument(
        "--baseline", action="store_true", help="Report B6 failures without gating the harness"
    )
    args = parser.parse_args()

    translate = baseline
    if args.translate:
        module, function = args.translate.split(":", 1)
        translate = getattr(importlib.import_module(module), function)

    judge: Judge | None = None
    if args.judge:
        module, function = args.judge.split(":", 1)
        judge = getattr(importlib.import_module(module), function)
    elif args.live:
        references = load_references()
        print(
            f"uc2 judge: {len(references)} segments x 2 Claude calls "
            f"(back-translate + score) on {args.judge_model}"
        )
        judge = claude_judge(args.judge_model)

    report = evaluate(translate=translate, judge=judge, strict=not args.baseline)
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
