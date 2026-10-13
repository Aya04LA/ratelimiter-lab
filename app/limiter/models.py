from dataclasses import dataclass


class BackendUnavailable(Exception):
    """The storage backend could not be reached (e.g. Redis is down)."""


@dataclass(frozen=True)
class Rule:
    """Allow `limit` requests per `window` seconds."""

    limit: int
    window: float

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        if self.window <= 0:
            raise ValueError("window must be > 0")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    # Seconds until the limiter is back to a completely fresh state.
    reset_after: float
    # Seconds until the next request would be allowed (0 when allowed).
    retry_after: float
