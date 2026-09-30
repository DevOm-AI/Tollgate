import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone

import httpx2
import pytest
from sqlalchemy import Engine, pool, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from app.billing.budget import Outcome, current_period, reserve, settle
from app.billing.pricing import cost_micros, max_prompt_tokens
from app.main import app
from app.models import ApiKey, KeySpend, RequestLog, Reservation, UsageOutbox
from app.providers.base import ChatCompletionRequest, ProviderError, billable_output_tokens
from tests.conftest import FAKE_PRICE, MODEL, chat, create_key

# What the fake provider's answer costs: 3 prompt tokens and 4 output tokens.
FAKE_COST = cost_micros(3, 4, FAKE_PRICE["input_micros_per_1k"], FAKE_PRICE["output_micros_per_1k"])


def worst_case(content: str, max_tokens: int) -> int:
    request = ChatCompletionRequest.model_validate(
        {"model": MODEL, "messages": [{"role": "user", "content": content}]}
    )
    return cost_micros(
        max_prompt_tokens(request),
        max_tokens,
        FAKE_PRICE["input_micros_per_1k"],
        FAKE_PRICE["output_micros_per_1k"],
    )


def spend(db_engine: Engine, key_id: str) -> KeySpend | None:
    with Session(db_engine) as session:
        return session.scalars(
            select(KeySpend).where(KeySpend.key_id == uuid.UUID(key_id))
        ).one_or_none()


def rows(db_engine: Engine, model, **where) -> list:
    with Session(db_engine) as session:
        query = select(model)
        for column, value in where.items():
            query = query.where(getattr(model, column) == value)
        return list(session.scalars(query))


# --- Pricing ---


@pytest.mark.parametrize(
    ("tokens_in", "tokens_out", "price_in", "price_out", "expected"),
    [
        (1000, 0, 100, 0, 100),
        (3, 4, 1000, 2000, 11),
        (1, 0, 1, 0, 1),  # 0.001 micro-dollars still costs 1: fractions round up
        (0, 0, 100, 400, 0),
        (1500, 500, 100, 400, 350),
        # Far past float precision, still exact.
        (10**12, 10**12, 10**6 + 1, 10**6 + 3, 2 * 10**15 + 4 * 10**9),
    ],
)
def test_cost_is_whole_micro_dollars_rounded_up(
    tokens_in, tokens_out, price_in, price_out, expected
):
    assert cost_micros(tokens_in, tokens_out, price_in, price_out) == expected


def test_prompt_bound_counts_bytes_and_message_overhead():
    request = ChatCompletionRequest.model_validate(
        {
            "model": "m",
            "messages": [
                {"role": "system", "content": "Be brief"},
                {"role": "user", "content": [{"type": "text", "text": "héllo"}, {"text": None}]},
            ],
        }
    )

    # 8 + 8 overhead, "Be brief" is 8 bytes, "héllo" is 6 bytes (é takes 2).
    assert max_prompt_tokens(request) == 8 + 8 + 8 + 6


@pytest.mark.parametrize(
    ("now", "period"),
    [
        (datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC), "2026-10"),
        (datetime(2026, 11, 1, 0, 0, tzinfo=UTC), "2026-11"),
        # 01:00 on Nov 1 in India is still October in UTC.
        (datetime(2026, 11, 1, 1, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))), "2026-10"),
    ],
)
def test_period_is_the_calendar_month_in_utc(now: datetime, period: str):
    assert current_period(now) == period


# --- Reserve and settle through the endpoint ---


def test_answer_is_charged_its_actual_cost(api, provider, db_engine):
    created = create_key(api)

    response = chat(api, created["key"], max_tokens=50)

    assert response.status_code == 200
    row = spend(db_engine, created["id"])
    assert (row.spent_micros, row.reserved_micros) == (FAKE_COST, 0)
    assert row.period == current_period()


def test_settle_records_the_request_and_its_usage(api, provider, db_engine):
    created = create_key(api)

    chat(api, created["key"], max_tokens=50)

    key_id = uuid.UUID(created["id"])
    [request] = rows(db_engine, RequestLog, key_id=key_id)
    assert (request.model, request.provider, request.status) == (MODEL, "fake", "ok")
    assert (request.input_tokens, request.output_tokens) == (3, 4)
    assert request.cost_micros == FAKE_COST
    assert request.latency_ms >= 0
    [usage] = rows(db_engine, UsageOutbox, customer_id=uuid.UUID(created["customer_id"]))
    assert usage.cost_micros == FAKE_COST
    assert usage.sent_at is None
    assert usage.window_start.second == 0
    [reservation] = rows(db_engine, Reservation, key_id=key_id)
    assert reservation.amount_micros == worst_case("Hi", 50)
    assert reservation.settled_at is not None


