"""
BUG-0077 — the public receipt page's rate limit must defend against
enumeration, i.e. be scoped per client IP, not per (IP, token).

ROOT CAUSE (re-verified against the installed slowapi 0.1.10, not assumed):
Limiter._check_request_limit computes `endpoint_url = request["path"]` and
__evaluate_limits then uses `limit_scope = lim.scope or endpoint`. The
bucket is therefore (key_func(request), scope) = (client IP, CONCRETE path).
Because the token IS the path, every distinct token got its own independent
30/minute budget, so a single IP could hammer unlimited different
(mostly-404) tokens without ever being throttled — exactly the traffic shape
of a token-enumeration attempt or a cheap DB-load DoS.

Note that a custom `key_func` alone cannot fix this: slowapi 0.1.10's
`Limiter.limit()` takes no `scope` argument at all, so the scope would stay
path-derived no matter what the key function returned. `shared_limit()`,
which does take an explicit scope, is the mechanism that actually works, and
it changes nothing for any other route.

These tests do not need a database: the route's DB dependency is overridden
with a stub that finds no receipt (every token 404s), which is precisely the
enumeration traffic in question. They do need a real Redis, since that is
the limiter's real storage.
"""
from __future__ import annotations

import secrets
import uuid

import pytest
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.testclient import TestClient

from backend.app.api.v1.routes_receipt_public import router as receipt_router
from backend.app.core.config import get_settings
from backend.app.core.ratelimit import limiter
from backend.app.db.session import get_db

settings = get_settings()
LIMIT = settings.RECEIPT_PUBLIC_RATE_LIMIT_PER_MINUTE


class _StubResult:
    def scalar_one_or_none(self):
        return None


class _StubSession:
    """Finds nothing — every token is a 404, the enumeration case."""

    async def execute(self, *_args, **_kwargs):
        return _StubResult()


async def _stub_db():
    yield _StubSession()


class _ClientIPMiddleware:
    """
    Pure-ASGI shim letting a test choose the client IP slowapi's
    get_remote_address() will see (it reads request.client.host directly).
    Starlette's TestClient hard-codes one client address, so without this
    every test would share a single rate-limit bucket.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            ip = headers.get(b"x-test-client-ip")
            if ip:
                scope = dict(scope)
                scope["client"] = (ip.decode(), 12345)
        await self.app(scope, receive, send)


def _build_client() -> TestClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(receipt_router, prefix="/v1")
    app.dependency_overrides[get_db] = _stub_db
    return TestClient(_ClientIPMiddleware(app))


def _unique_ip() -> str:
    """
    A fresh IP per test: the whole point of the fix is that one IP now maps
    to ONE bucket, so a fixed IP would carry state across runs within the
    limiter's 1-minute window.
    """
    n = uuid.uuid4().int
    return f"10.{n % 251}.{(n >> 8) % 251}.{(n >> 16) % 251}"


def _token() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def rate_limited():
    """Enable the limiter (conftest sets RATE_LIMIT_ENABLED=false) and restore after."""
    # limits' storage.check() RETURNS False when the backend is unreachable
    # (it does not raise), so it must be tested as a value — an earlier draft
    # that only wrapped it in try/except never skipped and failed instead.
    reachable = False
    try:
        reachable = bool(limiter._storage.check())
    except Exception:
        reachable = False
    if not reachable:
        pytest.skip("Real Redis not reachable at REDIS_URL — the limiter's real storage.")
    previous = limiter.enabled
    limiter.enabled = True
    try:
        yield
    finally:
        limiter.enabled = previous


class TestEnumerationIsThrottled:
    def test_many_different_tokens_from_one_ip_share_one_bucket(self, rate_limited):
        """
        The headline defect: before the fix, every one of these requests used
        a different token and therefore a different bucket, so none of them
        was ever throttled.
        """
        client = _build_client()
        ip = _unique_ip()
        headers = {"X-Test-Client-IP": ip}

        statuses = []
        for _ in range(LIMIT + 5):
            resp = client.get(f"/v1/r/{_token()}", headers=headers)
            statuses.append(resp.status_code)

        assert 429 in statuses, (
            f"{LIMIT + 5} requests to {LIMIT + 5} DIFFERENT tokens from one IP were "
            f"never throttled — the rate limit is still scoped per token. "
            f"statuses={statuses}"
        )
        # The first LIMIT requests are served (404, unknown token); the
        # remainder trip the shared bucket.
        assert statuses[:LIMIT] == [404] * LIMIT
        assert statuses[LIMIT] == 429

    def test_a_different_ip_is_unaffected(self, rate_limited):
        """Throttling one source must not deny service to everyone else."""
        client = _build_client()
        attacker = {"X-Test-Client-IP": _unique_ip()}
        bystander = {"X-Test-Client-IP": _unique_ip()}

        for _ in range(LIMIT + 2):
            client.get(f"/v1/r/{_token()}", headers=attacker)
        assert client.get(f"/v1/r/{_token()}", headers=attacker).status_code == 429

        resp = client.get(f"/v1/r/{_token()}", headers=bystander)
        assert resp.status_code == 404, "an unrelated client was caught by another IP's limit"

    def test_repeated_requests_to_the_same_token_still_throttle(self, rate_limited):
        """The original per-token protection must not be lost."""
        client = _build_client()
        headers = {"X-Test-Client-IP": _unique_ip()}
        token = _token()

        statuses = [client.get(f"/v1/r/{token}", headers=headers).status_code
                    for _ in range(LIMIT + 2)]
        assert statuses[LIMIT] == 429


class TestLimiterKeyShape:
    def test_the_limiter_key_no_longer_embeds_the_token(self, rate_limited):
        """
        Direct evidence, mirroring the gate finding's own
        `redis-cli KEYS "*LIMIT*"` output: the bucket key used to contain the
        raw receipt token (a second, quieter instance of BUG-0076's leak —
        the token was sitting in Redis in cleartext).
        """
        client = _build_client()
        ip = _unique_ip()
        token = _token()
        client.get(f"/v1/r/{token}", headers={"X-Test-Client-IP": ip})

        storage = limiter._storage
        keys = [k.decode() if isinstance(k, bytes) else k
                for k in storage.storage.scan_iter(match="*")]
        ours = [k for k in keys if ip in k]
        assert ours, f"no limiter key recorded for {ip}: {keys[:10]}"
        for key in ours:
            assert token not in key, f"receipt token is embedded in a limiter key: {key}"
            assert "public_receipt" in key, (
                f"limiter key is not using the fixed shared scope: {key}"
            )


class TestOtherRoutesUnaffected:
    def test_only_the_public_receipt_route_uses_a_fixed_scope(self):
        """
        The fix must not change bucketing anywhere else: every other
        rate-limited route keeps slowapi's default path-derived scope (an
        empty lim.scope). Imports the whole app first so that every
        @limiter-decorated route in the codebase is actually registered —
        otherwise this asserts over whichever modules happened to be
        imported and proves nothing.
        """
        import backend.app.main  # noqa: F401 — registers every route

        scoped = {
            name: lim.scope
            for name, limits in limiter._route_limits.items()
            for lim in limits
            if lim.scope
        }
        assert len(limiter._route_limits) > 1, "no other rate-limited routes registered"
        assert len(scoped) == 1, f"expected exactly one fixed-scope route, got {scoped}"

        route_name, scope = next(iter(scoped.items()))
        assert "receipt_public" in route_name, route_name
        assert scope == "public_receipt"
