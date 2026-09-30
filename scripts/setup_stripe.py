"""Create (or find) Tollgate's Stripe meter and metered price, and print their IDs.

    uv run python -m scripts.setup_stripe

Needs STRIPE_SECRET_KEY (test mode). Safe to run again: it finds what already exists.
Customers are subscribed with POST /admin/customers/{id}/billing.
"""

import sys

from app.billing.stripe_billing import METER_EVENT_NAME, PRICE_LOOKUP_KEY, get_stripe_billing


def main() -> int:
    billing = get_stripe_billing()
    if billing is None:
        print("STRIPE_SECRET_KEY is not set; see docs/accounts.md", file=sys.stderr)
        return 1
    meter_id = billing.ensure_meter()
    price_id = billing.ensure_price()
    print(f"meter  {meter_id}  (event name: {METER_EVENT_NAME})")
    print(f"price  {price_id}  (lookup key: {PRICE_LOOKUP_KEY})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
