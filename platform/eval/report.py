import json
from pathlib import Path

from pydantic import BaseModel, Field


class Report(BaseModel):
    app: str
    stage: str = "P0 scaffold; B6 application gates not measured"
    items: int = Field(ge=1)
    metrics: dict[str, float]
    gates: dict[str, bool]
    unmeasured: list[str]
    quality_gates: dict[str, bool] = Field(default_factory=dict)
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
                f"| {key} | {'PASS' if passed else 'FAIL'} |"
                for key, passed in self.quality_gates.items()
            )
        (directory / f"{self.app}.md").write_text("\n".join(lines) + "\n")


def fails_regression(
    current: dict[str, float],
    previous: dict[str, float],
    tolerance: dict[str, float],
    *,
    lower_is_better: set[str],
) -> bool:
    if not previous.keys() <= current.keys():
        return True
    return any(
        (current[k] - old if k in lower_is_better else old - current[k]) > tolerance.get(k, 0)
        for k, old in previous.items()
    )


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())