def test_reservation_expires_in_ten_minutes(api, provider, db_engine):
    created = create_key(api)
    before = datetime.now(UTC)

    chat(api, created["key"])

    [reservation] = rows(db_engine, Reservation, key_id=uuid.UUID(created["id"]))
    expected = before + timedelta(minutes=10)
    assert expected <= reservation.expires_at <= expected + timedelta(seconds=30)


def test_request_that_could_exceed_the_budget_is_402(api, provider, db_engine):
    created = create_key(api, monthly_budget_micros=worst_case("Hi", 1024) - 1)

    response = chat(api, created["key"])

    assert response.status_code == 402
    error = response.json()["error"]
    assert (error["type"], error["code"]) == ("insufficient_quota", "budget_exceeded")
    assert provider.requests == []
    # The refused reservation rolled back entirely: nothing is held.
    assert spend(db_engine, created["id"]) is None


def test_402_still_reports_rate_limits_and_gives_tokens_back(api, provider):
    created = create_key(api, monthly_budget_micros=0, tpm_limit=10_000)

    response = chat(api, created["key"], max_tokens=100)

    assert response.status_code == 402
    assert int(response.headers["X-RateLimit-Remaining-Tokens"]) >= 9_999


def test_budget_that_exactly_covers_the_worst_case_is_enough(api, provider, db_engine):
    budget = worst_case("Hi", 50)
    created = create_key(api, monthly_budget_micros=budget)

    assert chat(api, created["key"], max_tokens=50).status_code == 200
    # Now FAKE_COST is spent, so the same worst case no longer fits.
    assert chat(api, created["key"], max_tokens=50).status_code == 402
    assert spend(db_engine, created["id"]).spent_micros == FAKE_COST


def test_budgets_are_per_key(api, provider):
    empty = create_key(api, monthly_budget_micros=0)["key"]
    funded = create_key(api)["key"]

    assert chat(api, empty).status_code == 402
    assert chat(api, funded).status_code == 200


def test_concurrent_requests_never_overspend(api, provider, db_engine):
    # Room for exactly 10 worst cases. Answers are slow, so all 30 requests hold or ask for
    # money at the same time; check-then-add would let far more than 10 through.
    held = worst_case("Hi", 20)
    created = create_key(api, monthly_budget_micros=10 * held)
    provider.delay_s = 0.5

    async def send_all() -> list[int]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/chat/completions",
                        json={
                            "model": MODEL,
                            "messages": [{"role": "user", "content": "Hi"}],
                            "max_tokens": 20,
                        },
                        headers={"Authorization": f"Bearer {created['key']}"},
                    )
                    for _ in range(30)
                )
            )
        return [response.status_code for response in responses]

    codes = asyncio.run(send_all())

    assert codes.count(200) == 10
    assert codes.count(402) == 20
    row = spend(db_engine, created["id"])
    assert row.spent_micros == 10 * FAKE_COST <= created["monthly_budget_micros"]
    assert row.reserved_micros == 0


def test_failed_provider_call_releases_the_hold_and_bills_nothing(api, provider, db_engine):
    created = create_key(api)
    provider.error = ProviderError("fake: HTTP 500", status_code=500)

    response = chat(api, created["key"])

    assert response.status_code == 502
    row = spend(db_engine, created["id"])
    assert (row.spent_micros, row.reserved_micros) == (0, 0)
    [request] = rows(db_engine, RequestLog, key_id=uuid.UUID(created["id"]))
    assert (request.status, request.cost_micros) == ("provider_error", 0)
    assert rows(db_engine, UsageOutbox, customer_id=uuid.UUID(created["customer_id"])) == []


def test_answer_costing_more_than_held_is_billed_what_was_held(api, provider, db_engine, caplog):
    created = create_key(api)
    # The provider ignores max_tokens=5 and answers with 1,000 tokens.
    provider.completion_tokens = 1_000

    with caplog.at_level(logging.WARNING, logger="app.billing.budget"):
        response = chat(api, created["key"], max_tokens=5)

    assert response.status_code == 200
    assert spend(db_engine, created["id"]).spent_micros == worst_case("Hi", 5)
    assert "billing the reserved amount" in caplog.text


