"""MinIO access for the watched recordings prefix.

Read-only by design for the pipeline itself: ingestion lists and fetches, it
never writes back into the recordings bucket. `push_golden` exists only to load
the synthetic golden WAVs in so the pipeline can be exercised end to end, and
says so.
"""

import io
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

BUCKET = os.environ.get("UC3_BUCKET", "comms-surveillance")


def client() -> Any:
    from minio import Minio

    return Minio(
        os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
    )


@contextmanager
def fetch_object(minio: Any, key: str, *, bucket: str = BUCKET) -> Iterator[Path]:
    """Download one object to a temp file for the duration of the block.

    The adapter boundary (`platform/adapters/sarvam_common.py:local_media`) takes
    local paths and `file://` URIs only -- "apps materialize object-store assets"
    -- so handing it `s3://...` raises. Materializing here is that step, and the
    file is removed afterwards because these are call recordings and PRD E2's
    retention question is unanswered.
    """
    handle, name = tempfile.mkstemp(suffix=Path(key).suffix or ".wav")
    os.close(handle)
    path = Path(name)
    try:
        minio.fget_object(bucket, key, str(path))
        yield path
    finally:
        path.unlink(missing_ok=True)


def push_golden(
    source: Path, *, minio: Any | None = None, bucket: str = BUCKET, prefix: str = "recordings/"
) -> list[str]:
    """Upload the golden WAVs so `make ingest-golden-audio` has something to sweep.

    Test fixture loading, not part of the nightly path. The keys are the file
    names, so re-running it overwrites rather than duplicating -- and because
    `calls.source_key` is unique, re-running the sweep afterwards still yields
    one call per recording.
    """
    minio = minio or client()
    if not minio.bucket_exists(bucket):
        minio.make_bucket(bucket)
    keys = []
    for wav in sorted(source.glob("*.wav")):
        data = wav.read_bytes()
        key = f"{prefix}{wav.name}"
        minio.put_object(bucket, key, io.BytesIO(data), length=len(data), content_type="audio/wav")
        keys.append(key)
    return keys
