"""An in-memory stand-in for the parts of stripe.StripeClient Tollgate uses."""

import uuid
from types import SimpleNamespace
from typing import Any

import stripe


class _Resource:
    def __init__(self, fake: "FakeStripe", kind: str, prefix: str) -> None:
        self._fake = fake
        self._kind = kind
        self._prefix = prefix

    def create(self, params: dict[str, Any], options: dict[str, Any] | None = None):
        self._fake.calls.append((self._kind, "create", params, options or {}))
        if self._fake.fail_with is not None:
            raise self._fake.fail_with
        key = (options or {}).get("idempotency_key")
        if key and key in self._fake.idempotent:
            return self._fake.idempotent[key]
        obj = SimpleNamespace(id=f"{self._prefix}_{uuid.uuid4().hex[:14]}", **params)
        self._fake.objects[self._kind].append(obj)
        if key:
            self._fake.idempotent[key] = obj
        return obj

    def list(self, params: dict[str, Any] | None = None, options: dict[str, Any] | None = None):
        params = params or {}
        self._fake.calls.append((self._kind, "list", params, options or {}))
        found = self._fake.objects[self._kind]
        if self._kind == "prices" and "lookup_keys" in params:
            found = [p for p in found if p.lookup_key in params["lookup_keys"]]
        if self._kind == "subscriptions":
            found = [
                s
                for s in found
                if s.customer == params.get("customer")
                and params.get("price") in [item["price"] for item in s.items]
            ]
        return list(found)


class FakeStripe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.objects: dict[str, list] = {
            k: [] for k in ("meters", "prices", "customers", "subscriptions", "meter_events")
        }
        self.idempotent: dict[str, Any] = {}
        self.fail_with: stripe.StripeError | None = None
        self.v1 = SimpleNamespace(
            billing=SimpleNamespace(
                meters=_Resource(self, "meters", "mtr"),
                meter_events=_Resource(self, "meter_events", "mev"),
            ),
            prices=_Resource(self, "prices", "price"),
            customers=_Resource(self, "customers", "cus"),
            subscriptions=_Resource(self, "subscriptions", "sub"),
        )

    def created(self, kind: str) -> list[dict]:
        return [params for k, action, params, _ in self.calls if k == kind and action == "create"]
