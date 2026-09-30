import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models import KeySpend
from app.providers.base import ProviderError
from app.providers.breaker import CircuitBreaker
from app.providers.catalog import Catalog, get_catalog
from app.providers.mock import MockProvider
from tests.conftest import FAKE_PRICE, FakeProvider, chat, create_key


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def breaker(clock) -> CircuitBreaker:
    return CircuitBreaker(threshold=5, window_s=30, open_s=30, clock=clock)


def fail(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        breaker.record_failure()


# --- State machine ---


def test_breaker_starts_closed(breaker):
    assert breaker.state == "closed"
    assert breaker.allow()


def test_five_failures_in_thirty_seconds_open_it(breaker, clock):
    fail(breaker, 4)
    assert breaker.state == "closed"

    breaker.record_failure()

    assert breaker.state == "open"
    assert not breaker.allow()
    assert not breaker.available()


def test_failures_spread_over_more_than_thirty_seconds_do_not(breaker, clock):
    for _ in range(10):
        breaker.record_failure()
        clock.now += 8  # At most 4 failures in any 30 s.

    assert breaker.state == "closed"


def test_a_success_clears_the_failures(breaker):
    fail(breaker, 4)
    breaker.record_success()
    fail(breaker, 4)

    assert breaker.state == "closed"


def test_after_thirty_seconds_one_test_request_goes_through(breaker, clock):
    fail(breaker, 5)
    clock.now += 29.9
    assert not breaker.allow()

    clock.now += 0.1
    assert breaker.state == "half_open"
    assert breaker.allow()
    # Only one: the rest wait for the test request's result.
    assert not breaker.allow()
    assert not breaker.available()


def test_test_request_succeeding_closes_it(breaker, clock):
    fail(breaker, 5)
    clock.now += 30
    breaker.allow()

    breaker.record_success()

    assert breaker.state == "closed"
    assert breaker.allow()


def test_test_request_failing_opens_it_again(breaker, clock):
    fail(breaker, 5)
    clock.now += 30
    breaker.allow()

    breaker.record_failure()

    assert breaker.state == "open"
    clock.now += 29
    assert not breaker.allow()


def test_test_request_that_never_reports_back_stops_blocking(breaker, clock):
    fail(breaker, 5)
    clock.now += 30
    assert breaker.allow()

    clock.now += 30

    assert breaker.allow()


def test_cancelled_test_request_frees_the_slot(breaker, clock):
    fail(breaker, 5)
    clock.now += 30
    breaker.allow()

    breaker.release()

    assert breaker.allow()


def test_seconds_until_available(breaker, clock):
    assert breaker.seconds_until_available() == 0
    fail(breaker, 5)
    clock.now += 10.5

    assert breaker.seconds_until_available() == 20


def test_wait_is_positive_while_the_test_request_is_in_flight(breaker, clock):
    fail(breaker, 5)
    clock.now += 30
    breaker.allow()  # The test request goes out.
    clock.now += 5

    assert breaker.seconds_until_available() == 25


# --- In the endpoint ---


@pytest.fixture
def fakes(api):
    primary, fallback = FakeProvider(), FakeProvider()
    fallback.name = "fake2"
    for name in ("fake", "fake2"):
        assert api.put("/admin/prices", json=FAKE_PRICE | {"provider": name}).status_code == 200
    app.dependency_overrides[get_catalog] = lambda: Catalog(
        [primary, fallback], {"fake-chat": ["fake/test-model", "fake2/test-model"]}
    )
    return primary, fallback


def test_open_breaker_skips_the_provider_without_waiting_on_it(api, fakes, breakers):
    primary, fallback = fakes
    key = create_key(api)["key"]
    primary.error = ProviderError("fake: HTTP 503", status_code=503)

    for _ in range(5):
        chat(api, key, model="fake-chat")
    assert len(primary.requests) == 5
    assert breakers["fake"].state == "open"

    response = chat(api, key, model="fake-chat")

    # Straight to the fallback: the primary isn't asked again while its breaker is open.
    assert response.status_code == 200
    assert len(primary.requests) == 5
    assert len(fallback.requests) == 6


def test_rejected_requests_do_not_open_the_breaker(api, fakes, breakers):
    primary, _ = fakes
    key = create_key(api)["key"]
    primary.error = ProviderError("fake: HTTP 400", status_code=400, upstream_message="bad")

    for _ in range(6):
        chat(api, key, model="fake-chat")

    assert breakers["fake"].state == "closed"


def test_every_breaker_open_is_a_503_that_holds_nothing(api, fakes, breakers, db_engine):
    created = create_key(api)
    fail(breakers["fake"], 5)
    fail(breakers["fake2"], 5)

    response = chat(api, created["key"], model="fake-chat")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "providers_unavailable"
    assert 1 <= int(response.headers["Retry-After"]) <= 30
    with Session(db_engine) as session:
        held = session.scalars(
            select(KeySpend).where(KeySpend.key_id == uuid.UUID(created["id"]))
        ).one_or_none()
    assert held is None


def test_stream_skips_an_open_provider_too(api, fakes, breakers):
    primary, fallback = fakes
    key = create_key(api)["key"]
    fail(breakers["fake"], 5)

    response = api.post(
        "/v1/chat/completions",
        json={
            "model": "fake-chat",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        },
        headers={"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    assert primary.requests == []
    assert len(fallback.requests) == 1


def test_mock_failing_every_time_opens_its_breaker_and_shows_on_health(api, breakers):
    fallback = FakeProvider()
    assert api.put("/admin/prices", json=FAKE_PRICE).status_code == 200
    mock = MockProvider(error_rate=1.0)
    app.dependency_overrides[get_catalog] = lambda: Catalog(
        [mock, fallback], {"demo-chat": ["mock", "fake/test-model"]}
    )
    key = create_key(api)["key"]

    codes = [chat(api, key, model="demo-chat").status_code for _ in range(8)]

    assert codes == [200] * 8
    health = api.get("/health").json()
    assert health["providers"]["mock"]["state"] == "open"
    assert health["providers"]["fake"]["state"] == "closed"


class ClosesBehindYou(CircuitBreaker):
    """Looks available when the request is admitted, then refuses at attempt time: as if the
    breaker opened in between (another request's failure)."""

    def available(self) -> bool:
        return True

    def allow(self) -> bool:
        return False


class AlwaysClosing:
    def __getitem__(self, name):
        return ClosesBehindYou()


@pytest.mark.parametrize("stream", [False, True])
def test_breaker_opening_after_admission_is_a_503_that_releases_the_hold(
    api, fakes, db_engine, stream
):
    from app.providers.breaker import get_breakers

    app.dependency_overrides[get_breakers] = lambda: AlwaysClosing()
    primary, fallback = fakes
    created = create_key(api)

    response = api.post(
        "/v1/chat/completions",
        json={
            "model": "fake-chat",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": stream,
        },
        headers={"Authorization": f"Bearer {created['key']}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "providers_unavailable"
    assert primary.requests == fallback.requests == []
    with Session(db_engine) as session:
        held = session.scalars(
            select(KeySpend).where(KeySpend.key_id == uuid.UUID(created["id"]))
        ).one()
    assert (held.spent_micros, held.reserved_micros) == (0, 0)
