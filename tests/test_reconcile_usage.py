import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import anyio
import pytest
import stripe
from sqlalchemy import Engine, pool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.billing.stripe_billing import StripeBilling
from app.jobs.reconcile_usage import reconcile, seconds_until_next_run
from app.models import Customer, UsageOutbox
from tests.fake_stripe import FakeStripe

DAY = date(2026, 9, 29)
NOON = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def make_customer(db_engine: Engine, billed: bool = True) -> Customer:
    with Session(db_engine, expire_on_commit=False) as session:
        customer = Customer(
            name="Reconcile test",
            stripe_customer_id=f"cus_{uuid.uuid4().hex[:14]}" if billed else None,
        )
        session.add(customer)
        session.commit()
        return customer


def record(
    db_engine: Engine, customer: Customer, micros: int, window: datetime, sent: bool = True
) -> None:
    with Session(db_engine) as session:
        session.add(
            UsageOutbox(
                customer_id=customer.id,
                cost_micros=micros,
                window_start=window,
                sent_at=datetime.now(UTC) if sent else None,
            )
        )
        session.commit()


def stripe_has(fake: FakeStripe, customer: Customer, micros: int, when: datetime) -> None:
    """Put a meter event straight into the fake Stripe."""
    fake.objects["meter_events"].append(
        SimpleNamespace(
            identifier=uuid.uuid4().hex,
            timestamp=int(when.timestamp()),
            payload={"stripe_customer_id": customer.stripe_customer_id, "value": str(micros)},
        )
    )


def run(db_engine: Engine, fake: FakeStripe, day: date = DAY):
    async def main():
        engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
        try:
            return await reconcile(
                StripeBilling(fake), day, async_sessionmaker(engine, expire_on_commit=False)
            )
        finally:
            await engine.dispose()

    return {check.customer_id: check for check in anyio.run(main)}


@pytest.fixture
def fake() -> FakeStripe:
    return FakeStripe()


def test_matching_usage_is_not_flagged(db_engine, fake, caplog):
    customer = make_customer(db_engine)
    record(db_engine, customer, 300, NOON)
    record(db_engine, customer, 200, NOON + timedelta(hours=3))
    stripe_has(fake, customer, 300, NOON)
    stripe_has(fake, customer, 200, NOON + timedelta(hours=3))

    with caplog.at_level(logging.WARNING, logger="app.jobs.reconcile_usage"):
        check = run(db_engine, fake)[customer.id]

    assert (check.recorded, check.sent, check.in_stripe) == (500, 500, 500)
    assert check.matches
    assert "mismatch" not in caplog.text


def test_usage_stripe_never_got_is_flagged(db_engine, fake, caplog):
    customer = make_customer(db_engine)
    record(db_engine, customer, 300, NOON)
    record(db_engine, customer, 45, NOON, sent=False)
    stripe_has(fake, customer, 300, NOON)

    with caplog.at_level(logging.WARNING, logger="app.jobs.reconcile_usage"):
        check = run(db_engine, fake)[customer.id]

    assert (check.recorded, check.sent, check.in_stripe) == (345, 300, 300)
    assert not check.matches
    assert "recorded 345 micros (300 sent), Stripe has 300 (difference -45)" in caplog.text


def test_usage_stripe_has_twice_is_flagged(db_engine, fake, caplog):
    customer = make_customer(db_engine)
    record(db_engine, customer, 300, NOON)
    stripe_has(fake, customer, 300, NOON)
    stripe_has(fake, customer, 300, NOON)  # Billed twice.

    with caplog.at_level(logging.WARNING, logger="app.jobs.reconcile_usage"):
        check = run(db_engine, fake)[customer.id]

    assert not check.matches
    assert "difference +300" in caplog.text


def test_only_the_utc_day_is_compared(db_engine, fake):
    customer = make_customer(db_engine)
    day_start = datetime(2026, 9, 29, tzinfo=UTC)
    for when in (day_start - timedelta(minutes=1), day_start + timedelta(days=1)):
        record(db_engine, customer, 999, when)
        stripe_has(fake, customer, 999, when)
    record(db_engine, customer, 10, day_start)
    stripe_has(fake, customer, 10, day_start)

    check = run(db_engine, fake)[customer.id]

    assert (check.recorded, check.in_stripe) == (10, 10)


def test_billed_customer_with_no_usage_is_still_checked(db_engine, fake):
    customer = make_customer(db_engine)
    stripe_has(fake, customer, 50, NOON)  # Stripe has usage Tollgate never recorded.

    check = run(db_engine, fake)[customer.id]

    assert (check.recorded, check.in_stripe) == (0, 50)
    assert not check.matches


def test_customers_without_stripe_are_skipped(db_engine, fake):
    customer = make_customer(db_engine, billed=False)
    record(db_engine, customer, 300, NOON)

    assert customer.id not in run(db_engine, fake)


def test_stripe_error_is_logged_and_does_not_stop_the_check(db_engine, fake, caplog):
    customer = make_customer(db_engine)
    record(db_engine, customer, 300, NOON)
    fake.fail_with = stripe.APIConnectionError("network down")

    with caplog.at_level(logging.ERROR, logger="app.jobs.reconcile_usage"):
        check = run(db_engine, fake)[customer.id]

    assert check.in_stripe is None
    assert "Couldn't read Stripe usage" in caplog.text


@pytest.mark.parametrize(
    ("now", "wait_s"),
    [
        (datetime(2026, 10, 1, 0, 10, tzinfo=UTC), 20 * 60),
        (datetime(2026, 10, 1, 0, 30, tzinfo=UTC), 24 * 3600),
        (datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 90 * 60),
    ],
)
def test_runs_daily_at_half_past_midnight_utc(now: datetime, wait_s: float):
    assert seconds_until_next_run(now) == wait_s
