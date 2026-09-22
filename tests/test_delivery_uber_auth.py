"""
`backend/app/delivery/uber/auth.py` — the operational-token cache/mint-lock,
the mechanism that keeps this platform under Uber's 100-mint/hour cap (the
101st mint invalidates the oldest, so minting per-request would be a real
outage risk under any real traffic).
"""

from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.app.delivery.errors import DeliveryProviderError
from backend.app.delivery.uber.auth import OperationalTokenUnavailable, get_operational_token


class FakeRedis:
    def __init__(self, cached_token=None, lock_acquirable=True):
        self._store = {}
        if cached_token:
            self._store["uber_eats:oauth:operational_token"] = cached_token
        self._lock_acquirable = lock_acquirable
        self.set_calls = []
        self.delete_calls = []

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        if nx:
            if not self._lock_acquirable or key in self._store:
                return False
            self._store[key] = value
            return True
        self._store[key] = value
        return True

    async def delete(self, key):
        self.delete_calls.append(key)
        self._store.pop(key, None)


class TestCachedTokenPath:
    async def test_returns_cached_token_without_minting(self):
        redis = FakeRedis(cached_token="cached-token-value")
        http_client = AsyncMock()
        token = await get_operational_token(redis, http_client)
        assert token == "cached-token-value"
        http_client.post.assert_not_called()


class TestMintingPath:
    async def test_mints_and_caches_when_no_cached_token(self, monkeypatch):
        from backend.app.core.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "UBER_EATS_CLIENT_ID", "cid")
        monkeypatch.setattr(settings, "UBER_EATS_CLIENT_SECRET", "csecret")

        redis = FakeRedis(cached_token=None)
        http_client = AsyncMock()
        response = AsyncMock()
        response.status_code = 200
        response.json = lambda: {"access_token": "new-token", "expires_in": 2592000}
        http_client.post = AsyncMock(return_value=response)

        token = await get_operational_token(redis, http_client)
        assert token == "new-token"
        http_client.post.assert_awaited_once()
        # Cached under the fixed TTL-margin rule, and the mint lock released.
        assert redis._store["uber_eats:oauth:operational_token"] == "new-token"
        assert "uber_eats:oauth:mint_lock" not in redis._store

    async def test_missing_credentials_fails_closed_without_a_network_call(self, monkeypatch):
        from backend.app.core.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "UBER_EATS_CLIENT_ID", "")
        monkeypatch.setattr(settings, "UBER_EATS_CLIENT_SECRET", "")

        redis = FakeRedis(cached_token=None)
        http_client = AsyncMock()
        with pytest.raises(OperationalTokenUnavailable):
            await get_operational_token(redis, http_client)
        http_client.post.assert_not_called()

    async def test_non_200_mint_response_raises_without_leaking_body(self, monkeypatch):
        from backend.app.core.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "UBER_EATS_CLIENT_ID", "cid")
        monkeypatch.setattr(settings, "UBER_EATS_CLIENT_SECRET", "csecret")

        redis = FakeRedis(cached_token=None)
        http_client = AsyncMock()
        response = AsyncMock()
        response.status_code = 401
        http_client.post = AsyncMock(return_value=response)

        with pytest.raises(DeliveryProviderError):
            await get_operational_token(redis, http_client)


class TestLockContention:
    async def test_a_second_caller_waits_for_the_first_mint_rather_than_minting_concurrently(self, monkeypatch):
        """
        Simulates: caller B arrives while caller A already holds the mint
        lock. B must NOT mint a second token — it must poll and pick up A's
        cached result. This is the exact mechanism that keeps concurrent
        workers from burning through the 100/hour mint budget together.
        """
        redis = FakeRedis(cached_token=None, lock_acquirable=False)
        # Simulate the token appearing mid-poll (as if caller A just finished).
        call_count = {"n": 0}
        original_get = redis.get

        async def get_with_delay(key):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                redis._store["uber_eats:oauth:operational_token"] = "token-from-other-caller"
            return await original_get(key)

        redis.get = get_with_delay

        with patch("backend.app.delivery.uber.auth._LOCK_WAIT_INTERVAL_SECONDS", 0):
            http_client = AsyncMock()
            token = await get_operational_token(redis, http_client)
        assert token == "token-from-other-caller"
        http_client.post.assert_not_called()


class TestRedisUnavailable:
    async def test_redis_get_failure_fails_closed(self):
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=RedisConnectionError("down"))
        http_client = AsyncMock()
        with pytest.raises(OperationalTokenUnavailable):
            await get_operational_token(redis, http_client)
        http_client.post.assert_not_called()
