"""MinIO access for the watched recordings prefix.

Read-only by design for the pipeline itself: ingestion lists and fetches, it
never writes back into the recordings bucket. `push_golden` exists only to load
the synthetic golden WAVs in so the pipeline can be exercised end to end, and
says so.

## Transport is secure by default, and plaintext is opt-in per environment

`client()` is the one chokepoint every MinIO call in this app goes through --
the nightly ingestion sweep and `api.flag_audio`, which signs the reviewer's
recording URL with whatever client it is handed. A client built with
`secure=False` signs `http://` grants, and that URL *is* the bearer token for a
call recording: anyone on the path can lift it and replay the audio for its
180-second lifetime. So the default is `secure=True` and a deployment has to ask
for plaintext, rather than having to remember to ask for TLS.

Asking is not enough on its own. `MINIO_SECURE=false` is refused outside the
environments `auth.NON_PROD_ENVS` names, for the same reason the dev bypass is:
an unset or misspelt `ENV` is a plausible deployment typo, and the failure mode
here is confidential audio on the wire. The allow-list is imported rather than
restated -- two copies drift, and the copy that drifts is the one nobody
re-reads.

Local development is unaffected: `ENV=dev` with an explicit `MINIO_SECURE=false`
still talks to `http://localhost:9000`.
"""

import io
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from comms_surveillance.auth import NON_PROD_ENVS

BUCKET = os.environ.get("UC3_BUCKET", "comms-surveillance")

# Spellings of "no". Anything else -- unset, empty, "yes", or a typo like
# "flase" -- means TLS, because the safe direction for an unreadable value is
# the encrypted one. A typo then fails loudly at connect time instead of
# quietly downgrading the transport.
_FALSE = frozenset({"0", "false", "no", "off"})


class InsecureObjectStoreRefused(RuntimeError):
    """`MINIO_SECURE=false` outside a known non-prod `ENV`.

    A `RuntimeError` and not a `KeyError`: `api.flag_audio` maps `KeyError` to a
    503 "object storage is not configured", which would render a refusal to use
    plaintext as a routine outage and get somebody to restart the service rather
    than fix the transport.
    """


def secure() -> bool:
    """Whether to build clients over TLS. Raises if plaintext is not permitted here.

    Defaulting to True is the whole point: the previous default was False, so
    every deployment that never set the variable signed recording grants over
    `http://` and nothing said so.
    """
    requested_plaintext = os.environ.get("MINIO_SECURE", "").strip().lower() in _FALSE
    if not requested_plaintext:
        return True
    env = os.environ.get("ENV", "")
    if env.strip().lower() not in NON_PROD_ENVS:
        raise InsecureObjectStoreRefused(
            f"MINIO_SECURE=false is refused unless ENV is one of "
            f"{', '.join(sorted(NON_PROD_ENVS))}; got {env.strip() or '<unset>'!r}. "
            "Presigned recording URLs are bearer tokens for call audio and must not "
            "be minted over http. Remove MINIO_SECURE or put TLS in front of MinIO."
        )
    return False


def client() -> Any:
    from minio import Minio

    return Minio(
        os.environ.get("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
        # Last, after the credentials: a deployment with no MinIO configured at
        # all keeps the 503 `api.flag_audio` already gives it, and one that is
        # configured but plaintext gets a refusal that names the variable.
        secure=secure(),
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
    # Named after the recording, so a temp file left behind by a crash, or a
    # path in a stack trace, says which call it belongs to.
    handle, name = tempfile.mkstemp(prefix=f"{Path(key).stem}-", suffix=Path(key).suffix or ".wav")
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
