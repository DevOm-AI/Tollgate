import json
import time
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.billing.pricing import cost_micros
from app.core.config import Settings, get_settings
from app.main import app
from app.models import KeySpend, RequestLog
from app.providers.base import ProviderError, ProviderTimeout
from app.providers.catalog import Catalog, get_catalog
from app.providers.mock import MockProvider
from tests.conftest import ADMIN_KEY, FAKE_PRICE, STREAM_WORDS, FakeProvider, chat, create_key

ROUTE = "fake-chat"
# The fallback costs twice as much, so a bill shows which provider answered.
FALLBACK_PRICE = FAKE_PRICE | {
    "provider": "fake2",
    "input_micros_per_1k": 2 * FAKE_PRICE["input_micros_per_1k"],
    "output_micros_per_1k": 2 * FAKE_PRICE["output_micros_per_1k"],
}


@pytest.fixture
def fakes(api):
    """A route fake-chat: primary "fake", then fallback "fake2"."""
    primary, fallback = FakeProvider(), FakeProvider()
    fallback.name = "fake2"
    for price in (FAKE_PRICE, FALLBACK_PRICE):
        assert api.put("/admin/prices", json=price).status_code == 200
    app.dependency_overrides[get_catalog] = lambda: Catalog(
        [primary, fallback], {ROUTE: ["fake/test-model", "fake2/test-model"]}
    )
    return primary, fallback


def logged(db_engine: Engine, key_id: str) -> RequestLog:
    with Session(db_engine) as session:
        return session.scalars(
            select(RequestLog).where(RequestLog.key_id == uuid.UUID(key_id))
        ).one()


def spend(db_engine: Engine, key_id: str) -> tuple[int, int]:
    with Session(db_engine) as session:
        row = session.scalars(select(KeySpend).where(KeySpend.key_id == uuid.UUID(key_id))).one()
        return row.spent_micros, row.reserved_micros


def stream(api, key: str):
    return api.post(
        "/v1/chat/completions",
        json={"model": ROUTE, "messages": [{"role": "user", "content": "Hi"}], "stream": True},
        headers={"Authorization": f"Bearer {key}"},
    )


def events(response) -> list[str]:
    return [line.removeprefix("data: ") for line in response.text.split("\n\n") if line]


# --- When to fall back ---


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("fake: HTTP 500", status_code=500),
        ProviderError("fake: HTTP 503", status_code=503),
        ProviderError("fake: HTTP 429", status_code=429),
        ProviderError("fake: ConnectError"),
        ProviderTimeout("fake: ConnectTimeout"),
    ],
)
def test_retryable_failure_falls_back_to_the_next_provider(api, fakes, db_engine, error):
    primary, fallback = fakes
    created = create_key(api)
    primary.error = error

    response = chat(api, created["key"], model=ROUTE)

    assert response.status_code == 200
    assert len(primary.requests) == len(fallback.requests) == 1
    request = logged(db_engine, created["id"])
    assert (request.status, request.provider, request.model) == ("ok", "fake2", ROUTE)


@pytest.mark.parametrize("status_code", [400, 404, 422])
def test_request_the_provider_rejects_does_not_fall_back(api, fakes, status_code):
    primary, fallback = fakes
    key = create_key(api)["key"]
    primary.error = ProviderError("fake: HTTP", status_code=status_code, upstream_message="bad")

    response = chat(api, key, model=ROUTE)

    assert response.status_code == 400
    assert fallback.requests == []


def test_every_provider_failing_returns_the_last_error_and_bills_nothing(api, fakes, db_engine):
    primary, fallback = fakes
    created = create_key(api)
    primary.error = ProviderError("fake: HTTP 500", status_code=500)
    fallback.error = ProviderTimeout("fake2: ReadTimeout")

    response = chat(api, created["key"], model=ROUTE)

    assert response.status_code == 504
    assert spend(db_engine, created["id"]) == (0, 0)
    request = logged(db_engine, created["id"])
    assert (request.status, request.provider) == ("timeout", "fake2")


def test_primary_timing_out_falls_back_with_a_fresh_timeout(api, fakes):
    primary, fallback = fakes
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_key=ADMIN_KEY, provider_total_timeout_s=0.5
    )
    key = create_key(api)["key"]
    primary.delay_s = 5
    fallback.delay_s = 0.3  # Would miss a deadline shared with the primary.

    started = time.monotonic()
    response = chat(api, key, model=ROUTE)

    assert response.status_code == 200
    assert time.monotonic() - started < 2


