import json
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, Field

THRESHOLDS = Path(__file__).parent / "thresholds.yaml"


class Threshold(BaseModel):
    """One B6 gate: a number, the direction that number is good in, and its source.

    Direction is stored, never inferred from the metric's name. `.claude/rules/eval.md`
    requires the comparison to be explicit, and a name is not a direction: `wer_hi-IN`
    and `hit_at_3` are both "the number the runner emitted", and only this field says
    which way a change has to move before it counts as a regression.
    """

    direction: Literal["min", "max"]
    value: float
    source: str
    blocking: bool = False

    def holds(self, measured: float) -> bool:
        return measured >= self.value if self.direction == "min" else measured <= self.value

    def describe(self, measured: float) -> str:
        return f"{measured:g} vs {'>=' if self.direction == 'min' else '<='} {self.value:g}"


class Thresholds(BaseModel):
    """The B6 gates for one app, loaded from `platform/eval/thresholds.yaml`.

    A threshold speaks only about a metric the run actually produced. Whether a metric
    *should* have been produced is a separate question, answered by `Report.unmeasured`
    and `--strict`, so that a stage which did not run is reported unmeasured rather than
    silently scored against a gate it never met.
    """

    app: str
    metrics: dict[str, Threshold]

    @classmethod
    def load(cls, path: Path = THRESHOLDS, *, app: str = "uc1") -> Self:
        document = yaml.safe_load(path.read_text()) or {}
        if app not in document:
            raise ValueError(f"{path} carries no thresholds for {app}")
        return cls(app=app, metrics=document[app])

    @property
    def lower_is_better(self) -> set[str]:
        return {name for name, t in self.metrics.items() if t.direction == "max"}

    def results(self, metrics: Mapping[str, float]) -> dict[str, bool]:
        """Pass/fail for every gated metric this run measured, in threshold order."""
        return {name: t.holds(metrics[name]) for name, t in self.metrics.items() if name in metrics}

    def blocking_failures(self, metrics: Mapping[str, float]) -> dict[str, str]:
        """Build blockers only: the gates whose breach fails the harness in every mode."""
        return {
            name: t.describe(metrics[name])
            for name, t in self.metrics.items()
            if t.blocking and name in metrics and not t.holds(metrics[name])
        }


class Report(BaseModel):
    app: str
    stage: str = "P0 scaffold; B6 application gates not measured"
    items: int = Field(ge=1)
    metrics: dict[str, float]
    gates: dict[str, bool]
    unmeasured: list[str]
    quality_gates: dict[str, bool] = Field(default_factory=dict)
    # gate name -> the exact golden item ids that failed it. A rate alone ("5%
    # complied") is not actionable and `.claude/rules/eval.md` requires the items to be
    # named, so a blocking gate records who broke it, not just that somebody did.
    blocking_items: dict[str, list[str]] = Field(default_factory=dict)
    # metric -> "0.2 vs >= 0.85", filled from `Thresholds` so the report says what the
    # gate was, not only whether it passed.
    thresholds: dict[str, str] = Field(default_factory=dict)
    details: list[dict[str, object]] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.gates) and all(self.gates.values())

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{self.app}.json").write_text(self.model_dump_json(indent=2) + "\n")
        lines = [
            f"# {self.app}",
            "",
            self.stage,
            "",
            f"Items: {self.items}",
            "",
        ]
        if "hit_at_3_off" in self.metrics and "hit_at_3_on" in self.metrics:
            lines.extend(
                ["| Hit@3 | Translate off | Translate on | Eligible |", "|---|---:|---:|---:|"]
            )
            for label, suffix in [
                ("Overall", ""),
                ("Hindi", "_hi-IN"),
                ("Telugu", "_te-IN"),
                ("Tamil", "_ta-IN"),
            ]:
                off = self.metrics.get(f"hit_at_3_off{suffix}")
                on = self.metrics.get(f"hit_at_3_on{suffix}")
                if off is not None and on is not None:
                    eligible = self.metrics[f"retrieval_eligible{suffix}"]
                    lines.append(f"| {label} | {off:.2%} | {on:.2%} | {eligible:g} |")
            lines.extend(
                [
                    "",
                    "Translation spend is an attempted-request estimate; "
                    "timed-out requests may still be billed.",
                    "",
                ]
            )
        lines.extend(["| Metric | Value |", "|---|---:|"])
        lines.extend(f"| {key} | {value:g} |" for key, value in self.metrics.items())
        lines.extend(
            [
                "",
                f"Harness checks: {'PASS' if self.passed else 'FAIL'}",
                "",
                "Unmeasured B6 gates: " + ", ".join(self.unmeasured),
            ]
        )
        if self.quality_gates:
            lines.extend(["", "| B6 quality gate | Result |", "|---|---|"])
            lines.extend(
                f"| {key} | {'PASS' if passed else 'FAIL'}"
                + (f" ({self.thresholds[key]})" if key in self.thresholds else "")
                + " |"
                for key, passed in self.quality_gates.items()
            )
        for gate, ids in self.blocking_items.items():
            lines.extend(["", f"Items failing the blocking gate `{gate}`: " + ", ".join(ids)])
        (directory / f"{self.app}.md").write_text("\n".join(lines) + "\n")


def fails_regression(
    current: dict[str, float],
    previous: dict[str, float],
    tolerance: dict[str, float],
    *,
    lower_is_better: set[str] | None = None,
    thresholds: Thresholds | None = None,
) -> bool:
    """True when `current` breaches a B6 gate, drops a metric, or slips past `tolerance`.

    Two failures, both of which have to be caught. A run can sit above every absolute B6
    number and still have walked steadily downhill since the last run (`previous` +
    `tolerance`), and a run can hold its ground while sitting below a gate it never met
    (`thresholds`). Direction is explicit in both halves: `thresholds` carries min/max
    per metric, and `lower_is_better` names the rest. Passing neither is an error rather
    than a guess -- an inferred direction turns a regression into a silent pass.

    A metric in `previous` that `current` does not carry fails: a stage that stopped
    reporting is a regression, not an absence of evidence.
    """
    if thresholds is not None:
        if not all(thresholds.results(current).values()):
            return True
        if lower_is_better is None:
            lower_is_better = thresholds.lower_is_better
    if lower_is_better is None:
        raise ValueError(
            "Regression direction must be explicit: pass thresholds or lower_is_better"
        )
    if not previous.keys() <= current.keys():
        return True
    return any(
        (current[k] - old if k in lower_is_better else old - current[k]) > tolerance.get(k, 0)
        for k, old in previous.items()
    )


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())
