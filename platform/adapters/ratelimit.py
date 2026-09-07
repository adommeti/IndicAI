import asyncio
import os
import time
from typing import Protocol

from indic_platform.config import settings as _settings  # noqa: F401


class Limiter(Protocol):
    async def acquire(self) -> None: ...


class TokenBucket:
    """Shared process-wide token bucket. Inject RedisTokenBucket across worker processes."""

    def __init__(self, rpm: float = 1000, capacity: float = 1) -> None:
        if rpm <= 0 or capacity < 1:
            raise ValueError("rpm must be positive and capacity >= 1")
        self.rate = rpm / 60
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.rate
            await asyncio.sleep(delay)


class RedisTokenBucket:
    """Atomic account bucket; Redis TIME avoids clock skew across Celery workers."""

    SCRIPT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local old = redis.call('HMGET', KEYS[1], 'tokens', 'updated')
local available = tonumber(old[1]) or tonumber(ARGV[2])
local elapsed = now - (tonumber(old[2]) or now)
local tokens = math.min(tonumber(ARGV[2]), available + elapsed * tonumber(ARGV[1]))
local delay = 0
if tokens >= 1 then tokens = tokens - 1 else delay = (1 - tokens) / tonumber(ARGV[1]) end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', now)
redis.call('EXPIRE', KEYS[1], 120)
return tostring(delay)
"""

    def __init__(self, url: str, account: str = "shared", rpm: float = 1000) -> None:
        from redis.asyncio import Redis

        self.redis = Redis.from_url(url)
        self.key = f"indic:rate:{account}"
        self.rpm = rpm

    async def acquire(self) -> None:
        while True:
            delay = float(await self.redis.eval(self.SCRIPT, 1, self.key, self.rpm / 60, 1))
            if delay <= 0:
                return
            await asyncio.sleep(delay)


SHARED_BUCKET = TokenBucket(float(os.getenv("ADAPTER_RPM", "1000")))
