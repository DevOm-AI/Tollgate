"""Create the playground's demo key and print it, to put in DEMO_API_KEY.

    uv run python -m scripts.create_demo_key

The key gets a $0.10 monthly budget and 5 requests per minute: whatever visitors do with the
public playground, Tollgate's own limits cap what it can cost. Run it once; each run makes
a new key (the old one keeps working until revoked).
"""

import asyncio
import sys

from app.core.db import SessionLocal, engine
from app.core.security import generate_api_key
from app.models import ApiKey, Customer

DEMO_CUSTOMER = "Tollgate demo"
DEMO_BUDGET_MICROS = 100_000  # $0.10
DEMO_RPM_LIMIT = 5
DEMO_TPM_LIMIT = 20_000


async def create_demo_key() -> str:
    new_key = generate_api_key()
    async with SessionLocal() as db:
        customer = Customer(name=DEMO_CUSTOMER)
        db.add(customer)
        await db.flush()
        db.add(
            ApiKey(
                customer_id=customer.id,
                key_hash=new_key.key_hash,
                prefix=new_key.prefix,
                rpm_limit=DEMO_RPM_LIMIT,
                tpm_limit=DEMO_TPM_LIMIT,
                monthly_budget_micros=DEMO_BUDGET_MICROS,
            )
        )
        await db.commit()
    await engine.dispose()
    return new_key.key


def main() -> int:
    key = asyncio.run(create_demo_key())
    print("Demo key created ($0.10 budget, 5 requests/min). Set it where the API runs:")
    print(f"DEMO_API_KEY={key}")
    print("It's shown only now; Tollgate stores just its hash.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
