import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.billing.budget import current_period
from app.models import RequestLog
from app.providers.base import ProviderError
from tests.conftest import chat, create_customer, create_key


def log_request(db_engine: Engine, key_id: str, **values) -> None:
    row = {
        "key_id": uuid.UUID(key_id),
        "model": "fast-chat",
        "provider": "groq",
        "status": "ok",
        "input_tokens": 10,
        "output_tokens": 20,
        "cost_micros": 5,
        "latency_ms": 100,
    } | values
    with Session(db_engine) as session:
        session.add(RequestLog(**row))
        session.commit()


# --- Keys ---


def test_keys_list_shows_limits_and_this_months_spend(api, provider):
    created = create_key(api, monthly_budget_micros=500_000)
    chat(api, created["key"])

    [key] = [k for k in api.get("/admin/keys").json() if k["id"] == created["id"]]

    assert key["prefix"] == created["prefix"]
    assert key["customer_name"] == "Acme"
    assert key["monthly_budget_micros"] == 500_000
    assert key["period"] == current_period()
    assert key["spent_micros"] > 0
    assert key["reserved_micros"] == 0
    assert "key" not in key and "key_hash" not in key


def test_key_with_no_usage_this_month_shows_zero_spend(api):
    created = create_key(api)

    [key] = [k for k in api.get("/admin/keys").json() if k["id"] == created["id"]]

    assert (key["spent_micros"], key["reserved_micros"]) == (0, 0)


def test_customers_list(api):
    customer = create_customer(api, name=f"Customer {uuid.uuid4().hex[:6]}")

    names = [c["name"] for c in api.get("/admin/customers").json()]

    assert customer["name"] in names


# --- Stats ---


def test_stats_has_a_row_for_every_day_in_the_window(api):
    created = create_key(api)

    stats = api.get(f"/admin/keys/{created['id']}/stats?days=7").json()

    assert stats["days"] == 7
    assert len(stats["daily"]) == 7
    assert stats["daily"][-1]["day"] == datetime.now(UTC).date().isoformat()
    assert all(day["requests"] == 0 for day in stats["daily"])
    assert stats["latency_p50_ms"] is None
    assert stats["providers"] == []


def test_stats_count_requests_tokens_and_cost_per_day(api, db_engine):
    created = create_key(api)
    now = datetime.now(UTC)
    log_request(db_engine, created["id"], created_at=now)
    log_request(db_engine, created["id"], created_at=now, status="timeout", cost_micros=0)
    log_request(db_engine, created["id"], created_at=now - timedelta(days=2))
    log_request(db_engine, created["id"], created_at=now - timedelta(days=40))  # Outside.

    daily = api.get(f"/admin/keys/{created['id']}/stats?days=7").json()["daily"]

    today, two_days_ago = daily[-1], daily[-3]
    assert (today["requests"], today["errors"]) == (2, 1)
    assert (today["input_tokens"], today["output_tokens"], today["cost_micros"]) == (20, 40, 5)
    assert two_days_ago["requests"] == 1
    assert sum(day["requests"] for day in daily) == 3


def test_stats_latency_percentiles_over_answered_requests(api, db_engine):
    created = create_key(api)
    for latency in range(1, 101):  # 1..100 ms
        log_request(db_engine, created["id"], latency_ms=latency)
    log_request(db_engine, created["id"], latency_ms=99_999, status="timeout")

    stats = api.get(f"/admin/keys/{created['id']}/stats").json()

    assert stats["latency_p50_ms"] == pytest.approx(50.5)
    assert stats["latency_p95_ms"] == pytest.approx(95.05)


def test_stats_error_rate_by_provider(api, db_engine):
    created = create_key(api)
    for status in ("ok", "ok", "ok", "provider_error"):
        log_request(db_engine, created["id"], provider="groq", status=status)
    for status in ("ok", "cancelled"):
        log_request(db_engine, created["id"], provider="gemini", status=status)

    providers = {
        p["provider"]: p for p in api.get(f"/admin/keys/{created['id']}/stats").json()["providers"]
    }

    assert (providers["groq"]["requests"], providers["groq"]["errors"]) == (4, 1)
    assert providers["groq"]["error_rate"] == 0.25
    # A client hanging up isn't the provider's error.
    assert providers["gemini"]["errors"] == 0


@pytest.mark.parametrize("days", [0, 91])
def test_stats_window_is_limited(api, days):
    created = create_key(api)

    assert api.get(f"/admin/keys/{created['id']}/stats?days={days}").status_code == 422


def test_stats_for_unknown_key_is_404(api):
    assert api.get(f"/admin/keys/{uuid.uuid4()}/stats").status_code == 404


# --- Request log ---


def test_request_log_is_newest_first_and_metadata_only(api, provider):
    created = create_key(api)
    chat(api, created["key"], messages=[{"role": "user", "content": "a secret prompt"}])
    provider.error = ProviderError("fake: HTTP 500", status_code=500)
    chat(api, created["key"])

    log = api.get(f"/admin/keys/{created['id']}/requests").json()

    assert [entry["status"] for entry in log] == ["provider_error", "ok"]
    assert set(log[0]) == {
        "id",
        "model",
        "provider",
        "status",
        "input_tokens",
        "output_tokens",
        "cost_micros",
        "latency_ms",
        "created_at",
    }
    assert "secret" not in str(log)


def test_request_log_pages_back(api, db_engine):
    created = create_key(api)
    for _ in range(5):
        log_request(db_engine, created["id"])

    first = api.get(f"/admin/keys/{created['id']}/requests?limit=2").json()
    second = api.get(
        f"/admin/keys/{created['id']}/requests?limit=2&before_id={first[-1]['id']}"
    ).json()

    assert len(first) == len(second) == 2
    assert first[-1]["id"] > second[0]["id"]


def test_request_log_for_unknown_key_is_404(api):
    assert api.get(f"/admin/keys/{uuid.uuid4()}/requests").status_code == 404


# --- Access ---


@pytest.mark.parametrize(
    "path", ["/admin/keys", "/admin/customers", f"/admin/keys/{uuid.uuid4()}/stats"]
)
def test_dashboard_routes_need_the_admin_key(api, path):
    assert api.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_dashboard_origin_may_call_the_api_and_read_the_limits(api):
    response = api.options(
        "/v1/chat/completions",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_other_origins_are_not_allowed(api):
    response = api.get("/admin/keys", headers={"Origin": "https://evil.example"})

    assert "access-control-allow-origin" not in response.headers
