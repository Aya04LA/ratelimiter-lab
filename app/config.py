import os
from dataclasses import dataclass


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Empty means use the in-process memory backend (single instance only).
    redis_url: str | None = None
    default_limit: int = 10
    default_window: float = 10.0
    # When the backend is unreachable: True lets traffic through, False returns 503.
    fail_open: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            redis_url=os.environ.get("REDIS_URL") or None,
            default_limit=int(os.environ.get("DEFAULT_LIMIT", "10")),
            default_window=float(os.environ.get("DEFAULT_WINDOW", "10")),
            fail_open=_bool(os.environ.get("FAIL_OPEN", "true")),
        )