def test_answer_is_billed_at_the_price_of_the_provider_that_answered(api, fakes, db_engine):
    primary, _ = fakes
    created = create_key(api)
    primary.error = ProviderError("fake: HTTP 502", status_code=502)

    chat(api, created["key"], model=ROUTE, max_tokens=50)

    expected = cost_micros(
        3, 4, FALLBACK_PRICE["input_micros_per_1k"], FALLBACK_PRICE["output_micros_per_1k"]
    )
    assert spend(db_engine, created["id"]) == (expected, 0)


def test_one_request_counts_once_against_the_rate_limit(api, fakes):
    primary, _ = fakes
    key = create_key(api, rpm_limit=5)["key"]
    primary.error = ProviderError("fake: HTTP 500", status_code=500)

    response = chat(api, key, model=ROUTE)

    assert response.headers["X-RateLimit-Remaining-Requests"] == "4"


def test_unpriced_provider_in_a_route_is_skipped(api, db_engine):
    unpriced, priced = FakeProvider(), FakeProvider()
    unpriced.name, priced.name = "unpriced", "fake2"
    assert api.put("/admin/prices", json=FALLBACK_PRICE).status_code == 200
    app.dependency_overrides[get_catalog] = lambda: Catalog(
        [unpriced, priced], {ROUTE: ["unpriced/m", "fake2/test-model"]}
    )
    created = create_key(api)

    assert chat(api, created["key"], model=ROUTE).status_code == 200
    assert unpriced.requests == []
    assert logged(db_engine, created["id"]).provider == "fake2"


# --- Streams: only before the first output ---


def test_stream_falls_back_when_the_primary_fails_before_any_output(api, fakes, db_engine):
    primary, fallback = fakes
    created = create_key(api)
    primary.error = ProviderError("fake: HTTP 503", status_code=503)

    response = stream(api, created["key"])

    assert response.status_code == 200
    data = events(response)
    assert data[-1] == "[DONE]"
    text = "".join(
        choice["delta"].get("content", "")
        for event in data[:-1]
        for choice in json.loads(event)["choices"]
    )
    assert text == "".join(STREAM_WORDS)
    assert logged(db_engine, created["id"]).provider == "fake2"


def test_stream_falls_back_when_the_primary_sends_only_a_role_chunk_then_stalls(api, fakes):
    primary, fallback = fakes
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, admin_api_key=ADMIN_KEY, provider_first_token_timeout_s=0.3
    )
    key = create_key(api)["key"]
    primary.role_chunk_then_pause_s = 5

    response = stream(api, key)

    assert response.status_code == 200
    # The primary's role-only chunk never reached the client: one opening, from fake2.
    roles = [
        choice["delta"].get("role")
        for event in events(response)[:-1]
        for choice in json.loads(event)["choices"]
    ]
    assert roles.count("assistant") == 1
    assert len(fallback.requests) == 1


def test_stream_never_switches_provider_after_output_was_sent(api, fakes, db_engine):
    primary, fallback = fakes
    created = create_key(api)
    primary.break_before_chunk = 2

    response = stream(api, created["key"])

    assert response.status_code == 200
    assert json.loads(events(response)[-1])["error"]["code"] == "provider_error"
    assert fallback.requests == []
    assert logged(db_engine, created["id"]).provider == "fake"


# --- The mock set to fail every time ---


def test_mock_failing_every_request_is_covered_by_the_fallback(api, db_engine):
    fallback = FakeProvider()
    assert api.put("/admin/prices", json=FAKE_PRICE).status_code == 200
    app.dependency_overrides[get_catalog] = lambda: Catalog(
        [MockProvider(error_rate=1.0), fallback], {"demo-chat": ["mock", "fake/test-model"]}
    )
    created = create_key(api)

    codes = [chat(api, created["key"], model="demo-chat").status_code for _ in range(5)]

    assert codes == [200] * 5
    assert len(fallback.requests) == 5


# --- Settings ---


def test_route_that_could_outlast_a_reservation_is_refused():
    with pytest.raises(ValidationError, match="outlast"):
        Settings(
            _env_file=None,
            provider_total_timeout_s=120,
            routes={"long": ["mock"] + [f"groq/m{i}" for i in range(4)]},
        )
