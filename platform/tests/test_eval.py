import json
from pathlib import Path

from indic_platform.eval.report import fails_regression
from indic_platform.eval.runners.run import evaluate
from indic_platform.security.harden import new_canary, validate_or_reject


def test_reports_and_regressions(tmp_path: Path) -> None:
    for app in ["uc1", "uc2", "uc3"]:
        report = evaluate(app)
        assert report.passed and report.unmeasured
        report.write(tmp_path)
        assert json.loads((tmp_path / f"{app}.json").read_text())["items"] == 8
        assert "Unmeasured B6" in (tmp_path / f"{app}.md").read_text()
    assert fails_regression(
        {"accuracy": 0.8}, {"accuracy": 0.9}, {"accuracy": 0.02}, lower_is_better=set()
    )
    assert fails_regression({"latency": 3}, {"latency": 2}, {}, lower_is_better={"latency"})
    assert fails_regression({}, {"accuracy": 1}, {}, lower_is_better=set())


def test_canary_rejected() -> None:
    import pytest
    from pydantic import BaseModel

    class Result(BaseModel):
        answer: str

    canary = new_canary()
    with pytest.raises(ValueError, match="Canary"):
        validate_or_reject(json.dumps({"answer": canary}), Result, canary=canary)
