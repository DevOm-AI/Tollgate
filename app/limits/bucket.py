import time
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis

# Refill, check and take in one script: Redis runs it atomically, so two requests can never
# both take the last token. The bucket is a hash {tokens, ts}; a missing bucket is full.
#
# KEYS[1]  bucket key
# ARGV[1]  capacity: most tokens the bucket holds, and how many it refills per period
# ARGV[2]  period in ms (60,000 for "per minute")
# ARGV[3]  cost: tokens this request takes
# ARGV[4]  now in ms, from the app's clock (not Redis TIME, which some hosted Redis builds
#          don't allow in scripts)
# ARGV[5]  "take": take `cost` only if the bucket has it.
#          "adjust": take `cost` regardless (negative gives tokens back). The bucket may go
#          below zero, which makes the key wait longer; it never goes above capacity.
#
# Returns {allowed (1/0), tokens left (whole, at least 0), ms until `cost` tokens are there
#          (-1 if never), ms until the bucket is full again}.
BUCKET_SCRIPT = """
local capacity = tonumber(ARGV[1])
local period_ms = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local mode = ARGV[5]

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(bucket[1]) or capacity
local ts = tonumber(bucket[2]) or now

-- A clock that went backwards refills nothing, and never moves ts back.
local elapsed = math.max(0, now - ts)
-- Multiply before dividing, so whole results (e.g. 30,000 ms) come out exact.
tokens = math.min(capacity, tokens + elapsed * capacity / period_ms)

local allowed = 0
if mode == 'adjust' then
  tokens = math.min(capacity, tokens - cost)
  allowed = 1
elseif tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end

local retry_ms = 0
if allowed == 0 then
  if cost > capacity then
    retry_ms = -1
  else
    retry_ms = math.ceil((cost - tokens) * period_ms / capacity)
  end
end
local reset_ms = math.ceil((capacity - tokens) * period_ms / capacity)

redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(math.max(now, ts)))
-- Once full, the bucket is the same as a missing one: let Redis drop it.
redis.call('PEXPIRE', KEYS[1], math.max(reset_ms, 1))

return {allowed, math.max(0, math.floor(tokens)), retry_ms, reset_ms}
"""

KEY_PREFIX = "tg:bucket:"
MINUTE_MS = 60_000


@dataclass(frozen=True)
class TakeResult:
    allowed: bool
    limit: int
    # Whole tokens left after this take.
    remaining: int
    # When a denied request could succeed; None if it never can (cost > limit).
    retry_after_ms: int | None
    # When the bucket is full again.
    reset_ms: int


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class TokenBucket:
    """Buckets in Redis that hold up to `capacity` tokens and refill `capacity` per period."""

    def __init__(
        self,
        redis: Redis,
        *,
        period_ms: int = MINUTE_MS,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self._script = redis.register_script(BUCKET_SCRIPT)
        self._period_ms = period_ms
        self._clock = clock

    async def take(self, name: str, capacity: int, cost: int = 1) -> TakeResult:
        """Take `cost` tokens from bucket `name` if it has them. Nothing is taken otherwise."""
        if cost < 0:
            raise ValueError("cost must not be negative")
        return await self._run(name, capacity, cost, "take")

    async def adjust(self, name: str, capacity: int, tokens: int) -> TakeResult:
        """Give `tokens` back to bucket `name` (up to capacity), or take -`tokens` regardless."""
        return await self._run(name, capacity, -tokens, "adjust")

    async def _run(self, name: str, capacity: int, cost: int, mode: str) -> TakeResult:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        allowed, remaining, retry_ms, reset_ms = await self._script(
            keys=[KEY_PREFIX + name],
            args=[capacity, self._period_ms, cost, self._clock(), mode],
        )
        return TakeResult(
            allowed=bool(allowed),
            limit=capacity,
            remaining=int(remaining),
            retry_after_ms=None if retry_ms < 0 else int(retry_ms),
            reset_ms=int(reset_ms),
        )
