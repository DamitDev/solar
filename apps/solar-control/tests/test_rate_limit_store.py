"""Tests for the per-key Redis rate limit store."""

from unittest.mock import patch

import pytest

from app.redis_state.rate_limit import RateLimitStore


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self.redis = redis
        self.key = ""

    def incr(self, key: str) -> None:
        self.key = key
        self.redis.counts[key] = self.redis.counts.get(key, 0) + 1
        self.redis.last_key = key

    def expire(self, key: str, ttl: int) -> None:
        self.redis.ttls[key] = ttl

    async def execute(self) -> list[object]:
        return [self.redis.counts[self.redis.last_key], True]


class FakeRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.last_key = ""

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


@pytest.mark.anyio
async def test_hit_allows_under_the_limit():
    store = RateLimitStore()
    fake = FakeRedis()
    with patch("app.redis_state.rate_limit.redis_client", return_value=fake):
        allowed, retry_after = await store.hit("key-1", 3)
    assert allowed is True
    assert retry_after == 0
    assert list(fake.ttls.values()) == [120]


@pytest.mark.anyio
async def test_hit_blocks_over_the_limit_with_retry_after():
    store = RateLimitStore()
    fake = FakeRedis()
    with patch("app.redis_state.rate_limit.redis_client", return_value=fake):
        results = [await store.hit("key-1", 2) for _ in range(3)]
    assert [result[0] for result in results] == [True, True, False]
    assert results[-1][1] >= 1


@pytest.mark.anyio
async def test_hit_is_per_key():
    store = RateLimitStore()
    fake = FakeRedis()
    with patch("app.redis_state.rate_limit.redis_client", return_value=fake):
        first, _ = await store.hit("key-1", 1)
        second, _ = await store.hit("key-2", 1)
    assert first is True
    assert second is True


@pytest.mark.anyio
async def test_hit_uses_a_fixed_minute_window():
    store = RateLimitStore()
    fake = FakeRedis()
    with (
        patch("app.redis_state.rate_limit.redis_client", return_value=fake),
        patch("app.redis_state.rate_limit.time.time", return_value=120.5),
    ):
        await store.hit("key-1", 5)
    assert fake.last_key == "solar:ratelimit:key-1:2"