def test_model_without_a_price_is_refused(api, provider):
    key = create_key(api)["key"]
    api.put("/admin/prices", json=FAKE_PRICE | {"model": "other-model"})

    response = chat(api, key, model="fake/unpriced-model")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_priced"
    assert provider.requests == []


def test_mock_model_is_priced_by_the_migrations(api):
    prices = api.get("/admin/prices").json()

    assert {"provider": "mock", "model": "mock"}.items() <= next(
        price for price in prices if price["provider"] == "mock"
    ).items()


# --- Settle directly ---


def test_settling_a_released_reservation_bills_nothing(api, provider, db_engine, caplog):
    created = create_key(api)
    key_id = uuid.UUID(created["id"])

    async def run() -> int:
        engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            key = await db.get(ApiKey, key_id)
            hold = await reserve(db, key, 500)
            # As if a sweeper released it: hold returned, reservation closed.
            await db.execute(
                update(Reservation)
                .where(Reservation.id == hold.reservation_id)
                .values(settled_at=datetime.now(UTC))
            )
            await db.execute(
                update(KeySpend)
                .where(KeySpend.key_id == key_id)
                .values(reserved_micros=KeySpend.reserved_micros - 500)
            )
            await db.commit()
            charged = await settle(db, hold, 400, Outcome(model="m", provider="fake", status="ok"))
        await engine.dispose()
        return charged

    with caplog.at_level(logging.WARNING, logger="app.billing.budget"):
        assert asyncio.run(run()) == 0

    row = spend(db_engine, created["id"])
    assert (row.spent_micros, row.reserved_micros) == (0, 0)
    assert "already released" in caplog.text


# --- Price admin ---


def test_price_can_be_set_and_changed(api):
    price = {
        "provider": "groq",
        "model": f"model-{uuid.uuid4().hex}",
        "input_micros_per_1k": 50,
        "output_micros_per_1k": 80,
    }

    assert api.put("/admin/prices", json=price).status_code == 200
    changed = api.put("/admin/prices", json=price | {"output_micros_per_1k": 90}).json()

    assert changed["output_micros_per_1k"] == 90
    listed = [p for p in api.get("/admin/prices").json() if p["model"] == price["model"]]
    assert listed == [price | {"output_micros_per_1k": 90}]


@pytest.mark.parametrize(
    "change", [{"input_micros_per_1k": -1}, {"provider": ""}, {"output_micros_per_1k": 1.5}]
)
def test_invalid_price_is_422(api, change):
    assert api.put("/admin/prices", json=FAKE_PRICE | change).status_code == 422


@pytest.mark.parametrize(("method", "path"), [("put", "/admin/prices"), ("get", "/admin/prices")])
def test_price_routes_need_the_admin_key(api, method, path):
    response = api.request(method, path, headers={"Authorization": "Bearer wrong"}, json={})

    assert response.status_code == 401


# --- Thinking tokens ---


@pytest.mark.parametrize(
    ("prompt", "completion", "total", "billed"),
    [
        (76, 40, 116, 40),  # Groq/OpenAI: reasoning is inside completion_tokens.
        (6, 14, 135, 129),  # Gemini: 115 thinking tokens only show in total_tokens.
        (6, 0, 43, 37),  # Gemini that spent everything thinking: still billed.
        (5, 3, None, 3),  # No total reported: completion_tokens is all there is.
    ],
)
def test_billable_output_includes_thinking_tokens(prompt, completion, total, billed):
    assert billable_output_tokens(prompt, completion, total) == billed


def test_hidden_thinking_tokens_are_billed(api, provider, db_engine):
    created = create_key(api)
    # Like Gemini: 4 visible output tokens, 50 more thinking, only in total_tokens.
    original = provider.complete

    async def thinking(request):
        completion = await original(request)
        completion.usage.total_tokens += 50
        return completion

    provider.complete = thinking

    chat(api, created["key"], max_tokens=100)

    [request] = rows(db_engine, RequestLog, key_id=uuid.UUID(created["id"]))
    assert request.output_tokens == 54
    assert request.cost_micros == cost_micros(
        3, 54, FAKE_PRICE["input_micros_per_1k"], FAKE_PRICE["output_micros_per_1k"]
    )
