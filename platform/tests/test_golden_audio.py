"""The UC1 golden audio must stay present and byte-identical to its manifest.

Evaluation numbers are only comparable across runs if the audio behind them is
the same audio. These checks fail loudly when a file is missing or has drifted,
rather than letting a run silently report a different WER.
"""

import hashlib
import json
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden" / "uc1_helpdesk"
MANIFEST = GOLDEN / "audio_manifest.jsonl"


def _rows() -> list[dict]:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_manifest_exists_and_is_complete() -> None:
    rows = _rows()
    assert len(rows) == 135, f"expected 135 manifest rows, found {len(rows)}"
    assert len({r["id"] for r in rows}) == len(rows), "duplicate ids in audio manifest"
    for row in rows:
        for key in ("id", "path", "sha256", "speaker", "model"):
            assert row.get(key), f"{row.get('id', '?')}: missing '{key}'"


@pytest.mark.parametrize("row", _rows(), ids=lambda r: r["id"])
def test_audio_file_matches_manifest_hash(row: dict) -> None:
    path = GOLDEN / row["path"]
    assert path.is_file(), (
        f"{row['id']}: {row['path']} is missing. Regenerate it with the recorded "
        f"speaker '{row['speaker']}' on {row['model']} and re-verify the hash."
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == row["sha256"], (
        f"{row['id']}: content changed (manifest {row['sha256'][:12]}…, file {digest[:12]}…). "
        "Do not edit the manifest to match; add a dated manifest and document the drift."
    )
