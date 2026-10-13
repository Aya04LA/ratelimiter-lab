import pytest

from app.limiter import (
    ALGORITHMS,
    FixedWindow,
    MemoryBackend,
    Rule,
    SlidingLog,
    SlidingWindowCounter,
    TokenBucket,
)

ALL = list(ALGORITHMS.values())


def make(cls, backend, clock, limit=3, window=10.0):
    return cls(backend, Rule(limit, window), clock)


async def allowed(limiter, key: str, n: int) -> list[bool]:
    return [(await limiter.hit(key)).allowed for _ in range(n)]


# --- Behaviour every algorithm must share ------------------------------------


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.name)
async def test_allows_up_to_limit_then_rejects(cls, backend, clock):
    limiter = make(cls, backend, clock)
    assert await allowed(limiter, "a", 4) == [True, True, True, False]


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.name)
async def test_remaining_counts_down(cls, backend, clock):
    limiter = make(cls, backend, clock)
    assert [(await limiter.hit("a")).remaining for _ in range(4)] == [2, 1, 0, 0]


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.name)
async def test_keys_are_isolated(cls, backend, clock):
    limiter = make(cls, backend, clock)
    await allowed(limiter, "a", 3)
    assert await allowed(limiter, "b", 1) == [True]


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.name)
async def test_retry_after_is_accurate(cls, backend, clock):
    """Retrying exactly when told succeeds; retrying a moment earlier doesn't."""
    limiter = make(cls, backend, clock)
    await allowed(limiter, "a", 3)
    denied = await limiter.hit("a")
    assert not denied.allowed and denied.retry_after > 0

    clock.advance(denied.retry_after - 0.01)
    assert not (await limiter.hit("a")).allowed
    clock.advance(0.01 + 1e-6)
    assert (await limiter.hit("a")).allowed


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.name)
async def test_fully_recovers_after_reset(cls, backend, clock):
    limiter = make(cls, backend, clock)
    await allowed(limiter, "a", 3)
    last = await limiter.hit("a")
    clock.advance(last.reset_after + 1e-6)
    assert await allowed(limiter, "a", 4) == [True, True, True, False]


# --- What makes each algorithm different --------------------------------------


async def test_fixed_window_lets_double_through_at_boundary(backend, clock):
    """The known weakness: 2x the limit within 0.2s, straddling a boundary."""
    limiter = make(FixedWindow, backend, clock)
    clock.advance(9.9)
    assert await allowed(limiter, "a", 3) == [True] * 3
    clock.advance(0.2)
    assert await allowed(limiter, "a", 3) == [True] * 3


async def test_sliding_log_blocks_boundary_burst(backend, clock):
    limiter = make(SlidingLog, backend, clock)
    clock.advance(9.9)
    await allowed(limiter, "a", 3)
    clock.advance(0.2)
    assert await allowed(limiter, "a", 3) == [False] * 3


async def test_sliding_window_counter_weights_previous_window(backend, clock):
    limiter = make(SlidingWindowCounter, backend, clock)
    clock.advance(9.9)
    await allowed(limiter, "a", 3)

    clock.advance(0.2)  # 1% into the next window: 3 * 0.99 = 2.97 already used
    assert await allowed(limiter, "a", 1) == [False]

    clock.advance(6.9)  # 70% in: 3 * 0.3 = 0.9 used, so 2 more fit
    assert await allowed(limiter, "a", 3) == [True, True, False]


async def test_token_bucket_refills_at_steady_rate(backend, clock):
    limiter = make(TokenBucket, backend, clock, limit=10, window=10.0)  # 1 token/s
    assert await allowed(limiter, "a", 11) == [True] * 10 + [False]
    clock.advance(1)
    assert await allowed(limiter, "a", 2) == [True, False]
    clock.advance(2.5)
    assert await allowed(limiter, "a", 3) == [True, True, False]


async def test_token_bucket_never_exceeds_capacity(backend, clock):
    limiter = make(TokenBucket, backend, clock, limit=5, window=10.0)
    await allowed(limiter, "a", 1)
    clock.advance(10_000)
    assert await allowed(limiter, "a", 6) == [True] * 5 + [False]


# --- Validation and housekeeping ----------------------------------------------


@pytest.mark.parametrize("limit,window", [(0, 10), (5, 0), (5, -1)])
def test_rule_rejects_invalid_values(limit, window):
    with pytest.raises(ValueError):
        Rule(limit, window)


async def test_memory_backend_expires_idle_keys(clock):
    backend = MemoryBackend(clock=clock, sweep_every=1)
    limiter = make(SlidingLog, backend, clock)
    await allowed(limiter, "a", 1)
    assert len(backend) == 1
    clock.advance(11)
    await allowed(limiter, "b", 1)  # this write triggers a sweep
    assert len(backend) == 1
