import uuid
from datetime import UTC, datetime, timedelta

import anyio
import pytest
import stripe
from sqlalchemy import Engine, pool, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.billing.stripe_billing import METER_EVENT_NAME, StripeBilling
from app.jobs.push_usage import Group, event_identifier, push_usage
from app.models import Customer, UsageOutbox
from tests.fake_stripe import FakeStripe


def minute(minutes_ago: int) -> datetime:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return now - timedelta(minutes=minutes_ago)


def make_customer(db_engine: Engine, stripe_customer_id: str | None = "auto") -> Customer:
    if stripe_customer_id == "auto":
        stripe_customer_id = f"cus_{uuid.uuid4().hex[:14]}"
    with Session(db_engine, expire_on_commit=False) as session:
        customer = Customer(name="Outbox test", stripe_customer_id=stripe_customer_id)
        session.add(customer)
        session.commit()
        return customer


def add_usage(db_engine: Engine, customer: Customer, micros: int, window: datetime) -> None:
    with Session(db_engine) as session:
        session.add(UsageOutbox(customer_id=customer.id, cost_micros=micros, window_start=window))
        session.commit()


def unsent(db_engine: Engine, customer: Customer) -> int:
    with Session(db_engine) as session:
        return len(
            session.scalars(
                select(UsageOutbox).where(
                    UsageOutbox.customer_id == customer.id, UsageOutbox.sent_at.is_(None)
                )
            ).all()
        )


def events_for(fake: FakeStripe, customer: Customer) -> list:
    return [
        event
        for event in fake.objects["meter_events"]
        if event.payload["stripe_customer_id"] == customer.stripe_customer_id
    ]


def run_push(db_engine: Engine, fake: FakeStripe, times: int = 1, concurrently: bool = False):
    async def main():
        engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        billing = StripeBilling(fake)
        try:
            if concurrently:
                async with anyio.create_task_group() as group:
                    for _ in range(times):
                        group.start_soon(push_usage, billing, sessions)
            else:
                for _ in range(times):
                    await push_usage(billing, sessions)
        finally:
            await engine.dispose()

    anyio.run(main)


@pytest.fixture
def fake() -> FakeStripe:
    return FakeStripe()


def test_one_event_per_customer_and_minute_with_the_summed_usage(db_engine, fake):
    customer = make_customer(db_engine)
    add_usage(db_engine, customer, 100, minute(5))
    add_usage(db_engine, customer, 250, minute(5))
    add_usage(db_engine, customer, 7, minute(3))

    run_push(db_engine, fake)

    events = sorted(events_for(fake, customer), key=lambda e: e.timestamp)
    assert [e.payload["value"] for e in events] == ["350", "7"]
    assert [e.timestamp for e in events] == [
        int(minute(5).timestamp()),
        int(minute(3).timestamp()),
    ]
    assert all(e.event_name == METER_EVENT_NAME for e in events)
    assert unsent(db_engine, customer) == 0


def test_identifier_is_customer_and_minute(db_engine, fake):
    customer = make_customer(db_engine)
    add_usage(db_engine, customer, 100, minute(5))

    run_push(db_engine, fake)

    [event] = events_for(fake, customer)
    assert event.identifier == f"tg-{customer.id}-{minute(5):%Y%m%dT%H%M}"


def test_recent_minutes_wait_for_late_rows(db_engine, fake):
    customer = make_customer(db_engine)
    add_usage(db_engine, customer, 100, minute(0))
    add_usage(db_engine, customer, 100, minute(1))

    run_push(db_engine, fake)

    assert events_for(fake, customer) == []
    assert unsent(db_engine, customer) == 2


def test_customers_without_stripe_billing_are_left_in_the_outbox(db_engine, fake):
    customer = make_customer(db_engine, stripe_customer_id=None)
    add_usage(db_engine, customer, 100, minute(5))

    run_push(db_engine, fake)

    assert unsent(db_engine, customer) == 1


def test_stripe_failing_leaves_rows_unsent_for_the_next_run(db_engine, fake):
    customer = make_customer(db_engine)
    add_usage(db_engine, customer, 100, minute(5))
    fake.fail_with = stripe.APIConnectionError("network down")

    run_push(db_engine, fake)
    assert unsent(db_engine, customer) == 1

    fake.fail_with = None
    run_push(db_engine, fake)

    assert unsent(db_engine, customer) == 0
    assert [e.payload["value"] for e in events_for(fake, customer)] == ["100"]


def test_crash_after_sending_before_marking_does_not_bill_twice(db_engine, fake):
    customer = make_customer(db_engine)
    add_usage(db_engine, customer, 100, minute(5))
    run_push(db_engine, fake)
    # As if Tollgate crashed after Stripe said OK, before the rows were marked sent.
    with Session(db_engine) as session:
        session.execute(
            update(UsageOutbox).where(UsageOutbox.customer_id == customer.id).values(sent_at=None)
        )
        session.commit()

    run_push(db_engine, fake)

    creates = [c for c in fake.calls if c[0] == "meter_events" and c[1] == "create"]
    assert len(creates) == 2  # Sent again...
    assert len(events_for(fake, customer)) == 1  # ...and Stripe dropped the duplicate.
    assert unsent(db_engine, customer) == 0


def test_concurrent_runs_send_each_group_once(db_engine, fake):
    customer = make_customer(db_engine)
    for minutes_ago in range(2, 12):
        add_usage(db_engine, customer, 10, minute(minutes_ago))

    run_push(db_engine, fake, times=3, concurrently=True)

    creates = [
        c
        for c in fake.calls
        if c[0] == "meter_events"
        and c[1] == "create"
        and c[2]["payload"]["stripe_customer_id"] == customer.stripe_customer_id
    ]
    assert len(creates) == 10
    assert unsent(db_engine, customer) == 0


def test_rows_arriving_after_their_minute_was_sent_get_their_own_event(db_engine, fake):
    customer = make_customer(db_engine)
    add_usage(db_engine, customer, 100, minute(5))
    run_push(db_engine, fake)

    add_usage(db_engine, customer, 30, minute(5))  # Late, somehow.
    run_push(db_engine, fake)

    values = sorted(e.payload["value"] for e in events_for(fake, customer))
    assert values == ["100", "30"]
    assert unsent(db_engine, customer) == 0


def test_late_identifier_is_stable_for_retries():
    group = Group(uuid.uuid4(), minute(5), "cus_x")

    assert event_identifier(group, 42) == event_identifier(group, 42)
    assert event_identifier(group, 42) != event_identifier(group)
