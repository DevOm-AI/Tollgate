import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiKey, KeySpend, RequestLog, Reservation, UsageOutbox

logger = logging.getLogger(__name__)

# How long a reservation may stay open before it counts as leaked (e.g. a crash mid-request).
RESERVATION_TTL = timedelta(minutes=10)


def current_period(now: datetime | None = None) -> str:
    """The calendar month in UTC, e.g. 2026-10: the period budgets are counted in."""
    return (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m")


@dataclass(frozen=True)
class Hold:
    """Money reserved for one request."""

    reservation_id: uuid.UUID
    key_id: uuid.UUID
    customer_id: uuid.UUID
    period: str
    amount_micros: int


@dataclass(frozen=True)
class Outcome:
    """What happened to a request, for its row in `requests`."""

    model: str
    provider: str
    status: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int | None = None


async def reserve(
    db: AsyncSession, key: ApiKey, amount_micros: int, now: datetime | None = None
) -> Hold | None:
    """Hold `amount_micros` of the key's monthly budget, or return None if it won't fit.

    One conditional UPDATE claims the money: Postgres locks the key_spend row, so concurrent
    requests queue on it and each re-checks the budget against the latest total. Checking
    first and adding later would let a burst of requests all pass the check at once.

    Budgets are per calendar month in UTC. A key's first request in a month creates that
    month's key_spend row (INSERT ... ON CONFLICT DO NOTHING, so concurrent first requests
    create it once); earlier months' spend doesn't count against it. The hold remembers its
    month, so a request that straddles midnight on the 1st settles against the month it
    reserved in.
    """
    period = current_period(now)
    await db.execute(insert(KeySpend).values(key_id=key.id, period=period).on_conflict_do_nothing())
    claimed = await db.execute(
        update(KeySpend)
        .where(
            KeySpend.key_id == key.id,
            KeySpend.period == period,
            KeySpend.spent_micros + KeySpend.reserved_micros + amount_micros
            <= key.monthly_budget_micros,
        )
        .values(reserved_micros=KeySpend.reserved_micros + amount_micros)
        .returning(KeySpend.key_id)
        .execution_options(synchronize_session=False)
    )
    if claimed.first() is None:
        await db.rollback()
        return None
    reservation_id = uuid.uuid4()
    db.add(
        Reservation(
            id=reservation_id,
            key_id=key.id,
            period=period,
            amount_micros=amount_micros,
            expires_at=datetime.now(UTC) + RESERVATION_TTL,
        )
    )
    await db.commit()
    return Hold(reservation_id, key.id, key.customer_id, period, amount_micros)


async def settle(db: AsyncSession, hold: Hold, cost_micros: int, outcome: Outcome) -> int:
    """Close the hold: charge `cost_micros`, free the rest, and record the request.

    One transaction: the spend, the request row and the usage row for Stripe are written
    together or not at all, so usage can't be billed without being spent, or the reverse.
    A failed request settles with a cost of 0. Returns what was charged.
    """
    closed = await db.execute(
        update(Reservation)
        .where(Reservation.id == hold.reservation_id, Reservation.settled_at.is_(None))
        .values(settled_at=func.now())
        .returning(Reservation.id)
        .execution_options(synchronize_session=False)
    )
    if closed.first() is None:
        # Already released as leaked: its money went back to the budget, so charging now
        # could take the key past it. The request goes unbilled.
        logger.warning("Reservation %s was already released; not billing", hold.reservation_id)
        charged = 0
        spend_change = {}
    else:
        charged = cost_micros
        if charged > hold.amount_micros:
            # The reservation was meant to be the worst case. Bill no more than was held, so
            # the key can't pass its budget; Tollgate absorbs the difference.
            logger.warning(
                "Request cost %d micros but reserved %d; billing the reserved amount",
                cost_micros,
                hold.amount_micros,
            )
            charged = hold.amount_micros
        spend_change = {
            "reserved_micros": KeySpend.reserved_micros - hold.amount_micros,
            "spent_micros": KeySpend.spent_micros + charged,
        }

    if spend_change:
        await db.execute(
            update(KeySpend)
            .where(KeySpend.key_id == hold.key_id, KeySpend.period == hold.period)
            .values(**spend_change)
            .execution_options(synchronize_session=False)
        )
    db.add(
        RequestLog(
            key_id=hold.key_id,
            model=outcome.model,
            provider=outcome.provider,
            status=outcome.status,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            cost_micros=charged,
            latency_ms=outcome.latency_ms,
        )
    )
    if charged > 0:
        db.add(
            UsageOutbox(
                customer_id=hold.customer_id,
                cost_micros=charged,
                window_start=func.date_trunc("minute", func.now()),
            )
        )
    await db.commit()
    return charged
