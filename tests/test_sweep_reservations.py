import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, pool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from app.billing.budget import current_period, reserve
from app.jobs import sweep_reservations
from app.jobs.sweep_reservations import release_expired, sweep
from app.models import ApiKey, Customer, KeySpend, Reservation

PERIOD = current_period()


def make_key(db_engine: Engine, *, spent: int = 0) -> uuid.UUID:
    with Session(db_engine) as session:
        customer = Customer(name="Sweep test")
        session.add(customer)
        session.flush()
        key = ApiKey(
            customer_id=customer.id,
            key_hash=uuid.uuid4().hex * 2,
            prefix="ab12cd34",
            rpm_limit=60,
            tpm_limit=100_000,
            monthly_budget_micros=1_000_000,
        )
        session.add(key)
        session.flush()
        session.add(KeySpend(key_id=key.id, period=PERIOD, spent_micros=spent))
        session.commit()
        return key.id


def hold(
    db_engine: Engine,
    key_id: uuid.UUID,
    amount: int,
    *,
    expires_in: timedelta,
    settled: bool = False,
    period: str = PERIOD,
) -> uuid.UUID:
    """A reservation, with its amount counted in key_spend.reserved_micros unless settled."""
    with Session(db_engine) as session:
        if session.get(KeySpend, (key_id, period)) is None:
            session.add(KeySpend(key_id=key_id, period=period))
            session.flush()
        reservation = Reservation(
            key_id=key_id,
            period=period,
            amount_micros=amount,
            expires_at=datetime.now(UTC) + expires_in,
            settled_at=datetime.now(UTC) if settled else None,
        )
        session.add(reservation)
        if not settled:
            session.get(KeySpend, (key_id, period)).reserved_micros += amount
        session.commit()
        return reservation.id


def spend(db_engine: Engine, key_id: uuid.UUID, period: str = PERIOD) -> tuple[int, int]:
    with Session(db_engine) as session:
        row = session.get(KeySpend, (key_id, period))
        return row.spent_micros, row.reserved_micros


def is_open(db_engine: Engine, reservation_id: uuid.UUID) -> bool:
    with Session(db_engine) as session:
        return session.get(Reservation, reservation_id).settled_at is None


def run(db_engine: Engine, job):
    """Run `job(sessions)` against the scratch database."""

    async def main():
        engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
        try:
            return await job(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(main())


EXPIRED = timedelta(minutes=-1)
OPEN = timedelta(minutes=10)


def test_expired_reservation_is_released_and_billed_nothing(db_engine):
    key_id = make_key(db_engine, spent=250)
    reservation_id = hold(db_engine, key_id, 400, expires_in=EXPIRED)

    run(db_engine, sweep)

    assert spend(db_engine, key_id) == (250, 0)
    assert not is_open(db_engine, reservation_id)


def test_unexpired_reservation_is_left_alone(db_engine):
    key_id = make_key(db_engine)
    reservation_id = hold(db_engine, key_id, 400, expires_in=OPEN)

    run(db_engine, sweep)

    assert spend(db_engine, key_id) == (0, 400)
    assert is_open(db_engine, reservation_id)


def test_settled_reservation_is_not_released_twice(db_engine):
    key_id = make_key(db_engine)
    hold(db_engine, key_id, 400, expires_in=EXPIRED, settled=True)
    hold(db_engine, key_id, 300, expires_in=OPEN)

    run(db_engine, sweep)

    assert spend(db_engine, key_id) == (0, 300)


def test_several_reservations_across_keys_and_months(db_engine):
    first, second = make_key(db_engine), make_key(db_engine)
    hold(db_engine, first, 100, expires_in=EXPIRED)
    hold(db_engine, first, 200, expires_in=EXPIRED)
    hold(db_engine, first, 50, expires_in=EXPIRED, period="2020-01")
    hold(db_engine, second, 700, expires_in=EXPIRED)
    hold(db_engine, second, 5, expires_in=OPEN)

    run(db_engine, sweep)

    assert spend(db_engine, first) == (0, 0)
    assert spend(db_engine, first, "2020-01") == (0, 0)
    assert spend(db_engine, second) == (0, 5)


def test_large_backlog_is_released_in_batches(db_engine):
    key_id = make_key(db_engine)
    for _ in range(7):
        hold(db_engine, key_id, 10, expires_in=EXPIRED)

    async def job(sessions):
        async with sessions() as db:
            return await release_expired(db, batch_size=3)

    assert run(db_engine, job) >= 7
    assert spend(db_engine, key_id) == (0, 0)


def test_two_sweeps_at_once_release_each_reservation_once(db_engine):
    key_id = make_key(db_engine)
    for _ in range(40):
        hold(db_engine, key_id, 25, expires_in=EXPIRED)

    async def job(sessions):
        return await asyncio.gather(sweep(sessions), sweep(sessions))

    run(db_engine, job)

    # A double release would take reserved_micros below zero, which the database refuses.
    assert spend(db_engine, key_id) == (0, 0)


def test_crash_between_reserve_and_settle_frees_the_budget(db_engine):
    key_id = make_key(db_engine)

    async def crash_then_sweep(sessions):
        async with sessions() as db:
            key = await db.get(ApiKey, key_id)
            reserved = await reserve(db, key, 1_000_000)
            # The server "crashes": settle never runs. The whole budget is stuck...
            assert await reserve(db, key, 1) is None
            # ...until the reservation expires and the sweep releases it.
            reservation = await db.get(Reservation, reserved.reservation_id)
            reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
        await sweep(sessions)
        async with sessions() as db:
            return await reserve(db, await db.get(ApiKey, key_id), 1_000_000)

    assert run(db_engine, crash_then_sweep) is not None
    assert spend(db_engine, key_id) == (0, 1_000_000)


def test_released_reservations_are_logged(db_engine, caplog):
    key_id = make_key(db_engine)
    hold(db_engine, key_id, 10, expires_in=EXPIRED)

    with caplog.at_level(logging.WARNING, logger="app.jobs.sweep_reservations"):
        run(db_engine, sweep)

    assert "expired reservations" in caplog.text


def test_job_keeps_running_after_a_failed_sweep(monkeypatch, caplog):
    calls = []

    async def flaky_sweep() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database away")
        if len(calls) == 3:
            raise asyncio.CancelledError
        return 0

    monkeypatch.setattr(sweep_reservations, "sweep", flaky_sweep)

    with caplog.at_level(logging.ERROR, logger="app.jobs.sweep_reservations"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(sweep_reservations.run_forever(interval_s=0))

    assert len(calls) == 3
    assert "Reservation sweep failed" in caplog.text
