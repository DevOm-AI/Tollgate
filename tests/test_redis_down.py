import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.redis import get_redis
from app.limits.bucket import TakeResult
from app.limits.rate_limit import RateLimiter
from app.main import app
from tests.conftest import chat, create_key, reachable_redis

# Nothing listens on port 1, so every command fails to connect.
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"


@pytest.fixture
def redis_down(api):
    async def get_unreachable_redis() -> AsyncIterator[Redis]:
        redis = Redis.from_url(UNREACHABLE_REDIS_URL, socket_connect_timeout=0.2)
        try:
            yield redis
        finally:
            await redis.aclose()

    app.dependency_overrides[get_redis] = get_unreachable_redis


def test_requests_go_ahead_without_limits_when_redis_is_down(api, provider, redis_down, caplog):
    key = create_key(api, rpm_limit=1)["key"]

    with caplog.at_level(logging.WARNING, logger="app.limits.rate_limit"):
        codes = [chat(api, key).status_code for _ in range(3)]

    assert codes == [200, 200, 200]
    assert "rate limits not applied" in caplog.text


def test_no_rate_limit_headers_when_redis_is_down(api, provider, redis_down):
    key = create_key(api)["key"]

    response = chat(api, key)

    assert response.status_code == 200
    assert not any(name.startswith("x-ratelimit") for name in response.headers)


def test_warning_does_not_include_the_redis_address(api, provider, redis_down, caplog):
    key = create_key(api)["key"]

    with caplog.at_level(logging.WARNING, logger="app.limits.rate_limit"):
        chat(api, key)

    assert "127.0.0.1" not in caplog.text


class FailingSettleBucket:
    """Takes work; giving back fails, as if Redis went down mid-request."""

    async def take(self, name: str, capacity: int, cost: int = 1) -> TakeResult:
        return TakeResult(True, capacity, capacity - cost, 0, 0)

    async def adjust(self, name: str, capacity: int, tokens: int) -> TakeResult:
        raise RedisConnectionError("gone")


def test_settle_fails_open_when_redis_goes_down_mid_request(caplog):
    limiter = RateLimiter(FailingSettleBucket())

    async def run() -> TakeResult | None:
        admission = await limiter.admit(uuid.uuid4(), 5, 100, estimated_tokens=10)
        return await limiter.settle(admission, actual_tokens=3)

    with caplog.at_level(logging.WARNING, logger="app.limits.rate_limit"):
        assert asyncio.run(run()) is None

    assert "rate limits not applied" in caplog.text


def test_limits_apply_again_once_redis_is_back(api, provider, redis_down):
    key = create_key(api, rpm_limit=1)["key"]
    chat(api, key)

    app.dependency_overrides[get_redis] = reachable_redis

    assert [chat(api, key).status_code for _ in range(2)] == [200, 429]
