"""Release reservations left open past their expiry, e.g. by a crash between reserve and settle.

Run once a minute: python -m app.jobs.sweep_reservations
"""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import SessionLocal

logger = logging.getLogger(__name__)

INTERVAL_S = 60
# Reservations released per transaction, so one huge backlog doesn't hold locks for long.
BATCH_SIZE = 500

# One statement: close a batch of expired reservations and give their money back to the
# keys' budgets. Nothing is charged: the requests never finished.
#
# FOR UPDATE SKIP LOCKED makes it safe to run alongside settle and alongside another sweep:
# a reservation being settled right now is skipped (settle closes it), and one this sweep
# has locked makes settle wait, then find it closed and bill nothing. Both lock the
# reservation before key_spend, so they can't deadlock.
RELEASE_EXPIRED = text(
    """
    WITH released AS (
        UPDATE reservations
        SET settled_at = now()
        WHERE id IN (
            SELECT id FROM reservations
            WHERE settled_at IS NULL AND expires_at < now()
            ORDER BY expires_at
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        )
        RETURNING key_id, period, amount_micros
    ),
    totals AS (
        SELECT key_id, period, sum(amount_micros) AS amount, count(*) AS released
        FROM released
        GROUP BY key_id, period
    ),
    freed AS (
        UPDATE key_spend AS spend
        SET reserved_micros = spend.reserved_micros - totals.amount
        FROM totals
        WHERE spend.key_id = totals.key_id AND spend.period = totals.period
        RETURNING totals.released
    )
    SELECT coalesce(sum(released), 0) FROM freed
    """
)


async def release_expired(db: AsyncSession, batch_size: int = BATCH_SIZE) -> int:
    """Release every expired reservation. Returns how many were released."""
    total = 0
    while True:
        released = int(await db.scalar(RELEASE_EXPIRED, {"batch_size": batch_size}))
        await db.commit()
        total += released
        if released < batch_size:
            return total


async def sweep(sessions: async_sessionmaker[AsyncSession] = SessionLocal) -> int:
    async with sessions() as db:
        released = await release_expired(db)
    if released:
        # Each one is a request that never settled: worth a look if it keeps happening.
        logger.warning("Released %d expired reservations", released)
    return released


async def run_forever(interval_s: float = INTERVAL_S) -> None:
    while True:
        try:
            await sweep()
        except Exception:
            # Keep going: the next run picks up whatever this one missed.
            logger.exception("Reservation sweep failed")
        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run_forever())
