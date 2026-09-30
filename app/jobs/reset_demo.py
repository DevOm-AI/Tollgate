"""Once a day, give the playground's demo key its budget back, so the demo keeps working.

The demo key has a tiny budget so a public page can't run up a bill; Tollgate enforces it
like any other. Resetting it daily lets the next visitor try the demo too. Only the spend is
reset: money held by requests in flight stays held, so their settles still add up.
"""

import logging
from datetime import UTC, datetime, time

import anyio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.billing.budget import current_period
from app.core.db import SessionLocal
from app.core.security import hash_api_key
from app.jobs.schedule import seconds_until_daily

logger = logging.getLogger(__name__)

RUN_AT = time(0, 0, tzinfo=UTC)

RESET_SPEND = text(
    """
    UPDATE key_spend SET spent_micros = 0
    WHERE period = :period
      AND key_id = (SELECT id FROM api_keys WHERE key_hash = :key_hash)
    RETURNING key_id
    """
)

KEY_EXISTS = text("SELECT EXISTS (SELECT 1 FROM api_keys WHERE key_hash = :key_hash)")


async def reset_demo_spend(
    demo_key: str, sessions: async_sessionmaker[AsyncSession] = SessionLocal
) -> bool:
    """Set the demo key's spend this month back to zero. False if the key isn't found."""
    key_hash = hash_api_key(demo_key)
    async with sessions() as db:
        if not await db.scalar(KEY_EXISTS, {"key_hash": key_hash}):
            logger.warning("DEMO_API_KEY doesn't match any key; nothing to reset")
            return False
        await db.execute(RESET_SPEND, {"period": current_period(), "key_hash": key_hash})
        await db.commit()
    logger.info("Demo key's budget reset")
    return True


async def run_forever(demo_key: str) -> None:
    while True:
        await anyio.sleep(seconds_until_daily(RUN_AT, datetime.now(UTC)))
        try:
            await reset_demo_spend(demo_key)
        except Exception:
            logger.exception("Demo budget reset failed")
