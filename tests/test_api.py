import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.limiter import BackendUnavailable, MemoryBackend
from app.main import create_app


def make_client(clock, backend=None, **settings):
    s = Settings(redis_url=None, default_limit=3, default_window=10.0, **settings)
    app = create_app(s, backend if backend is not None else MemoryBackend(clock=clock), clock)
    return TestClient(app)


@pytest.fixture
def client(clock):
    with make_client(clock) as c:
        yield c


class DownBackend(MemoryBackend):
    """Simulates Redis being unreachable."""

    name = "down"

    async def _fail(self, *args, **kwargs):
        raise BackendUnavailable("connection refused")

    fixed_window = sliding_window_counter = sliding_log = token_bucket = _fail

    async def ping(self) -> bool:
        return False


def test_index_serves_demo_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["backend"] == "memory"
    assert "X-Served-By" in r.headers


def test_algorithms_listing(client):
    body = client.get("/api/algorithms").json()
    assert {a["name"] for a in body["algorithms"]} == {
        "fixed_window",
        "sliding_log",
        "sliding_window_counter",
        "token_bucket",
    }
    assert body["limit"] == 3


@pytest.mark.parametrize(
    "algorithm", ["fixed_window", "sliding_log", "sliding_window_counter", "token_bucket"]
)
def test_demo_returns_429_with_standard_headers(client, algorithm):
    responses = [client.get(f"/api/demo/{algorithm}") for _ in range(4)]
    assert [r.status_code for r in responses] == [200, 200, 200, 429]
    assert [r.headers["RateLimit-Remaining"] for r in responses] == ["2", "1", "0", "0"]
    assert responses[0].headers["RateLimit-Limit"] == "3"
    assert "Retry-After" not in responses[0].headers
    assert int(responses[-1].headers["Retry-After"]) >= 1


def test_demo_isolates_client_ids(client):
    for _ in range(3):
        client.get("/api/demo/token_bucket", headers={"X-Client-Id": "tab-a"})
    assert client.get("/api/demo/token_bucket", headers={"X-Client-Id": "tab-a"}).status_code == 429
    assert client.get("/api/demo/token_bucket", headers={"X-Client-Id": "tab-b"}).status_code == 200


def test_malformed_client_id_falls_back_to_ip(client):
    bad = {"X-Client-Id": "../../etc/passwd"}
    codes = [client.get("/api/demo/fixed_window", headers=bad).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    # Same bucket as a request with no id at all.
    assert client.get("/api/demo/fixed_window").status_code == 429


def test_unknown_algorithm_is_404(client):
    assert client.get("/api/demo/leaky_bucket").status_code == 404


def test_check_service(client):
    body = {"key": "user:42", "algorithm": "sliding_log", "limit": 2, "window": 60}
    results = [client.post("/v1/check", json=body).json()["allowed"] for _ in range(3)]
    assert results == [True, True, False]


def test_check_rules_do_not_share_state(client):
    base = {"key": "user:42", "algorithm": "fixed_window", "window": 60}
    client.post("/v1/check", json={**base, "limit": 1})
    assert client.post("/v1/check", json={**base, "limit": 1}).json()["allowed"] is False
    assert client.post("/v1/check", json={**base, "limit": 5}).json()["allowed"] is True


@pytest.mark.parametrize(
    "patch",
    [
        {"limit": 0},
        {"window": 0},
        {"window": 100_000},
        {"algorithm": "nope"},
        {"key": ""},
        {"key": "has spaces"},
    ],
)
def test_check_validates_input(client, patch):
    body = {"key": "k", "algorithm": "token_bucket", "limit": 5, "window": 10, **patch}
    assert client.post("/v1/check", json=body).status_code == 422


def test_fail_open_when_backend_down(clock):
    with make_client(clock, DownBackend(), fail_open=True) as c:
        r = c.get("/api/demo/fixed_window")
        assert r.status_code == 200
        assert r.headers["X-RateLimit-Degraded"] == "1"
        assert c.get("/health").status_code == 503


def test_fail_closed_when_backend_down(clock):
    with make_client(clock, DownBackend(), fail_open=False) as c:
        r = c.get("/api/demo/fixed_window")
        assert r.status_code == 503
        assert r.headers["Retry-After"] == "1"
