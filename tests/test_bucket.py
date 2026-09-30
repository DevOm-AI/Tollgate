import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest
from redis.asyncio import Redis

from app.core.config import Settings
from app.limits.bucket import KEY_PREFIX, MINUTE_MS, TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now_ms = 1_790_000_000_000

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, ms: int) -> None:
        self.now_ms += ms


def run(test: Callable[[TokenBucket, str, FakeClock, Redis], Awaitable[None]]) -> None:
    """Run `test` with a bucket on real Redis (REDIS_URL, else the compose Redis)."""

    async def main() -> None:
        redis = Redis.from_url(Settings(_env_file=None).redis_url)
        name = f"test:{uuid.uuid4().hex}"
        clock = FakeClock()
        try:
            await test(TokenBucket(redis, clock=clock), name, clock, redis)
        finally:
            await redis.delete(KEY_PREFIX + name)
            await redis.aclose()

    asyncio.run(main())


def test_new_bucket_is_full_then_empties():
    async def test(bucket, name, clock, redis):
        results = [await bucket.take(name, capacity=5) for _ in range(6)]

        assert [r.allowed for r in results] == [True] * 5 + [False]
        assert [r.remaining for r in results] == [4, 3, 2, 1, 0, 0]
        assert all(r.limit == 5 for r in results)

    run(test)


def test_concurrent_takes_never_share_the_last_token():
    async def test(bucket, name, clock, redis):
        results = await asyncio.gather(*(bucket.take(name, capacity=5) for _ in range(20)))

        assert sum(r.allowed for r in results) == 5

    run(test)


def test_bucket_refills_at_its_limit_per_minute():
    async def test(bucket, name, clock, redis):
        for _ in range(60):
            await bucket.take(name, capacity=60)
        assert not (await bucket.take(name, capacity=60)).allowed

        # 60 per minute is one per second.
        clock.advance(999)
        assert not (await bucket.take(name, capacity=60)).allowed
        clock.advance(1)
        assert (await bucket.take(name, capacity=60)).allowed
        assert not (await bucket.take(name, capacity=60)).allowed

    run(test)


def test_bucket_never_holds_more_than_its_capacity():
    async def test(bucket, name, clock, redis):
        await bucket.take(name, capacity=5)
        clock.advance(10 * MINUTE_MS)

        results = [await bucket.take(name, capacity=5) for _ in range(6)]

        assert sum(r.allowed for r in results) == 5

    run(test)


def test_denied_take_takes_nothing():
    async def test(bucket, name, clock, redis):
        await bucket.take(name, capacity=10, cost=8)

        denied = await bucket.take(name, capacity=10, cost=5)
        allowed = await bucket.take(name, capacity=10, cost=2)

        assert not denied.allowed
        assert allowed.allowed
        assert allowed.remaining == 0

    run(test)


def test_retry_after_is_when_enough_tokens_are_back():
    async def test(bucket, name, clock, redis):
        await bucket.take(name, capacity=60, cost=60)

        denied = await bucket.take(name, capacity=60, cost=3)

        assert denied.retry_after_ms == 3_000
        clock.advance(denied.retry_after_ms)
        assert (await bucket.take(name, capacity=60, cost=3)).allowed

    run(test)


def test_cost_above_capacity_can_never_pass():
    async def test(bucket, name, clock, redis):
        result = await bucket.take(name, capacity=10, cost=11)

        assert not result.allowed
        assert result.retry_after_ms is None
        assert result.remaining == 10

    run(test)


def test_reset_is_when_the_bucket_is_full_again():
    async def test(bucket, name, clock, redis):
        result = await bucket.take(name, capacity=60, cost=30)

        assert result.reset_ms == 30_000
        assert 0 < await redis.pttl(KEY_PREFIX + name) <= 30_000

    run(test)


def test_lowering_the_capacity_caps_the_tokens():
    async def test(bucket, name, clock, redis):
        await bucket.take(name, capacity=100)

        results = [await bucket.take(name, capacity=3) for _ in range(4)]

        assert [r.allowed for r in results] == [True, True, True, False]

    run(test)


def test_clock_going_backwards_refills_nothing():
    async def test(bucket, name, clock, redis):
        await bucket.take(name, capacity=2, cost=2)
        clock.advance(-MINUTE_MS)

        assert not (await bucket.take(name, capacity=2)).allowed

        # Back at the original time: still nothing refilled.
        clock.advance(MINUTE_MS)
        assert not (await bucket.take(name, capacity=2)).allowed

    run(test)


def test_buckets_are_separate():
    async def test(bucket, name, clock, redis):
        other = f"{name}:other"
        try:
            await bucket.take(name, capacity=1)

            assert (await bucket.take(other, capacity=1)).allowed
        finally:
            await redis.delete(KEY_PREFIX + other)

    run(test)


@pytest.mark.parametrize(("capacity", "cost"), [(0, 1), (5, -1)])
def test_invalid_capacity_or_cost_is_refused(capacity: int, cost: int):
    async def test(bucket, name, clock, redis):
        with pytest.raises(ValueError):
            await bucket.take(name, capacity=capacity, cost=cost)

    run(test)
