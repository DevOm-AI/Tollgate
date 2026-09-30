import math
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends
from redis.asyncio import Redis

from app.core.redis import get_redis
from app.limits.bucket import TakeResult, TokenBucket

LimitName = Literal["requests", "tokens"]


class RateLimited(Exception):
    """A key's per-minute limit is used up.

    `limit` names the one that refused; `requests` and `tokens` are both buckets as they are
    now, so the response can report them.
    """

    def __init__(
        self, limit: LimitName, requests: TakeResult, tokens: TakeResult, cost: int
    ) -> None:
        super().__init__(f"{limit} per minute limit reached")
        self.limit = limit
        self.requests = requests
        self.tokens = tokens
        self.cost = cost

    @property
    def result(self) -> TakeResult:
        """The bucket that refused."""
        return self.requests if self.limit == "requests" else self.tokens


@dataclass(frozen=True)
class Admission:
    """A request let through, holding the tokens-per-minute estimate it took."""

    key_id: uuid.UUID
    tpm_limit: int
    estimated_tokens: int
    requests: TakeResult
    tokens: TakeResult


class RateLimiter:
    """Two limits per key: requests per minute and tokens per minute.

    The real token count is only known after the answer, so a request takes an estimate up
    front (input tokens + max_tokens) and settles the difference afterwards.
    """

    def __init__(self, bucket: TokenBucket) -> None:
        self._bucket = bucket

    async def admit(
        self, key_id: uuid.UUID, rpm_limit: int, tpm_limit: int, estimated_tokens: int
    ) -> Admission:
        """Take 1 request and the estimate, or raise RateLimited having taken neither."""
        requests = await self._bucket.take(f"{key_id}:rpm", rpm_limit)
        if not requests.allowed:
            # Taking 0 reads the tokens bucket without changing it.
            tokens = await self._bucket.take(f"{key_id}:tpm", tpm_limit, 0)
            raise RateLimited("requests", requests, tokens, cost=1)
        tokens = await self._bucket.take(f"{key_id}:tpm", tpm_limit, estimated_tokens)
        if not tokens.allowed:
            # The request doesn't go ahead, so it doesn't count against requests either.
            requests = await self._bucket.adjust(f"{key_id}:rpm", rpm_limit, 1)
            raise RateLimited("tokens", requests, tokens, cost=estimated_tokens)
        return Admission(key_id, tpm_limit, estimated_tokens, requests, tokens)

    async def settle(self, admission: Admission, actual_tokens: int) -> TakeResult:
        """Give back what the estimate over-took, or take what it under-took."""
        return await self._bucket.adjust(
            f"{admission.key_id}:tpm",
            admission.tpm_limit,
            admission.estimated_tokens - actual_tokens,
        )


def rate_limit_headers(requests: TakeResult, tokens: TakeResult) -> dict[str, str]:
    """Both limits, as OpenAI reports them. Reset is whole seconds until the bucket is full."""
    headers = {}
    for suffix, result in (("Requests", requests), ("Tokens", tokens)):
        headers[f"X-RateLimit-Limit-{suffix}"] = str(result.limit)
        headers[f"X-RateLimit-Remaining-{suffix}"] = str(result.remaining)
        headers[f"X-RateLimit-Reset-{suffix}"] = str(_seconds(result.reset_ms))
    return headers


def retry_after(exc: RateLimited) -> int | None:
    """Whole seconds until the refused request could pass; None if it never can."""
    ms = exc.result.retry_after_ms
    return None if ms is None else max(1, _seconds(ms))


def _seconds(ms: int) -> int:
    return math.ceil(ms / 1000)


def get_rate_limiter(redis: Annotated[Redis, Depends(get_redis)]) -> RateLimiter:
    return RateLimiter(TokenBucket(redis))
