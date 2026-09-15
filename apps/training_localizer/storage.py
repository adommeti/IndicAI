"""Object storage for produced artifacts, with a checksum on the way in.

Every artifact row in PRD D6 carries a `sha256`, and the manifest is "the audit
record of what was delivered in which version" (D8). That only holds if the hash
is computed from the bytes actually stored, so `put` hashes and uploads in one
step and returns both the URI and the digest. Nothing else writes artifact rows.
"""

import hashlib
import io
import os
from dataclasses import dataclass
from typing import Any

BUCKET = os.environ.get("MINIO_BUCKET", "training-localizer")


@dataclass(frozen=True)
class Stored:
    uri: str
    sha256: str
    bytes: int


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def client() -> Any:
    from minio import Minio

    endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    return Minio(
        endpoint,
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
    )


def object_key(module_id: str, language: str, kind: str, suffix: str) -> str:
    return f"{module_id}/{language}/{kind}{suffix}"


def put(data: bytes, *, key: str, content_type: str, minio: Any | None = None) -> Stored:
    """Store bytes and return the URI and digest of exactly what was stored."""
    minio = minio or client()
    if not minio.bucket_exists(BUCKET):
        minio.make_bucket(BUCKET)
    minio.put_object(BUCKET, key, io.BytesIO(data), length=len(data), content_type=content_type)
    return Stored(uri=f"s3://{BUCKET}/{key}", sha256=digest(data), bytes=len(data))


class InMemoryStorage:
    """A stand-in used by the tests and by any run without MinIO.

    It hashes identically, so a manifest produced against it is structurally the
    same document -- only the URI scheme differs, and it says so.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, data: bytes, *, key: str, content_type: str) -> Stored:
        self.objects[key] = data
        return Stored(uri=f"memory://{BUCKET}/{key}", sha256=digest(data), bytes=len(data))

    def get(self, key: str) -> bytes:
        return self.objects[key]


class MinioStorage:
    """The `Storage` protocol `production.py` expects, backed by MinIO.

    One place rather than a shim per Celery task: the tasks differ in what they
    produce, not in where it goes.
    """

    def __init__(self, minio: Any | None = None) -> None:
        self._minio = minio or client()

    def put(self, data: bytes, *, key: str, content_type: str) -> Stored:
        return put(data, key=key, content_type=content_type, minio=self._minio)
