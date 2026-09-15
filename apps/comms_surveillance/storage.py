"""MinIO access for the watched recordings prefix.

Read-only by design for the pipeline itself: ingestion lists and fetches, it
never writes back into the recordings bucket. `push_golden` exists only to load
the synthetic golden WAVs in so the pipeline can be exercised end to end, and
says so.
"""

import io
import os
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
