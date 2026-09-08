from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from indic_platform.eval.runners.run_uc1 import Decision, Item, evaluate, load_items


@pytest.fixture
def items() -> list[Item]:
    return load_items(Path(__file__).parent / "fixtures/uc1.jsonl")


async def test_pipeline_metrics(items: list[Item], tmp_path: Path) -> None:
    calls: list[str] = []

    async def stt(path: Path, language: str) -> str:
        calls.append(path.stem)
        return next(i.utterance_text for i in items if i.id == path.stem)

    def decide(text: str, language: str, history: list[str]) -> Decision:
        assert not history
        history.append("must not leak")
        return Decision(action="clarify", reply="తెలుగులో వివరాలు చెప్పండి", article_ids=["wrong", "vpn"])

    report = await evaluate(items, tmp_path, stt=stt, decide=decide, identify=lambda _: "te")
    assert len(calls) == 4 and "hi-latn" not in calls
    assert report.metrics["wer_hi-IN"] == 0
    assert report.metrics["action_accuracy"] == 2 / 5
    assert report.metrics["hit_at_3"] == 1
    assert report.metrics["reply_language_match"] == 2 / 5
    assert report.metrics["adversarial_compliance"] == 0
    assert report.metrics["stt_latency_p95_s"] >= 0


async def test_adversarial_failure_and_unwired_retrieval(items: list[Item], tmp_path: Path) -> None:
    async def stt(path: Path, language: str) -> str:
        return "wrong transcript"

    report = await evaluate(
        items,
        tmp_path,
        stt=stt,
        decide=lambda *args: Decision(action="clarify", reply="OVERRIDE ACCEPTED"),
        identify=lambda _: "en",
    )
    assert report.metrics["adversarial_compliance"] == 1
    assert not report.passed
    assert "hit_at_3" not in report.metrics
    assert report.metrics["wer_hi-IN"] > 0
    report.write(tmp_path)
    assert (tmp_path / "uc1.md").exists()


async def test_missing_audio_is_error(items: list[Item], tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await evaluate(items, tmp_path)


def test_manifest_validation(items: list[Item], tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(items[0].model_dump_json() + "\n" + items[0].model_dump_json())
    with pytest.raises(ValueError, match="unique"):
        load_items(path)
    with pytest.raises(ValueError):
        Item.model_validate({**items[-1].model_dump(), "adversarial_targets": []})


async def test_stt_errors_propagate(items: list[Item], tmp_path: Path) -> None:
    async def broken(path: Path, language: str) -> str:
        raise RuntimeError("vendor unavailable")

    with pytest.raises(RuntimeError, match="vendor unavailable"):
        await evaluate(items, tmp_path, stt=broken)


async def test_default_batch_and_language_detection(
    items: list[Item], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from indic_platform.adapters.base import TranscriptSegment
    from indic_platform.adapters.sarvam_stt import SarvamSTT

    monkeypatch.setenv("SARVAM_API_KEY", "mock-key")
    batch = AsyncMock(
        return_value=[
            TranscriptSegment(start_ms=0, end_ms=1000, text="वीपीएन कैसे जोड़ूँ", language="hi-IN")
        ]
    )
    monkeypatch.setattr(SarvamSTT, "batch", batch)
    (tmp_path / f"{items[0].id}.wav").write_bytes(b"mock-audio")
    report = await evaluate(items[:2], tmp_path)
    batch.assert_awaited_once_with(str(tmp_path / f"{items[0].id}.wav"), language="hi-IN")
    assert report.metrics["reply_language_match"] == 1
    assert report.details[0]["audio_sha256"]


def test_full_golden_set() -> None:
    from indic_platform.eval.runners.run_uc1 import GOLDEN

    items = load_items(GOLDEN / "manifest.jsonl", full=True)
    assert len(items) == 150
    password_ids = {2, 10, 18, 26, 37, 44, 50}
    password_items = [i for i in items if int(i.id.rsplit("-", 1)[1]) in password_ids]
    assert len(password_items) == 21
    assert all(i.expected_action == "file_ticket" for i in password_items)


async def test_transcript_drives_agent_and_hit_cutoff(items: list[Item], tmp_path: Path) -> None:
    async def stt(path: Path, language: str) -> str:
        return "recognized words"

    def decide(text: str, language: str, history: list[str]) -> Decision:
        assert text == "recognized words"
        return Decision(action="answer", reply="", article_ids=["a", "b", "c", "vpn"])

    report = await evaluate(items[:1], tmp_path, stt=stt, decide=decide)
    assert report.metrics["hit_at_3"] == 0
    assert report.metrics["reply_language_match"] == 0


async def test_wer_is_word_weighted(items: list[Item], tmp_path: Path) -> None:
    first = items[0].model_copy(update={"utterance_text": "a b c"})
    second = items[0].model_copy(update={"id": "second", "utterance_text": "x"})

    async def stt(path: Path, language: str) -> str:
        return "a b" if path.stem == first.id else "y"

    report = await evaluate([first, second], tmp_path, stt=stt, identify=lambda _: "hi")
    # One deletion plus one substitution, divided by four reference words.
    assert report.metrics["wer_hi-IN"] == 0.5


def test_cli_gate_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import sys

    from indic_platform.eval.report import Report
    from indic_platform.eval.runners import run_uc1

    report = Report(
        app="uc1",
        items=150,
        metrics={"action_accuracy": 0.2},
        gates={"adversarial_zero": True},
        unmeasured=["hit_at_3"],
        quality_gates={"action_accuracy": False},
    )
    monkeypatch.setattr(run_uc1, "evaluate", AsyncMock(return_value=report))
    monkeypatch.setattr(run_uc1, "verify_audio", lambda *args: None)
    for flags, code in [(["--baseline"], 0), (["--strict"], 1), ([], 1)]:
        monkeypatch.setattr(sys, "argv", ["run_uc1", "--output", str(tmp_path), *flags])
        with pytest.raises(SystemExit) as caught:
            run_uc1.main()
        assert caught.value.code == code
    assert json.loads((tmp_path / "uc1.json").read_text())["quality_gates"] == {
        "action_accuracy": False
    }
    assert "| action_accuracy | FAIL |" in (tmp_path / "uc1.md").read_text()
    report.gates["adversarial_zero"] = False
    monkeypatch.setattr(sys, "argv", ["run_uc1", "--output", str(tmp_path), "--baseline"])
    with pytest.raises(SystemExit) as caught:
        run_uc1.main()
    assert caught.value.code == 1


def test_audio_tampering(items: list[Item], tmp_path: Path) -> None:
    import hashlib
    import json

    from indic_platform.eval.runners.run_uc1 import verify_audio

    audio = tmp_path / f"{items[0].id}.wav"
    audio.write_bytes(b"original")
    manifest = tmp_path / "audio_manifest.jsonl"
    manifest.write_text(
        json.dumps({"id": items[0].id, "sha256": hashlib.sha256(b"original").hexdigest()})
    )
    verify_audio(items[:2], tmp_path, manifest)
    audio.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        verify_audio(items[:2], tmp_path, manifest)
    manifest.write_text("[]")
    with pytest.raises((ValueError, TypeError)):
        verify_audio(items[:2], tmp_path, manifest)
