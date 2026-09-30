"""Run every background job in one process, each on its own schedule.

python -m app.jobs
"""

import logging

import anyio

from app.billing.stripe_billing import get_stripe_billing
from app.jobs import push_usage, sweep_reservations

logger = logging.getLogger("app.jobs")


async def main() -> None:
    async with anyio.create_task_group() as jobs:
        jobs.start_soon(sweep_reservations.run_forever)
        billing = get_stripe_billing()
        if billing is None:
            logger.warning("STRIPE_SECRET_KEY is not set: usage stays in the outbox")
        else:
            jobs.start_soon(push_usage.run_forever, billing)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    anyio.run(main)
