import uuid

import pytest
import stripe
from sqlalchemy.orm import Session

from app.billing import stripe_billing
from app.billing.stripe_billing import (
    METER_EVENT_NAME,
    PRICE_LOOKUP_KEY,
    StripeBilling,
    get_stripe_billing,
)
from app.core.config import Settings
from app.main import app
from app.models import Customer
from tests.conftest import create_customer
from tests.fake_stripe import FakeStripe


@pytest.fixture
def fake() -> FakeStripe:
    return FakeStripe()


@pytest.fixture
def billing(fake) -> StripeBilling:
    return StripeBilling(fake)


# --- Meter and price ---


def test_meter_sums_micro_dollars_per_stripe_customer(billing, fake):
    billing.ensure_meter()

    [meter] = fake.created("meters")
    assert meter["event_name"] == METER_EVENT_NAME
    assert meter["default_aggregation"] == {"formula": "sum"}
    assert meter["customer_mapping"] == {"type": "by_id", "event_payload_key": "stripe_customer_id"}
    assert meter["value_settings"] == {"event_payload_key": "value"}


def test_price_bills_one_micro_dollar_per_unit_monthly_on_the_meter(billing, fake):
    price_id = billing.ensure_price()

    [meter] = fake.objects["meters"]
    [price] = fake.created("prices")
    assert price_id.startswith("price_")
    assert price["currency"] == "usd"
    # $0.000001 = 0.0001 cents: an invoice equals the metered micro-dollars, to the cent.
    assert price["unit_amount_decimal"] == "0.0001"
    assert price["recurring"] == {"interval": "month", "usage_type": "metered", "meter": meter.id}
    assert price["lookup_key"] == PRICE_LOOKUP_KEY


def test_setup_is_find_or_create(billing, fake):
    first = (billing.ensure_meter(), billing.ensure_price())
    second = (billing.ensure_meter(), billing.ensure_price())

    assert first == second
    assert len(fake.objects["meters"]) == len(fake.objects["prices"]) == 1


# --- Customers ---


def test_customer_gets_a_stripe_customer_and_an_invoiced_subscription(billing, fake):
    customer_id = uuid.uuid4()

    result = billing.set_up_customer(customer_id, "Acme", None)

    [customer] = fake.created("customers")
    assert customer["metadata"] == {"tollgate_customer_id": str(customer_id)}
    [subscription] = fake.created("subscriptions")
    assert subscription["customer"] == result.stripe_customer_id
    assert subscription["items"] == [{"price": result.price_id}]
    # Charged automatically (Stripe's default): no customer email needed to subscribe.
    assert "collection_method" not in subscription


def test_existing_stripe_customer_is_reused(billing, fake):
    result = billing.set_up_customer(uuid.uuid4(), "Acme", "cus_existing")

    assert result.stripe_customer_id == "cus_existing"
    assert fake.created("customers") == []


def test_customer_setup_twice_changes_nothing(billing, fake):
    customer_id = uuid.uuid4()

    first = billing.set_up_customer(customer_id, "Acme", None)
    second = billing.set_up_customer(customer_id, "Acme", first.stripe_customer_id)

    assert first == second
    assert len(fake.objects["customers"]) == len(fake.objects["subscriptions"]) == 1


def test_stripe_calls_carry_idempotency_keys(billing, fake):
    billing.set_up_customer(uuid.uuid4(), "Acme", None)

    for kind, action, _, options in fake.calls:
        if action == "create":
            assert options.get("idempotency_key"), kind


def test_no_stripe_key_means_no_billing(monkeypatch):
    no_key = Settings(_env_file=None, stripe_secret_key=None)
    monkeypatch.setattr(stripe_billing, "get_settings", lambda: no_key)
    get_stripe_billing.cache_clear()
    try:
        assert get_stripe_billing() is None
    finally:
        get_stripe_billing.cache_clear()


# --- Admin route ---


@pytest.fixture
def api_with_stripe(api, fake):
    app.dependency_overrides[get_stripe_billing] = lambda: StripeBilling(fake)
    return api


def test_billing_route_sets_up_and_stores_the_stripe_customer(api_with_stripe, fake, db_engine):
    customer = create_customer(api_with_stripe)

    response = api_with_stripe.post(f"/admin/customers/{customer['id']}/billing")

    assert response.status_code == 200
    body = response.json()
    assert body["stripe_customer_id"].startswith("cus_")
    assert body["subscription_id"].startswith("sub_")
    with Session(db_engine) as session:
        stored = session.get(Customer, uuid.UUID(customer["id"]))
    assert stored.stripe_customer_id == body["stripe_customer_id"]


def test_billing_route_twice_returns_the_same_setup(api_with_stripe, fake):
    customer = create_customer(api_with_stripe)
    path = f"/admin/customers/{customer['id']}/billing"

    assert api_with_stripe.post(path).json() == api_with_stripe.post(path).json()
    assert len(fake.objects["customers"]) == 1


def test_billing_route_for_unknown_customer_is_404(api_with_stripe):
    assert api_with_stripe.post(f"/admin/customers/{uuid.uuid4()}/billing").status_code == 404


def test_billing_route_without_stripe_is_503(api):
    app.dependency_overrides[get_stripe_billing] = lambda: None
    customer = create_customer(api)

    assert api.post(f"/admin/customers/{customer['id']}/billing").status_code == 503


def test_stripe_refusing_is_a_502(api_with_stripe, fake):
    customer = create_customer(api_with_stripe)
    fake.fail_with = stripe.AuthenticationError("Invalid API Key provided")

    response = api_with_stripe.post(f"/admin/customers/{customer['id']}/billing")

    assert response.status_code == 502


def test_billing_route_needs_the_admin_key(api):
    response = api.post(
        f"/admin/customers/{uuid.uuid4()}/billing", headers={"Authorization": "Bearer wrong"}
    )

    assert response.status_code == 401
