"""The four classic rate-limiting algorithms.

Each algorithm computes the time-dependent parts (window boundaries, weights,
refill rates) and hands the atomic read-check-write to the backend.

Keys wrap the client id in a Redis Cluster hash tag `{...}` so that every key
belonging to one client lands on the same shard. The sliding window counter
touches two keys in one script, which Cluster only allows within a single slot.
"""

import math
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import ClassVar

from .backends import Backend
from .models import Decision, Rule


class Algorithm(ABC):
    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]

    def __init__(self, backend: Backend, rule: Rule, clock: Callable[[], float] = time.time) -> None:
        self.backend = backend
        self.rule = rule
        self.clock = clock

    @abstractmethod
    async def hit(self, key: str) -> Decision:
        """Record one request for `key` and decide whether it's allowed."""


class FixedWindow(Algorithm):
    name = "fixed_window"
    title = "Fixed window"
    description = (
        "Counts requests per calendar window (e.g. 12:00:00-12:00:10). Cheapest option, "
        "but a client can send 2x the limit by bursting around a window boundary."
    )

    async def hit(self, key: str) -> Decision:
        now = self.clock()
        limit, window = self.rule.limit, self.rule.window
        start = math.floor(now / window) * window
        allowed, count = await self.backend.fixed_window(
            f"rl:fw:{{{key}}}:{round(start * 1000)}", limit, window
        )
        reset = start + window - now
        return Decision(allowed, limit, max(0, limit - count), reset, 0.0 if allowed else reset)


class SlidingLog(Algorithm):
    name = "sliding_log"
    title = "Sliding log"
    description = (
        "Stores a timestamp for every accepted request and counts the ones inside the "
        "last window. Exact, but memory grows with the limit (O(limit) per client)."
    )

    async def hit(self, key: str) -> Decision:
        now = self.clock()
        limit, window = self.rule.limit, self.rule.window
        member = f"{now!r}:{uuid.uuid4().hex[:8]}"
        allowed, count, oldest, newest = await self.backend.sliding_log(
            f"rl:log:{{{key}}}", limit, window, now, member
        )
        reset = newest + window - now if newest is not None else 0.0
        retry = 0.0 if allowed or oldest is None else oldest + window - now
        return Decision(allowed, limit, max(0, limit - count), reset, retry)


class SlidingWindowCounter(Algorithm):
    name = "sliding_window_counter"
    title = "Sliding window counter"
    description = (
        "Blends the previous window's count, weighted by how much of it still overlaps "
        "the sliding window, with the current count. Near-exact with O(1) memory."
    )

    async def hit(self, key: str) -> Decision:
        now = self.clock()
        limit, window = self.rule.limit, self.rule.window
        start = math.floor(now / window) * window
        elapsed = now - start
        weight = (window - elapsed) / window
        allowed, prev, curr = await self.backend.sliding_window_counter(
            f"rl:swc:{{{key}}}:{round(start * 1000)}",
            f"rl:swc:{{{key}}}:{round((start - window) * 1000)}",
            limit,
            weight,
            2 * window,
        )
        used = prev * weight + curr
        if curr:
            reset = (window - elapsed) + window
        elif prev:
            reset = window - elapsed
        else:
            reset = 0.0
        retry = 0.0 if allowed else self._retry_after(elapsed, prev, curr)
        return Decision(allowed, limit, max(0, math.floor(limit - used)), reset, retry)

    def _retry_after(self, elapsed: float, prev: int, curr: int) -> float:
        limit, window = self.rule.limit, self.rule.window
        # A request fits once prev * weight + curr + 1 <= limit.
        budget = limit - 1 - curr
        if budget >= 0 and prev > 0:
            # Still this window: wait for the previous window's weight to decay enough.
            return max(0.0, window * (1 - budget / prev) - elapsed)
        # Not until the next window, where this window's count becomes `prev`.
        wait = window - elapsed
        if curr > limit - 1:
            wait += window * (1 - (limit - 1) / curr)
        return wait


class TokenBucket(Algorithm):
    name = "token_bucket"
    title = "Token bucket"
    description = (
        "A bucket holds up to `limit` tokens and refills at limit/window per second; each "
        "request spends one. Allows short bursts while enforcing the average rate."
    )

    async def hit(self, key: str) -> Decision:
        now = self.clock()
        capacity = self.rule.limit
        rate = capacity / self.rule.window
        allowed, tokens = await self.backend.token_bucket(
            f"rl:tb:{{{key}}}", capacity, rate, now, capacity / rate + 1
        )
        reset = (capacity - tokens) / rate
        retry = 0.0 if allowed else (1 - tokens) / rate
        return Decision(allowed, capacity, math.floor(tokens), reset, retry)


ALGORITHMS: dict[str, type[Algorithm]] = {
    cls.name: cls for cls in (FixedWindow, SlidingLog, SlidingWindowCounter, TokenBucket)
}
