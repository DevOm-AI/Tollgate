import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from app.core.db import get_db
from app.core.redis import get_redis
from app.main import app
from app.providers.breaker import Breakers, get_breakers
from app.providers.catalog import Catalog, get_catalog
from app.providers.mock import MockProvider


class FakeDb:
    def __init__(self, fail: bool) -> None:
        self.fail = fail

    async def execute(self, statement):
        if self.fail:
            raise OperationalError("SELECT 1", None, Exception("password=secret host=db"))


class FakeRedis:
    def __init__(self, fail: bool) -> None:
        self.fail = fail

    async def ping(self) -> bool:
        if self.fail:
            raise RedisConnectionError("redis://cache:6379 refused")
        return True


@pytest.fixture
def client_with():
    def make(*, db_fails: bool = False, redis_fails: bool = False) -> TestClient:
        app.dependency_overrides[get_db] = lambda: FakeDb(db_fails)
        app.dependency_overrides[get_redis] = lambda: FakeRedis(redis_fails)
        app.dependency_overrides[get_catalog] = lambda: Catalog([MockProvider()])
        app.dependency_overrides[get_breakers] = lambda: breakers
        return TestClient(app)

    breakers = Breakers(threshold=2)

    yield make
    app.dependency_overrides.clear()


def test_health_ok_when_db_and_redis_respond(client_with):
    response = client_with().get("/health")

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["checks"]) == ("ok", {"database": "ok", "redis": "ok"})


def test_health_503_when_db_is_down(client_with):
    response = client_with(db_fails=True).get("/health")

    assert response.status_code == 503
    body = response.json()
    assert (body["status"], body["checks"]) == ("error", {"database": "error", "redis": "ok"})


def test_health_503_when_redis_is_down(client_with):
    response = client_with(redis_fails=True).get("/health")

    assert response.status_code == 503
    body = response.json()
    assert (body["status"], body["checks"]) == ("error", {"database": "ok", "redis": "error"})


def test_health_does_not_leak_error_details(client_with):
    body = client_with(db_fails=True, redis_fails=True).get("/health").text

    assert "secret" not in body
    assert "cache:6379" not in body


def test_health_shows_each_providers_breaker(client_with):
    client = client_with()
    breakers = app.dependency_overrides[get_breakers]()

    assert client.get("/health").json()["providers"] == {
        "mock": {"state": "closed", "recent_failures": 0}
    }

    breakers["mock"].record_failure()
    breakers["mock"].record_failure()
    response = client.get("/health")

    # An open breaker is shown, but Tollgate itself is still healthy.
    assert response.status_code == 200
    assert response.json()["providers"]["mock"]["state"] == "open"
