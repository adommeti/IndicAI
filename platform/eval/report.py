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
            "| Metric | Value |",
            "|---|---:|",
        ]
        lines.extend(f"| {key} | {value:g} |" for key, value in self.metrics.items())
        lines.extend(
            [
                "",
                f"P0 checks: {'PASS' if self.passed else 'FAIL'}",
                "",
                "Unmeasured B6 gates: " + ", ".join(self.unmeasured),
            ]
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
