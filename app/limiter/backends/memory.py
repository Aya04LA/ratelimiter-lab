"""In-process backend for local development and tests.

Each method mirrors one Lua script in `redis_backend.py` line for line, so the two
backends make identical decisions. The test suite runs every algorithm against
both to prove it. Because asyncio is single-threaded and none of these methods
await, each call is atomic without any locking.

State lives in one process, so this backend cannot be shared between app
instances; use Redis for that.
"""

import time
from collections import deque
from collections.abc import Callable
from typing import Any


class MemoryBackend:
    name = "memory"

    def __init__(self, clock: Callable[[], float] = time.time, sweep_every: int = 1000) -> None:
        self._data: dict[str, Any] = {}
        self._expires: dict[str, float] = {}
        self._clock = clock
        self._writes = 0
        self._sweep_every = sweep_every

    def _get(self, key: str) -> Any:
        expires = self._expires.get(key)
        if expires is not None and expires <= self._clock():
            self._data.pop(key, None)
            self._expires.pop(key, None)
        return self._data.get(key)

    def _set(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = value
        self._expires[key] = self._clock() + ttl
        self._writes += 1
        if self._writes % self._sweep_every == 0:
            self._sweep()

    def _sweep(self) -> None:
        now = self._clock()
        for key in [k for k, exp in self._expires.items() if exp <= now]:
            self._data.pop(key, None)
            self._expires.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)

    async def fixed_window(self, key: str, limit: int, ttl: float) -> tuple[bool, int]:
        count = self._get(key) or 0
        if count < limit:
            count += 1
            self._set(key, count, ttl)
            return True, count
        return False, count

    async def sliding_window_counter(
        self, curr_key: str, prev_key: str, limit: int, weight: float, ttl: float
    ) -> tuple[bool, int, int]:
        prev = self._get(prev_key) or 0
        curr = self._get(curr_key) or 0
        if prev * weight + curr + 1 <= limit:
            curr += 1
            self._set(curr_key, curr, ttl)
            return True, prev, curr
        return False, prev, curr

    async def sliding_log(
        self, key: str, limit: int, window: float, now: float, member: str
    ) -> tuple[bool, int, float | None, float | None]:
        log: deque[float] = self._get(key) or deque()
        cutoff = now - window
        while log and log[0] <= cutoff:
            log.popleft()
        allowed = len(log) < limit
        if allowed:
            log.append(now)
        self._set(key, log, window)
        return allowed, len(log), (log[0] if log else None), (log[-1] if log else None)

    async def token_bucket(
        self, key: str, capacity: int, rate: float, now: float, ttl: float
    ) -> tuple[bool, float]:
        state = self._get(key)
        tokens, ts = state if state else (float(capacity), now)
        tokens = min(float(capacity), tokens + max(0.0, now - ts) * rate)
        allowed = tokens >= 1
        if allowed:
            tokens -= 1
        self._set(key, (tokens, now), ttl)
        return allowed, tokens

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass
