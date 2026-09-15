"""Cohort assignment and the comprehension report (PRD D10's business metric).

The question the pilot exists to answer is whether people understand a module
better in their own language than in English. That comparison is only worth
anything if three things hold, and each is enforced here rather than assumed:

1. **Cohort assignment is stable.** An employee who drifts between arms appears
   in both and biases the result. `assign_cohort` hashes (employee, pilot) — the
   same person always lands in the same arm for the same pilot, with no table to
   keep in sync.
2. **The arms are named honestly.** `native` took it in their own language;
   `control` took the English original. Nothing else is counted.
3. **A cell with too few attempts reports no rate.** A "100% pass rate" over two
   attempts is noise wearing a number's clothes, so small cells come back with
   `pass_rate: None` and their count, and the Markdown says so.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CONFIG = Path(__file__).parents[2] / "platform" / "config" / "pilot.yaml"

# Below this many attempts a cell reports its count and no rate. Five is not a
# statistical threshold; it is the point below which a percentage misleads more
# than it informs, and the report says which cells were suppressed.
MIN_CELL = 5


def load_pilot(path: Path = CONFIG) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text()))


def assign_cohort(employee_id: str, pilot: dict[str, Any]) -> str:
    """`native` or `control`, deterministically.

    Hashing rather than randomising at enrolment means the assignment survives a
    lost row, is reproducible from the employee id alone, and reshuffles when
    the pilot id changes.
    """
    if employee_id in set(pilot.get("control_exempt") or []):
        return "native"
    digest = hashlib.sha256(f"{pilot['pilot_id']}:{employee_id}".encode()).digest()
    # First eight bytes as a fraction of the space; stable across platforms.
    fraction = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return "control" if fraction < float(pilot["control_fraction"]) else "native"


def delivery_language(employee_id: str, preferred: str, pilot: dict[str, Any]) -> str:
    """Which language this employee is served, given their cohort.

    The control arm gets the English original; that is what makes it a control.
    """
    return "en-IN" if assign_cohort(employee_id, pilot) == "control" else preferred


@dataclass(frozen=True)
class Attempt:
    module_id: str
    language: str
    employee_id: str
    score: int
    max_score: int
    duration_ms: int
    cohort: str

    @property
    def fraction(self) -> float:
        return self.score / self.max_score if self.max_score else 0.0


def cell(attempts: list[Attempt], pass_mark: float) -> dict[str, Any]:
    """Pass rate, mean score and time-on-task for one group of attempts."""
    if not attempts:
        return {"attempts": 0, "pass_rate": None, "mean_score": None, "median_seconds": None}
    passed = sum(1 for a in attempts if a.fraction >= pass_mark)
    durations = sorted(a.duration_ms for a in attempts)
    middle = len(durations) // 2
    median_ms = (
        durations[middle] if len(durations) % 2 else (durations[middle - 1] + durations[middle]) / 2
    )
    return {
        "attempts": len(attempts),
        # Suppressed rather than rounded: a rate over a handful of attempts
        # reads as a finding and is not one.
        "pass_rate": (passed / len(attempts)) if len(attempts) >= MIN_CELL else None,
        "suppressed": len(attempts) < MIN_CELL,
        "mean_score": sum(a.fraction for a in attempts) / len(attempts),
        "median_seconds": round(median_ms / 1000, 1),
    }


def comprehension(attempts: list[Attempt], pilot: dict[str, Any]) -> dict[str, Any]:
    """The D10 business metric: pass rate by language and by cohort."""
    pass_mark = float(pilot["pass_mark"])
    by_language = {
        language: cell([a for a in attempts if a.language == language], pass_mark)
        for language in sorted({a.language for a in attempts})
    }
    by_cohort = {
        cohort: cell([a for a in attempts if a.cohort == cohort], pass_mark)
        for cohort in ("native", "control")
    }
    native, control = by_cohort["native"], by_cohort["control"]
    lift = (
        native["pass_rate"] - control["pass_rate"]
        if native["pass_rate"] is not None and control["pass_rate"] is not None
        else None
    )
    return {
        "pilot_id": pilot["pilot_id"],
        "pass_mark": pass_mark,
        "attempts": len(attempts),
        "by_language": by_language,
        "by_cohort": by_cohort,
        # PRD B6 calls this "directional improvement": the number the pilot is
        # for. None when either arm is too small to state one.
        "native_minus_control": lift,
        "suppressed_cells": sorted(
            name
            for group in (by_language, by_cohort)
            for name, values in group.items()
            if values.get("suppressed")
        ),
    }


def to_markdown(report: dict[str, Any]) -> str:
    """The same numbers as prose, for pasting into a pilot review."""

    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.0%}"

    lines = [
        f"# Comprehension — {report['pilot_id']}",
        "",
        f"{report['attempts']} attempts, pass mark {report['pass_mark']:.0%}.",
        "",
        "## By cohort",
        "",
        "| cohort | attempts | pass rate | mean score | median time |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, values in report["by_cohort"].items():
        lines.append(
            f"| {name} | {values['attempts']} | {pct(values['pass_rate'])} | "
            f"{pct(values['mean_score'])} | "
            f"{'—' if values['median_seconds'] is None else str(values['median_seconds']) + 's'} |"
        )
    lift = report["native_minus_control"]
    lines += [
        "",
        (
            f"Native-language cohort is **{lift:+.0%}** against the English-only control."
            if lift is not None
            else "**Not stated**: one arm has too few attempts to compare."
        ),
        "",
        "## By language",
        "",
        "| language | attempts | pass rate | mean score | median time |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, values in report["by_language"].items():
        lines.append(
            f"| {name} | {values['attempts']} | {pct(values['pass_rate'])} | "
            f"{pct(values['mean_score'])} | "
            f"{'—' if values['median_seconds'] is None else str(values['median_seconds']) + 's'} |"
        )
    if report["suppressed_cells"]:
        lines += [
            "",
            f"Rates withheld for {', '.join(report['suppressed_cells'])}: fewer than "
            f"{MIN_CELL} attempts, where a percentage misleads more than it informs.",
        ]
    return "\n".join(lines) + "\n"


def score_attempt(answers: dict[int, int], items: dict[int, int]) -> tuple[int, int]:
    """(score, max_score) for one attempt.

    `items` maps item_id to the correct option index. An unanswered item is
    wrong, not skipped: the pass mark is over the whole module, and letting an
    employee raise their rate by leaving items blank would make it meaningless.
    """
    return sum(1 for item_id, correct in items.items() if answers.get(item_id) == correct), len(
        items
    )
