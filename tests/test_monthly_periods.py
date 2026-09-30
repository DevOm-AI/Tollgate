import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import Engine, pool, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from app.billing import budget
from app.billing.budget import Outcome, reserve, settle
from app.models import ApiKey, Customer, KeySpend

SEPT_LAST_SECOND = datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC)
OCT_FIRST_SECOND = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC)
BUDGET = 1_000_000  # $1.00


def make_key(db_engine: Engine) -> uuid.UUID:
    with Session(db_engine) as session:
        customer = Customer(name="Period test")
        session.add(customer)
        session.flush()
        key = ApiKey(
            customer_id=customer.id,
            key_hash=uuid.uuid4().hex * 2,
            prefix="ab12cd34",
            rpm_limit=60,
            tpm_limit=100_000,
            monthly_budget_micros=BUDGET,
        )
        session.add(key)
        session.commit()
        return key.id


def periods(db_engine: Engine, key_id: uuid.UUID) -> dict[str, tuple[int, int]]:
    with Session(db_engine) as session:
        rows = session.scalars(select(KeySpend).where(KeySpend.key_id == key_id))
        return {row.period: (row.spent_micros, row.reserved_micros) for row in rows}


def run(db_engine: Engine, job):
    async def main():
        engine = create_async_engine(db_engine.url, poolclass=pool.NullPool)
        try:
            return await job(engine)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def charge(engine, key_id: uuid.UUID, amount: int, now: datetime) -> bool:
    """Reserve `amount` at `now` and settle it in full. False if the budget refused it."""
    async with AsyncSession(engine, expire_on_commit=False) as db:
        hold = await reserve(db, await db.get(ApiKey, key_id), amount, now=now)
        if hold is None:
            return False
        await settle(db, hold, amount, Outcome(model="m", provider="fake", status="ok"))
        return True


def test_first_request_of_a_month_creates_its_row(db_engine):
    key_id = make_key(db_engine)

    assert run(db_engine, lambda engine: charge(engine, key_id, 100, OCT_FIRST_SECOND))

    assert periods(db_engine, key_id) == {"2026-10": (100, 0)}


def test_new_month_starts_with_the_full_budget(db_engine):
    key_id = make_key(db_engine)

    async def job(engine):
        spent_september = await charge(engine, key_id, BUDGET, SEPT_LAST_SECOND)
        refused_september = await charge(engine, key_id, 1, SEPT_LAST_SECOND)
        spent_october = await charge(engine, key_id, BUDGET, OCT_FIRST_SECOND)
        return spent_september, refused_september, spent_october

    assert run(db_engine, job) == (True, False, True)
    assert periods(db_engine, key_id) == {"2026-09": (BUDGET, 0), "2026-10": (BUDGET, 0)}


def test_request_straddling_the_month_settles_in_the_month_it_reserved(db_engine, monkeypatch):
    key_id = make_key(db_engine)

    async def job(engine):
        async with AsyncSession(engine, expire_on_commit=False) as db:
            key = await db.get(ApiKey, key_id)
            hold = await reserve(db, key, 500, now=SEPT_LAST_SECOND)
            # The answer arrives after midnight: October has already begun, for any code
            # that asks the clock (settle must use the hold's month instead).
            monkeypatch.setattr(budget, "current_period", lambda now=None: "2026-10")
            await reserve(db, key, 1, now=OCT_FIRST_SECOND)
            await settle(db, hold, 300, Outcome(model="m", provider="fake", status="ok"))

    run(db_engine, job)

    periods_now = periods(db_engine, key_id)
    assert periods_now["2026-09"] == (300, 0)
    # October only holds its own (unsettled) reservation.
    assert periods_now["2026-10"] == (0, 1)


def test_concurrent_first_requests_create_one_row_and_share_the_budget(db_engine):
    key_id = make_key(db_engine)
    # Room for exactly 10 of these in the new month.
    amount = BUDGET // 10

    async def job(engine):
        return await asyncio.gather(
            *(charge(engine, key_id, amount, OCT_FIRST_SECOND) for _ in range(25))
        )

    results = run(db_engine, job)

    assert results.count(True) == 10
    assert periods(db_engine, key_id) == {"2026-10": (BUDGET, 0)}
