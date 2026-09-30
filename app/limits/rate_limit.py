import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends
from redis.asyncio import Redis

from app.core.redis import get_redis
from app.limits.bucket import TakeResult, TokenBucket

LimitName = Literal["requests", "tokens"]


class RateLimited(Exception):
    """A key's per-minute limit is used up. `result` says when it has room again."""

    def __init__(self, limit: LimitName, result: TakeResult, cost: int) -> None:
        super().__init__(f"{limit} per minute limit reached")
        self.limit = limit
        self.result = result
        self.cost = cost


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
            raise RateLimited("requests", requests, cost=1)
        tokens = await self._bucket.take(f"{key_id}:tpm", tpm_limit, estimated_tokens)
        if not tokens.allowed:
            # The request doesn't go ahead, so it doesn't count against requests either.
            await self._bucket.adjust(f"{key_id}:rpm", rpm_limit, 1)
            raise RateLimited("tokens", tokens, cost=estimated_tokens)
        return Admission(key_id, tpm_limit, estimated_tokens, requests, tokens)

    async def settle(self, admission: Admission, actual_tokens: int) -> TakeResult:
        """Give back what the estimate over-took, or take what it under-took."""
        return await self._bucket.adjust(
            f"{admission.key_id}:tpm",
            admission.tpm_limit,
            admission.estimated_tokens - actual_tokens,
        )


def get_rate_limiter(redis: Annotated[Redis, Depends(get_redis)]) -> RateLimiter:
    return RateLimiter(TokenBucket(redis))
