from typing import Protocol

from .memory import MemoryBackend
from .redis_backend import RedisBackend


class Backend(Protocol):
    """Atomic storage primitives, one per algorithm.

    Algorithms do the time math; backends only do the read-check-write step
    that must happen atomically.
    """

    name: str

    async def fixed_window(self, key: str, limit: int, ttl: float) -> tuple[bool, int]: ...

    async def sliding_window_counter(
        self, curr_key: str, prev_key: str, limit: int, weight: float, ttl: float
    ) -> tuple[bool, int, int]: ...

    async def sliding_log(
        self, key: str, limit: int, window: float, now: float, member: str
    ) -> tuple[bool, int, float | None, float | None]: ...

    async def token_bucket(
        self, key: str, capacity: int, rate: float, now: float, ttl: float
    ) -> tuple[bool, float]: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


__all__ = ["Backend", "MemoryBackend", "RedisBackend"]
