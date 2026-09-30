"""Circuit breakers: stop sending requests to a provider that keeps failing.

Without one, while a provider is down every request waits out its timeout before falling back.
Per provider: after `threshold` failures within `window_s` the breaker opens and the provider
gets no requests for `open_s`. Then it's half-open: one test request goes through. Success
closes the breaker; failure opens it again.

State lives in memory, per app instance: it protects latency, not money, so losing it on a
restart only means relearning that a provider is down.
"""

import math
import time
from collections import deque
from collections.abc import Callable
from functools import lru_cache
from typing import Any, Literal

from app.core.config import get_settings

State = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    def __init__(
        self,
        *,
        threshold: int = 5,
        window_s: float = 30.0,
        open_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.window_s = window_s
        self.open_s = open_s
        self._clock = clock
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        # When the half-open test request went out; None if none is in flight.
        self._trial_started_at: float | None = None

    @property
    def state(self) -> State:
        if self._opened_at is None:
            return "closed"
        if self._clock() < self._opened_at + self.open_s:
            return "open"
        return "half_open"

    def available(self) -> bool:
        """Whether `allow` could let a request through now. Takes nothing."""
        state = self.state
        return state == "closed" or (state == "half_open" and not self._trial_in_flight())

    def allow(self) -> bool:
        """Whether a request may go to the provider now. In half-open, the one that gets
        True is the test request; the rest get False until it reports back."""
        state = self.state
        if state == "closed":
            return True
        if state == "open" or self._trial_in_flight():
            return False
        self._trial_started_at = self._clock()
        return True

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None
        self._trial_started_at = None

    def record_failure(self) -> None:
        now = self._clock()
        if self.state == "half_open":
            self._open(now)  # The test request failed: back to open.
            return
        self._failures.append(now)
        while self._failures and self._failures[0] <= now - self.window_s:
            self._failures.popleft()
        if len(self._failures) >= self.threshold:
            self._open(now)

    def release(self) -> None:
        """A request ended without saying anything about the provider (e.g. cancelled)."""
        self._trial_started_at = None

    def seconds_until_available(self) -> int:
        if self.state != "open":
            return 0
        return max(1, math.ceil(self._opened_at + self.open_s - self._clock()))

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "recent_failures": len(self._failures)}

    def _open(self, now: float) -> None:
        self._opened_at = now
        self._trial_started_at = None
        self._failures.clear()

    def _trial_in_flight(self) -> bool:
        # A test request that never reported back (say, its task was killed) stops blocking
        # after one open period, so the breaker can't stay stuck half-open.
        started = self._trial_started_at
        return started is not None and self._clock() < started + self.open_s


class Breakers:
    """One breaker per provider name, made on first use."""

    def __init__(
        self,
        *,
        threshold: int = 5,
        window_s: float = 30.0,
        open_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = {
            "threshold": threshold,
            "window_s": window_s,
            "open_s": open_s,
            "clock": clock,
        }
        self._breakers: dict[str, CircuitBreaker] = {}

    def __getitem__(self, provider: str) -> CircuitBreaker:
        if provider not in self._breakers:
            self._breakers[provider] = CircuitBreaker(**self._settings)
        return self._breakers[provider]

    def snapshot(self, providers: list[str]) -> dict[str, dict[str, Any]]:
        return {name: self[name].snapshot() for name in providers}


@lru_cache
def get_breakers() -> Breakers:
    settings = get_settings()
    return Breakers(
        threshold=settings.breaker_failure_threshold,
        window_s=settings.breaker_window_s,
        open_s=settings.breaker_open_s,
    )
