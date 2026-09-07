import argparse
import json
from pathlib import Path

from indic_platform.eval.report import Report
from indic_platform.security.harden import evidence_is_exact, wrap_untrusted
from indic_platform.security.redact import redact
from pydantic import BaseModel


class Item(BaseModel):
    id: str
    source: str
    evidence: str
    expected_exact: bool
    sensitive: str | None = None


GATES = {
    "uc1": [
        "WER hi<=15% te/ta<=20%",
        "action>=85%",
        "hit@3>=80%",
        "groundedness>=95%",
        "audio p50<=2s p95<=3.5s",
    ],
    "uc2": ["semantic mean>=4", "terminology=100%", "pilot comprehension improvement"],
    "uc3": ["precision>=80%", "recall>=85%", "injection success=0%", "speaker attribution>=90%"],
}


def evaluate(app: str) -> Report:
    path = Path(__file__).parents[1] / "golden" / app / "scaffold.jsonl"
    items = [Item.model_validate_json(line) for line in path.read_text().splitlines() if line]
    if not items or len({item.id for item in items}) != len(items):
        raise ValueError("Golden set must be nonempty with unique IDs")
    exact = sum(evidence_is_exact(i.source, i.evidence) == i.expected_exact for i in items)
    wraps = sum(wrap_untrusted(i.source).count("</untrusted_data>") == 1 for i in items)
    redacts = sum(i.sensitive is None or i.sensitive not in redact(i.source) for i in items)
    metrics = {
        "evidence_check_accuracy": exact / len(items),
        "delimiter_integrity": wraps / len(items),
        "redaction_accuracy": redacts / len(items),
    }
    return Report(
        app=app,
        items=len(items),
        metrics=metrics,
        gates={k: v == 1 for k, v in metrics.items()},
        unmeasured=GATES[app],
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", choices=list(GATES), required=True)
    parser.add_argument("--output", type=Path, default=Path("docs/eval"))
    args = parser.parse_args()
    report = evaluate(args.app)
    report.write(args.output)
    print(
        json.dumps(
            {
                "app": report.app,
                "stage": report.stage,
                "items": report.items,
                "metrics": report.metrics,
                "passed": report.passed,
            }
        )
    )
    raise SystemExit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
