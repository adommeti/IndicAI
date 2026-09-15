"""Generate local-only stack credentials without changing vendor keys in .env."""

import os
import secrets
from pathlib import Path

path = Path(".env.stack")
if not path.exists():
    password = secrets.token_hex(16)
    values = {
        "STACK_PASSWORD": password,
        "NEXTAUTH_SECRET": secrets.token_hex(32),
        "SALT": secrets.token_hex(32),
        "ENCRYPTION_KEY": secrets.token_hex(32),
        "LANGFUSE_PUBLIC_KEY": "pk-lf-" + secrets.token_hex(16),
        "LANGFUSE_SECRET_KEY": "sk-lf-" + secrets.token_hex(24),  # pragma: allowlist secret
        "LANGFUSE_HOST": "http://localhost:3002",
        "DATABASE_URL": f"postgresql+psycopg://platform:{password}@localhost:15433/platform",
        "REDIS_URL": "redis://localhost:6380/0",
        "MINIO_ENDPOINT": "localhost:9000",
        "MINIO_ACCESS_KEY": "minio",
        "MINIO_SECRET_KEY": password,
        "LIVEKIT_API_KEY": "devkey",  # pragma: allowlist secret - public local key identifier
        "LIVEKIT_API_SECRET": password,
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("".join(f"{key}={value}\n" for key, value in values.items()))
    print("Created .env.stack (mode 0600); vendor keys remain in .env.")
