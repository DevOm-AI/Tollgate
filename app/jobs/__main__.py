"""Run every background job in one process, each on its own schedule.

python -m app.jobs
"""

import logging

import anyio

from app.billing.stripe_billing import get_stripe_billing
from app.core.config import get_settings
from app.jobs import push_usage, reconcile_usage, reset_demo, sweep_reservations

logger = logging.getLogger("app.jobs")


async def main() -> None:
    async with anyio.create_task_group() as jobs:
        jobs.start_soon(sweep_reservations.run_forever)
        billing = get_stripe_billing()
        if billing is None:
            logger.warning("STRIPE_SECRET_KEY is not set: usage stays in the outbox")
        else:
            jobs.start_soon(push_usage.run_forever, billing)
            jobs.start_soon(reconcile_usage.run_forever, billing)
        demo_key = get_settings().demo_api_key
        if demo_key is not None:
            jobs.start_soon(reset_demo.run_forever, demo_key.get_secret_value())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    anyio.run(main)
