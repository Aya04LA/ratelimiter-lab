import logging
import math
import re
import socket
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .limiter import (
    ALGORITHMS,
    Algorithm,
    Backend,
    BackendUnavailable,
    Decision,
    MemoryBackend,
    RedisBackend,
    Rule,
)

log = logging.getLogger("ratelimiter")

STATIC_DIR = Path(__file__).parent / "static"
INSTANCE = socket.gethostname()
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class CheckRequest(BaseModel):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")
    algorithm: str
    limit: int = Field(ge=1, le=100_000)
    window: float = Field(gt=0, le=86_400)

    @field_validator("algorithm")
    @classmethod
    def known_algorithm(cls, v: str) -> str:
        if v not in ALGORITHMS:
            raise ValueError(f"must be one of {sorted(ALGORITHMS)}")
        return v


def client_key(request: Request) -> str:
    """Identify the caller for the demo endpoints.

    The demo keys on IP plus a per-tab id so visitors sharing a network (office,
    campus, mobile carrier) don't eat each other's quota. A real API would key
    on the authenticated user or API key instead, because a client can rotate a
    self-chosen id at will.
    """
    ip = request.client.host if request.client else "unknown"
    client_id = request.headers.get("x-client-id", "")
    return f"{ip}:{client_id}" if CLIENT_ID.match(client_id) else ip


def rate_limit_headers(d: Decision) -> dict[str, str]:
    # Header names follow the IETF draft "RateLimit header fields for HTTP".
    headers = {
        "RateLimit-Limit": str(d.limit),
        "RateLimit-Remaining": str(d.remaining),
        "RateLimit-Reset": str(math.ceil(d.reset_after)),
    }
    if not d.allowed:
        headers["Retry-After"] = str(max(1, math.ceil(d.retry_after)))
    return headers


def create_app(
    settings: Settings | None = None,
    backend: Backend | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if backend is None:
        backend = RedisBackend.from_url(settings.redis_url) if settings.redis_url else MemoryBackend()
    default_rule = Rule(settings.default_limit, settings.default_window)
    limiters: dict[str, Algorithm] = {
        name: cls(backend, default_rule, clock) for name, cls in ALGORITHMS.items()
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("backend=%s rule=%s fail_open=%s", backend.name, default_rule, settings.fail_open)
        yield
        await backend.close()

    app = FastAPI(
        title="Rate Limiter Lab",
        description="Four rate-limiting algorithms backed by atomic Redis Lua scripts.",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def served_by(request: Request, call_next):
        # Lets the demo (and you) see which instance behind the load balancer answered.
        response = await call_next(request)
        response.headers["X-Served-By"] = INSTANCE
        return response

    async def decide(limiter: Algorithm, key: str) -> Decision | None:
        """Run the limiter; None means the backend is down and we failed open."""
        try:
            return await limiter.hit(key)
        except BackendUnavailable as e:
            log.warning("backend unavailable: %s", e)
            if settings.fail_open:
                return None
            raise HTTPException(503, "Rate limiter backend unavailable", {"Retry-After": "1"})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    async def health() -> JSONResponse:
        ok = await backend.ping()
        return JSONResponse(
            {"status": "ok" if ok else "degraded", "backend": backend.name, "instance": INSTANCE},
            status_code=200 if ok else 503,
        )

    @app.get("/api/algorithms")
    async def algorithms() -> dict:
        return {
            "server_time": clock(),
            "limit": default_rule.limit,
            "window": default_rule.window,
            "backend": backend.name,
            "algorithms": [
                {"name": cls.name, "title": cls.title, "description": cls.description}
                for cls in ALGORITHMS.values()
            ],
        }

    @app.get("/api/demo/{algorithm}")
    async def demo(algorithm: str, request: Request) -> JSONResponse:
        """A sample endpoint protected by the chosen algorithm (default rule)."""
        limiter = limiters.get(algorithm)
        if limiter is None:
            raise HTTPException(404, f"Unknown algorithm {algorithm!r}")
        decision = await decide(limiter, f"demo:{algorithm}:{client_key(request)}")
        if decision is None:
            return JSONResponse({"ok": True, "degraded": True}, headers={"X-RateLimit-Degraded": "1"})
        body = {"ok": decision.allowed, "server_time": clock(), **asdict(decision)}
        return JSONResponse(
            body, status_code=200 if decision.allowed else 429, headers=rate_limit_headers(decision)
        )

    @app.post("/v1/check")
    async def check(req: CheckRequest) -> JSONResponse:
        """Rate limiting as a service: other backends ask "may `key` proceed?".

        Always 200; the answer is in `allowed`. The rule is part of the storage
        key so callers using different rules for the same key don't collide.
        """
        limiter = ALGORITHMS[req.algorithm](backend, Rule(req.limit, req.window), clock)
        decision = await decide(limiter, f"svc:{req.algorithm}:{req.limit}:{req.window!r}:{req.key}")
        if decision is None:
            return JSONResponse({"allowed": True, "degraded": True})
        return JSONResponse(asdict(decision), headers=rate_limit_headers(decision))

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = create_app()
