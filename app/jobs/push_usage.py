"""Send usage from the outbox to Stripe: one meter event per customer and minute.

Settle writes a usage_outbox row in the same transaction that charges the budget, so usage
can't be lost between Tollgate and Stripe. This job groups unsent rows by customer and
minute, sends each group as one meter event, and marks the rows sent only after Stripe
answers OK. Each event carries a fixed identifier (customer + minute), which Stripe uses to
drop duplicates: a retry after a crash between sending and marking can't bill twice.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import anyio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.billing.stripe_billing import StripeBilling
from app.core.db import SessionLocal

logger = logging.getLogger(__name__)

# Groups sent per run; the rest wait for the next minute.
BATCH_SIZE = 500

# Only minutes that ended over a minute ago. A settle that starts at 12:00:59 can commit
# its 12:00 row just after 12:01; sending 12:00 before that row lands would leave it for a
# second event with the same identifier, which Stripe would drop.
UNSENT_GROUPS = text(
    """
    SELECT o.customer_id, o.window_start, c.stripe_customer_id
    FROM usage_outbox AS o
    JOIN customers AS c ON c.id = o.customer_id
    WHERE o.sent_at IS NULL
      AND o.window_start < date_trunc('minute', now()) - interval '1 minute'
      AND c.stripe_customer_id IS NOT NULL
    GROUP BY o.customer_id, o.window_start, c.stripe_customer_id
    ORDER BY o.window_start
    LIMIT :batch_size
    """
)

# The group's unsent rows, locked so a second job run can't send them too.
LOCK_GROUP = text(
    """
    SELECT id, cost_micros FROM usage_outbox
    WHERE customer_id = :customer_id AND window_start = :window_start AND sent_at IS NULL
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    """
)

ALREADY_SENT = text(
    """
    SELECT EXISTS (
        SELECT 1 FROM usage_outbox
        WHERE customer_id = :customer_id AND window_start = :window_start
          AND sent_at IS NOT NULL
    )
    """
)

MARK_SENT = text("UPDATE usage_outbox SET sent_at = now() WHERE id = ANY(:ids)")


@dataclass(frozen=True)
class Group:
    customer_id: uuid.UUID
    window_start: datetime
    stripe_customer_id: str


def event_identifier(group: Group, first_row_id: int | None = None) -> str:
    """customer + minute. Rows that arrive after their minute was already sent (which the
    grace period should prevent) get their own identifier, fixed by their first row's id,
    so they're neither dropped as a duplicate nor billed twice on retry."""
    identifier = f"tg-{group.customer_id}-{group.window_start:%Y%m%dT%H%M}"
    return identifier if first_row_id is None else f"{identifier}-late-{first_row_id}"


async def push_usage(
    billing: StripeBilling, sessions: async_sessionmaker[AsyncSession] = SessionLocal
) -> int:
    """Send every ready group. Returns how many were sent."""
    async with sessions() as db:
        rows = (await db.execute(UNSENT_GROUPS, {"batch_size": BATCH_SIZE})).all()
    sent = 0
    for customer_id, window_start, stripe_customer_id in rows:
        group = Group(customer_id, window_start, stripe_customer_id)
        try:
            if await _send_group(sessions, billing, group):
                sent += 1
        except Exception:
            # Left unsent: the next run retries it, with the same identifier.
            logger.exception("Sending usage for %s at %s failed", customer_id, window_start)
    return sent


async def _send_group(
    sessions: async_sessionmaker[AsyncSession], billing: StripeBilling, group: Group
) -> bool:
    async with sessions() as db, db.begin():
        params = {"customer_id": group.customer_id, "window_start": group.window_start}
        locked = (await db.execute(LOCK_GROUP, params)).all()
        if not locked:
            return False  # Another run has these rows, or just sent them.
        ids = [row_id for row_id, _ in locked]
        total = sum(cost for _, cost in locked)
        late = bool(await db.scalar(ALREADY_SENT, params))
        identifier = event_identifier(group, ids[0] if late else None)
        if late:
            logger.warning("Usage arrived after its minute was sent: %s", identifier)
        await anyio.to_thread.run_sync(
            billing.send_usage,
            group.stripe_customer_id,
            total,
            int(group.window_start.timestamp()),
            identifier,
        )
        # Only now that Stripe has it. A crash before this commit re-sends the same
        # identifier next time, and Stripe drops the duplicate.
        await db.execute(MARK_SENT, {"ids": ids})
    return True


INTERVAL_S = 60


async def run_forever(billing: StripeBilling, interval_s: float = INTERVAL_S) -> None:
    while True:
        try:
            await push_usage(billing)
        except Exception:
            # Keep going: unsent rows stay in the outbox for the next run.
            logger.exception("Usage push failed")
        await anyio.sleep(interval_s)
