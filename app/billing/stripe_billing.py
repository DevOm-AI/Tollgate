"""Stripe usage billing setup: a meter, a metered price on it, and a subscription per customer.

Usage is metered in micro-dollars (1 USD = 1,000,000): each meter event's value is what a
customer spent, and the price charges 0.0001 US cents (one micro-dollar) per unit, so an
invoice is exactly the usage Tollgate recorded, to the cent.

Every step is find-or-create (by lookup key, or with an idempotency key), so running it again
changes nothing. The Stripe library is synchronous; async callers run these in a thread.
"""

import uuid
from dataclasses import dataclass
from functools import lru_cache

import stripe

from app.core.config import get_settings

# What usage events are sent as; the meter sums their "value" per Stripe customer.
METER_EVENT_NAME = "tollgate_usage"
METER_VALUE_KEY = "value"
METER_CUSTOMER_KEY = "stripe_customer_id"
# How Tollgate finds its price again, instead of storing its ID.
PRICE_LOOKUP_KEY = "tollgate_usage_micros"
# One unit is one micro-dollar: $0.000001 = 0.0001 US cents.
UNIT_AMOUNT_CENTS = "0.0001"


@dataclass(frozen=True)
class CustomerBilling:
    stripe_customer_id: str
    subscription_id: str
    price_id: str


class StripeBilling:
    def __init__(self, client: stripe.StripeClient) -> None:
        self._client = client

    def ensure_meter(self) -> str:
        """The meter for Tollgate usage events, created if it doesn't exist yet."""
        for meter in self._client.v1.billing.meters.list({"status": "active", "limit": 100}):
            if meter.event_name == METER_EVENT_NAME:
                return meter.id
        meter = self._client.v1.billing.meters.create(
            {
                "display_name": "Tollgate usage (micro-dollars)",
                "event_name": METER_EVENT_NAME,
                "default_aggregation": {"formula": "sum"},
                "customer_mapping": {"type": "by_id", "event_payload_key": METER_CUSTOMER_KEY},
                "value_settings": {"event_payload_key": METER_VALUE_KEY},
            },
            {"idempotency_key": f"tollgate-meter-{METER_EVENT_NAME}"},
        )
        return meter.id

    def ensure_price(self) -> str:
        """The monthly metered price on the usage meter, created if it doesn't exist yet."""
        prices = self._client.v1.prices.list(
            {"lookup_keys": [PRICE_LOOKUP_KEY], "active": True, "limit": 1}
        )
        for price in prices:
            return price.id
        meter_id = self.ensure_meter()
        price = self._client.v1.prices.create(
            {
                "currency": "usd",
                "unit_amount_decimal": UNIT_AMOUNT_CENTS,
                "billing_scheme": "per_unit",
                "recurring": {"interval": "month", "usage_type": "metered", "meter": meter_id},
                "product_data": {"name": "Tollgate usage"},
                "lookup_key": PRICE_LOOKUP_KEY,
            },
            {"idempotency_key": f"tollgate-price-{PRICE_LOOKUP_KEY}"},
        )
        return price.id

    def ensure_customer(
        self, customer_id: uuid.UUID, name: str, stripe_customer_id: str | None
    ) -> str:
        """The Stripe customer for a Tollgate customer: the stored one, or a new one."""
        if stripe_customer_id:
            return stripe_customer_id
        customer = self._client.v1.customers.create(
            {"name": name, "metadata": {"tollgate_customer_id": str(customer_id)}},
            {"idempotency_key": f"tollgate-customer-{customer_id}"},
        )
        return customer.id

    def ensure_subscription(self, stripe_customer_id: str, price_id: str) -> str:
        """A subscription to the usage price.

        Charged automatically (Stripe's default): unlike invoices sent by email, that needs no
        customer email to set up. Collecting the money needs a payment method on the Stripe
        customer (added in Stripe, e.g. through Checkout); usage is metered either way.
        """
        existing = self._client.v1.subscriptions.list(
            {"customer": stripe_customer_id, "price": price_id, "status": "active", "limit": 1}
        )
        for subscription in existing:
            return subscription.id
        subscription = self._client.v1.subscriptions.create(
            {
                "customer": stripe_customer_id,
                "items": [{"price": price_id}],
            },
            {"idempotency_key": f"tollgate-subscription-{stripe_customer_id}-{price_id}"},
        )
        return subscription.id

    def send_usage(
        self, stripe_customer_id: str, micros: int, timestamp: int, identifier: str
    ) -> None:
        """Report `micros` of usage for one customer and minute as a meter event.

        Stripe drops an event whose `identifier` it has already seen, so sending the same
        usage again (a retry after a crash) can't bill it twice.
        """
        self._client.v1.billing.meter_events.create(
            {
                "event_name": METER_EVENT_NAME,
                "payload": {METER_CUSTOMER_KEY: stripe_customer_id, METER_VALUE_KEY: str(micros)},
                "timestamp": timestamp,
                "identifier": identifier,
            }
        )

    def usage_between(self, stripe_customer_id: str, start: int, end: int) -> int:
        """Micro-dollars Stripe's meter holds for a customer between two Unix times."""
        meter_id = self.ensure_meter()
        summaries = self._client.v1.billing.meters.event_summaries.list(
            meter_id, {"customer": stripe_customer_id, "start_time": start, "end_time": end}
        )
        return round(sum(summary.aggregated_value for summary in summaries))

    def set_up_customer(
        self, customer_id: uuid.UUID, name: str, stripe_customer_id: str | None
    ) -> CustomerBilling:
        price_id = self.ensure_price()
        stripe_customer_id = self.ensure_customer(customer_id, name, stripe_customer_id)
        subscription_id = self.ensure_subscription(stripe_customer_id, price_id)
        return CustomerBilling(stripe_customer_id, subscription_id, price_id)


@lru_cache
def get_stripe_billing() -> StripeBilling | None:
    """Stripe billing, or None when STRIPE_SECRET_KEY isn't set (usage isn't billed)."""
    key = get_settings().stripe_secret_key
    if key is None:
        return None
    return StripeBilling(stripe.StripeClient(key.get_secret_value(), max_network_retries=2))
