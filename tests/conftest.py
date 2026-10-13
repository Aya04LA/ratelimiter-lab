import os

import fakeredis
import pytest
import redis.asyncio as redis

from app.limiter import MemoryBackend, RedisBackend


class FakeClock:
    """Deterministic time. Starts on a 10s boundary so window math is predictable."""

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture(params=["memory", "redis"])
async def backend(request, clock):
    """Every algorithm test runs on both backends to prove they behave identically.

    Uses fakeredis (real Lua via lupa) by default, or a real server when
    REDIS_TEST_URL is set, as it is in CI.
    """
    if request.param == "memory":
        yield MemoryBackend(clock=clock)
        return
    url = os.environ.get("REDIS_TEST_URL")
    if url:
        client = redis.from_url(url, decode_responses=True)
        await client.flushdb()
    else:
        client = fakeredis.FakeAsyncRedis(decode_responses=True)
    b = RedisBackend(client)
    yield b
    await b.close()
