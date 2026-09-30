import logging
from datetime import UTC, datetime, time

import anyio
import pytest
from sqlalchemy import Engine, pool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.billing.budget import current_period
from app.core.security import generate_api_key
from app.jobs.reset_demo import reset_demo_spend
from app.jobs.schedule import seconds_until_daily
from app.models import ApiKey, Customer, KeySpend


def make_key(db_engine: Engine, spent: int, reserved: int = 0) -> tuple[str, ApiKey]:
    new_key = generate_api_key()
    with Session(db_engine, expire_on_commit=False) as session:
        customer = Customer(name="Demo reset test")
        session.add(customer)
        session.flush()
        key = ApiKey(
            customer_id=customer.id,
            key_hash=new_key.key_hash,
            prefix=new_key.prefix,
            rpm_limit=5,
            tpm_limit=20_000,
            monthly_budget_micros=100_000,
        )
        session.add(key)
        session.flush()
        session.add(
            KeySpend(
                key_id=key.id,
                period=current_period(),
                spent_micros=spent,
                reserved_micros=reserved,
            )
        )
        session.add(KeySpend(key_id=key.id, period="2020-01", spent_micros=spent))
        session.commit()
        return new_key.key, key


def spend(db_engine: Engine, key: ApiKey, period: str | None = None) -> tuple[int, int]:
    with Session(db_engine) as session:
        row = session.get(KeySpend, (key.id, period or current_period()))
        return row.spent_micros, row.reserved_micros


def reset(db_engine: Engine, demo_key: str) -> bool:
    async def main():
        engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
        try:
            return await reset_demo_spend(
                demo_key, async_sessionmaker(engine, expire_on_commit=False)
            )
        finally:
            await engine.dispose()

    return anyio.run(main)


def test_reset_gives_the_demo_key_its_budget_back(db_engine):
    demo_key, key = make_key(db_engine, spent=100_000)

    assert reset(db_engine, demo_key)

    assert spend(db_engine, key) == (0, 0)


def test_money_held_by_requests_in_flight_stays_held(db_engine):
    demo_key, key = make_key(db_engine, spent=90_000, reserved=2_058)

    reset(db_engine, demo_key)

    assert spend(db_engine, key) == (0, 2_058)


def test_only_the_demo_key_and_only_this_month_are_reset(db_engine):
    demo_key, key = make_key(db_engine, spent=100_000)
    _, other = make_key(db_engine, spent=50_000)

    reset(db_engine, demo_key)

    assert spend(db_engine, other) == (50_000, 0)
    assert spend(db_engine, key, "2020-01") == (100_000, 0)


def test_unknown_demo_key_is_logged_and_nothing_changes(db_engine, caplog):
    _, other = make_key(db_engine, spent=50_000)

    with caplog.at_level(logging.WARNING, logger="app.jobs.reset_demo"):
        assert not reset(db_engine, generate_api_key().key)

    assert "doesn't match any key" in caplog.text
    assert spend(db_engine, other) == (50_000, 0)


@pytest.mark.parametrize(
    ("now", "wait_s"),
    [
        (datetime(2026, 10, 1, 23, 0, tzinfo=UTC), 3600),
        (datetime(2026, 10, 1, 0, 0, tzinfo=UTC), 24 * 3600),
        (datetime(2026, 10, 1, 0, 0, 30, tzinfo=UTC), 24 * 3600 - 30),
    ],
)
def test_daily_schedule(now: datetime, wait_s: float):
    assert seconds_until_daily(time(0, 0, tzinfo=UTC), now) == wait_s
