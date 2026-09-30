"""Once a day, check that Stripe holds exactly the usage Tollgate recorded.

For the previous UTC day, each billed customer's usage total in Postgres is compared with
Stripe's meter summary for the same day. Any difference is logged: real billing systems
check themselves like this, because a gap means someone is being billed wrongly.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import anyio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.billing.stripe_billing import StripeBilling
from app.core.db import SessionLocal
from app.jobs.schedule import seconds_until_daily

logger = logging.getLogger(__name__)

# Runs at this time each day (UTC), for the day before: late enough that the day's usage
# has been pushed (each minute goes out a minute or two after it ends).
RUN_AT = time(0, 30, tzinfo=UTC)

# Every customer billed through Stripe, with the usage Tollgate recorded for them that day.
DAY_TOTALS = text(
    """
    SELECT c.id, c.stripe_customer_id,
           coalesce(sum(o.cost_micros), 0) AS recorded,
           coalesce(sum(o.cost_micros) FILTER (WHERE o.sent_at IS NOT NULL), 0) AS sent
    FROM customers AS c
    LEFT JOIN usage_outbox AS o
      ON o.customer_id = c.id AND o.window_start >= :start AND o.window_start < :end
    WHERE c.stripe_customer_id IS NOT NULL
    GROUP BY c.id, c.stripe_customer_id
    ORDER BY c.id
    """
)


@dataclass(frozen=True)
class DayCheck:
    customer_id: uuid.UUID
    stripe_customer_id: str
    day: date
    recorded: int  # Usage Tollgate recorded, in micro-dollars.
    sent: int  # The part of it the outbox has sent to Stripe.
    in_stripe: int | None  # What Stripe's meter holds; None if Stripe couldn't be asked.

    @property
    def matches(self) -> bool:
        return self.recorded == self.in_stripe


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(0, 0), tzinfo=UTC)
    return start, start + timedelta(days=1)


async def reconcile(
    billing: StripeBilling,
    day: date,
    sessions: async_sessionmaker[AsyncSession] = SessionLocal,
) -> list[DayCheck]:
    start, end = day_bounds(day)
    async with sessions() as db:
        rows = (await db.execute(DAY_TOTALS, {"start": start, "end": end})).all()

    checks = []
    for customer_id, stripe_customer_id, recorded, sent in rows:
        try:
            in_stripe = await anyio.to_thread.run_sync(
                billing.usage_between,
                stripe_customer_id,
                int(start.timestamp()),
                int(end.timestamp()),
            )
        except Exception:
            logger.exception("Couldn't read Stripe usage for %s on %s", customer_id, day)
            in_stripe = None
        check = DayCheck(customer_id, stripe_customer_id, day, recorded, sent, in_stripe)
        checks.append(check)
        if in_stripe is not None and not check.matches:
            logger.warning(
                "Usage mismatch for customer %s (%s) on %s: Tollgate recorded %d micros "
                "(%d sent), Stripe has %d (difference %+d)",
                customer_id,
                stripe_customer_id,
                day,
                recorded,
                sent,
                in_stripe,
                in_stripe - recorded,
            )
    matched = sum(check.matches for check in checks)
    logger.info("Reconciled %s: %d of %d customers match Stripe", day, matched, len(checks))
    return checks


def seconds_until_next_run(now: datetime) -> float:
    return seconds_until_daily(RUN_AT, now)


async def run_forever(billing: StripeBilling) -> None:
    while True:
        await anyio.sleep(seconds_until_next_run(datetime.now(UTC)))
        yesterday = datetime.now(UTC).date() - timedelta(days=1)
        try:
            await reconcile(billing, yesterday)
        except Exception:
            logger.exception("Reconciliation for %s failed", yesterday)
