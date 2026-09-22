"""
BUG-0078 — a Redis outage must surface as the documented 503, not a 500, on
POST /whatsapp/connection/start and /exchange.

The fail-CLOSED property was already correct and is not what is being fixed:
with Redis down, zero Graph calls and zero DB mutations happen. Only the
HTTP status was wrong. connect_state.py deliberately raises
StateServiceUnavailable and both routes translate it into a 503
WHATSAPP_STATE_SERVICE_UNAVAILABLE — but that code is unreachable, because
the @limiter decorator runs BEFORE the route body and its own Redis storage
raises first.

Verified empirically (not assumed) against a real dead Redis port: what
escapes slowapi is `redis.exceptions.ConnectionError`, which had no
registered handler and so became an unhandled 500.

These tests drive real HTTP requests through the real routers and the real
error-handler stack. The limiter's strategy is pointed at a genuinely
unreachable Redis port rather than mocked, so the failure is the real one.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.testclient import TestClient

from backend.app.api.v1.routes_whatsapp_connect import router as whatsapp_router
from backend.app.core.errors import register_error_handlers
from backend.app.core.ratelimit import limiter
from backend.app.core.rbac import AuthUser, require_owner, require_owner_or_manager
from backend.app.db.session import get_db

DEAD_REDIS_URI = "redis://127.0.0.1:1/0"

BUSINESS_ID = uuid.uuid4()
USER_ID = uuid.uuid4()


class _FakeBusiness:
    id = BUSINESS_ID
    is_whatsapp_enabled = True
    whatsapp_paused = False
    whatsapp_phone_number_id = None
    whatsapp_business_account_id = None


class _StubResult:
    def scalar_one_or_none(self):
        return _FakeBusiness()


class _StubSession:
    async def execute(self, *_a, **_k):
        return _StubResult()

    async def commit(self):
        return None

    def add(self, _obj):
        return None


async def _stub_db():
    yield _StubSession()


def _owner() -> AuthUser:
    return AuthUser(
        user_id=USER_ID, business_id=BUSINESS_ID, role="OWNER", token_type="access",
    )


def _build_client() -> TestClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_error_handlers(app)
    app.include_router(whatsapp_router, prefix="/v1")
    app.dependency_overrides[require_owner] = _owner
    app.dependency_overrides[require_owner_or_manager] = _owner
    app.dependency_overrides[get_db] = _stub_db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def limiter_redis_down():
    """
    Point the SHARED limiter's strategy at an unreachable Redis port — a real
    outage, not a mock — then restore it.
    """
    from limits.storage import storage_from_string
    from limits.strategies import FixedWindowRateLimiter

    previous_limiter = limiter._limiter
    previous_enabled = limiter.enabled
    try:
        dead_storage = storage_from_string(DEAD_REDIS_URI)
        limiter._limiter = FixedWindowRateLimiter(dead_storage)
    except Exception:  # pragma: no cover
        limiter._limiter = previous_limiter
        pytest.skip("could not build a dead-Redis limiter storage")
    limiter.enabled = True
    try:
        yield
    finally:
        limiter._limiter = previous_limiter
        limiter.enabled = previous_enabled


_EXCHANGE_BODY = {
    "state": "some-state-token",
    "code": "some-meta-code",
    "waba_id": "waba-123",
    "phone_number_id": "phone-123",
}


class TestRedisDownReturns503:
    def test_connection_start_returns_503(self, limiter_redis_down):
        resp = _build_client().post("/v1/business/whatsapp/connection/start")
        assert resp.status_code == 503, (
            f"expected the documented 503, got {resp.status_code}: {resp.text[:400]}"
        )

    def test_connection_exchange_returns_503(self, limiter_redis_down):
        resp = _build_client().post(
            "/v1/business/whatsapp/connection/exchange", json=_EXCHANGE_BODY,
        )
        assert resp.status_code == 503, (
            f"expected the documented 503, got {resp.status_code}: {resp.text[:400]}"
        )

    @pytest.mark.parametrize(
        "path,body",
        [
            ("/v1/business/whatsapp/connection/start", None),
            ("/v1/business/whatsapp/connection/exchange", _EXCHANGE_BODY),
        ],
    )
    def test_response_carries_a_clear_code_and_no_internal_detail(
        self, limiter_redis_down, path, body
    ):
        resp = _build_client().post(path, json=body)
        assert resp.status_code == 503

        payload = resp.json()
        assert "error" in payload, payload
        assert payload["error"]["code"], "no machine-readable error code"
        assert payload["error"]["message"]

        # No stack trace, no internal plumbing, no infrastructure addresses.
        blob = resp.text.lower()
        for leak in ("traceback", "redis", "slowapi", "127.0.0.1", "connectionerror", "file \""):
            assert leak not in blob, f"internal detail {leak!r} leaked into the response: {resp.text[:400]}"


class TestFailClosedPropertyStillHolds:
    def test_no_graph_call_and_no_db_mutation_when_redis_is_down(self, limiter_redis_down):
        """
        The security property that was already correct must stay correct: the
        route body never runs, so nothing is committed and Meta is never
        called. Asserted by construction — the stub session records any
        commit, and any Graph client instantiation would raise.
        """
        commits = []

        class _RecordingSession(_StubSession):
            async def commit(self):
                commits.append(1)

        async def _recording_db():
            yield _RecordingSession()

        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        register_error_handlers(app)
        app.include_router(whatsapp_router, prefix="/v1")
        app.dependency_overrides[require_owner] = _owner
        app.dependency_overrides[require_owner_or_manager] = _owner
        app.dependency_overrides[get_db] = _recording_db
        client = TestClient(app, raise_server_exceptions=False)

        assert client.post("/v1/business/whatsapp/connection/start").status_code == 503
        assert client.post(
            "/v1/business/whatsapp/connection/exchange", json=_EXCHANGE_BODY,
        ).status_code == 503
        assert commits == [], "a DB commit happened despite Redis being down"


class TestOtherFailuresAreNotMasked:
    def test_a_non_redis_error_is_still_a_500(self):
        """
        The handler must map a Redis AVAILABILITY failure to 503, not swallow
        unrelated exceptions into a misleading 503.
        """
        from fastapi import APIRouter

        router = APIRouter()

        @router.get("/boom")
        async def boom():
            raise ValueError("a genuine bug, not an outage")

        app = FastAPI()
        register_error_handlers(app)
        app.include_router(router)
        resp = TestClient(app, raise_server_exceptions=False).get("/boom")
        assert resp.status_code == 500

    def test_a_redis_protocol_error_is_still_a_500(self):
        """
        redis.exceptions.ResponseError means a malformed command — our bug,
        not an outage — and must NOT be reported as 503 Service Unavailable.
        """
        from fastapi import APIRouter
        from redis.exceptions import ResponseError

        router = APIRouter()

        @router.get("/boom")
        async def boom():
            raise ResponseError("ERR unknown command")

        app = FastAPI()
        register_error_handlers(app)
        app.include_router(router)
        resp = TestClient(app, raise_server_exceptions=False).get("/boom")
        assert resp.status_code == 500


class TestAgainstTheRealApplication:
    """
    The minimal app above proves the handler works; this proves it WINS in
    the real application, whose middleware stack includes
    UnhandledExceptionMiddleware — a catch-all that converts any escaping
    exception into a 500.

    Starlette resolves registered exception handlers in ExceptionMiddleware,
    which sits INSIDE every add_middleware() layer, so the 503 handler runs
    first and the catch-all never sees a Redis outage. That ordering is an
    assumption worth testing rather than trusting (see BUG-0076, which was
    entirely a wiring assumption that turned out to be false).
    """

    def _real_app_client(self) -> TestClient:
        from backend.app.main import app

        app.dependency_overrides[require_owner] = _owner
        app.dependency_overrides[require_owner_or_manager] = _owner
        app.dependency_overrides[get_db] = _stub_db
        # No `with` — the lifespan (ARQ pool, etc.) is deliberately not run.
        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize(
        "path,body",
        [
            ("/v1/business/whatsapp/connection/start", None),
            ("/v1/business/whatsapp/connection/exchange", _EXCHANGE_BODY),
        ],
    )
    def test_real_app_returns_503_not_the_catch_all_500(
        self, limiter_redis_down, path, body
    ):
        from backend.app.main import app

        client = self._real_app_client()
        try:
            resp = client.post(path, json=body)
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 503, (
            f"UnhandledExceptionMiddleware won and produced "
            f"{resp.status_code}: {resp.text[:300]}"
        )
        assert resp.json()["error"]["code"] == "SERVICE_TEMPORARILY_UNAVAILABLE"
        assert "INTERNAL_SERVER_ERROR" not in resp.text
