"""Rate limits against a real Redis (skipped when redis-server is absent)."""

import shutil
import socket
import subprocess
import time

import pytest
import redis as redis_lib
from sqlalchemy import select

from app.models import FraudEvent
from app.rate_limit import RateLimiter
from tests.conftest import auth_headers


@pytest.fixture(scope="module")
def redis_url():
    if shutil.which("redis-server") is None:
        pytest.skip("redis-server not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", ""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    client = redis_lib.Redis.from_url(url)
    for _ in range(50):
        try:
            client.ping()
            break
        except redis_lib.RedisError:
            time.sleep(0.1)
    else:
        proc.terminate()
        pytest.skip("redis-server failed to start")
    yield url
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def limiter(redis_url):
    return RateLimiter(
        redis_lib.Redis.from_url(redis_url), enabled=True, fail_open=True
    )


def test_fixed_window_allows_then_blocks(limiter):
    assert all(limiter.allow("k1", limit=3) for _ in range(3))
    assert limiter.allow("k1", limit=3) is False


def test_keys_are_independent(limiter):
    assert limiter.allow("a", limit=1) is True
    assert limiter.allow("a", limit=1) is False
    assert limiter.allow("b", limit=1) is True


def test_disabled_limiter_always_allows():
    limiter = RateLimiter(None, enabled=False, fail_open=True)
    assert all(limiter.allow("k", limit=1) for _ in range(10))


def test_write_endpoints_deny_with_429_and_log_fraud_event(client, db, limiter):
    client.app.state.rate_limiter = limiter
    headers, user_id = auth_headers(client)

    # exhaust the per-user write budget
    from app.config import get_settings

    old = get_settings().rate_limit_writes_per_min
    get_settings().rate_limit_writes_per_min = 2
    try:
        assert client.post("/v1/checkin", headers=headers).status_code == 200
        assert client.post("/v1/checkin", headers=headers).status_code == 200
        resp = client.post("/v1/checkin", headers=headers)
        assert resp.status_code == 429
    finally:
        get_settings().rate_limit_writes_per_min = old
        client.app.state.rate_limiter = RateLimiter(
            None, enabled=False, fail_open=True
        )

    events = (
        db.execute(
            select(FraudEvent).where(FraudEvent.kind == "rate_limited_writes")
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].user_id == user_id
