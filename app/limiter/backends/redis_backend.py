"""Redis backend: every check is a single Lua script.

Redis runs each script atomically, so concurrent requests from any number of
app instances can't race between "read the counter" and "write the counter".
One script call is also one network round trip per request.

Numbers cross the Python/Lua boundary as strings formatted with %.17g (Python
sends repr()), which round-trips doubles exactly. That keeps the arithmetic
here bit-identical to MemoryBackend.
"""

import math

import redis.asyncio as redis
from redis.exceptions import RedisError

from ..models import BackendUnavailable

FIXED_WINDOW = """
local limit = tonumber(ARGV[1])
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count < limit then
  count = redis.call('INCR', KEYS[1])
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return {1, count}
end
return {0, count}
"""

SLIDING_WINDOW_COUNTER = """
local limit = tonumber(ARGV[1])
local weight = tonumber(ARGV[2])
local prev = tonumber(redis.call('GET', KEYS[2]) or '0')
local curr = tonumber(redis.call('GET', KEYS[1]) or '0')
if prev * weight + curr + 1 <= limit then
  curr = redis.call('INCR', KEYS[1])
  redis.call('PEXPIRE', KEYS[1], ARGV[3])
  return {1, prev, curr}
end
return {0, prev, curr}
"""

SLIDING_LOG = """
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', string.format('%.17g', now - window))
local count = redis.call('ZCARD', KEYS[1])
local allowed = 0
if count < limit then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  count = count + 1
  allowed = 1
end
redis.call('PEXPIRE', KEYS[1], ARGV[5])
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local newest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
return {allowed, count, oldest[2] or '', newest[2] or ''}
"""

TOKEN_BUCKET = """
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
local t = string.format('%.17g', tokens)
redis.call('HSET', KEYS[1], 'tokens', t, 'ts', ARGV[3])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
return {allowed, t}
"""


def _ms(seconds: float) -> int:
    return max(1, math.ceil(seconds * 1000))


class RedisBackend:
    name = "redis"

    def __init__(self, client: redis.Redis) -> None:
        self._r = client
        self._fixed = client.register_script(FIXED_WINDOW)
        self._swc = client.register_script(SLIDING_WINDOW_COUNTER)
        self._log = client.register_script(SLIDING_LOG)
        self._bucket = client.register_script(TOKEN_BUCKET)

    @classmethod
    def from_url(cls, url: str) -> "RedisBackend":
        client = redis.from_url(
            url,
            decode_responses=True,
            # Fail fast: a rate limiter that hangs is worse than one that's down.
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
            health_check_interval=30,
        )
        return cls(client)

    async def _run(self, script, keys: list[str], args: list) -> list:
        try:
            return await script(keys=keys, args=args)
        except RedisError as e:
            raise BackendUnavailable(str(e)) from e

    async def fixed_window(self, key: str, limit: int, ttl: float) -> tuple[bool, int]:
        allowed, count = await self._run(self._fixed, [key], [limit, _ms(ttl)])
        return bool(allowed), int(count)

    async def sliding_window_counter(
        self, curr_key: str, prev_key: str, limit: int, weight: float, ttl: float
    ) -> tuple[bool, int, int]:
        allowed, prev, curr = await self._run(
            self._swc, [curr_key, prev_key], [limit, repr(weight), _ms(ttl)]
        )
        return bool(allowed), int(prev), int(curr)

    async def sliding_log(
        self, key: str, limit: int, window: float, now: float, member: str
    ) -> tuple[bool, int, float | None, float | None]:
        allowed, count, oldest, newest = await self._run(
            self._log, [key], [limit, repr(window), repr(now), member, _ms(window)]
        )
        return (
            bool(allowed),
            int(count),
            float(oldest) if oldest else None,
            float(newest) if newest else None,
        )

    async def token_bucket(
        self, key: str, capacity: int, rate: float, now: float, ttl: float
    ) -> tuple[bool, float]:
        allowed, tokens = await self._run(
            self._bucket, [key], [capacity, repr(rate), repr(now), _ms(ttl)]
        )
        return bool(allowed), float(tokens)

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        await self._r.aclose()
