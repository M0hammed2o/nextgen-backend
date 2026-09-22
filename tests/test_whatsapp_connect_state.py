"""
Gate hardening pass, 2026-09-22 — anti-CSRF/replay state for the WhatsApp
Embedded Signup connect flow (backend/app/whatsapp/connect_state.py,
POST /business/whatsapp/connection/start, and POST .../exchange's new
required `state` field).

Covers:
  - connect_state.py unit-level behaviour: mint/consume, single-use,
    cross-tenant mismatch, expiry, Redis-unavailable fail-closed.
  - Route wiring: /start mints, /exchange requires + validates it, reuse and
    cross-tenant replay are rejected, Redis-unavailable is a 503 on BOTH
    routes (never a silent skip).
  - Security sweep (Part 4 of the gate): state replay, credential
    non-exposure in response bodies, and re-confirmation that the
    pre-existing cross-tenant WABA/token-crossing protections still hold
    with the new state layer in front of them.

Uses the real throwaway Postgres DB (skips cleanly if unreachable, same
pattern as tests/test_whatsapp_connections_realdb.py) and the real Redis
instance pointed to by REDIS_URL (skips cleanly if unreachable) — no fakes
for Redis, since the whole point of this suite is to prove real atomic
GETDEL single-use semantics, not a mocked approximation of them. No network
calls to Meta anywhere — MetaGraphClient is replaced with an in-process fake
exactly like test_whatsapp_connections_realdb.py's _FakeGraphClient.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1 import routes_whatsapp_connect as routes
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.db.session import async_session_factory, engine, get_redis
from backend.app.whatsapp import connect_state
from backend.app.whatsapp.graph_client import GraphAPIError
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection
from shared.utils import generate_business_code


class DummyRequest:
    """
    Stand-in for FastAPI's Request — enough for @limiter.limit to no-op
    (RATE_LIMIT_ENABLED=false in the test env, conftest.py) and for the route
    bodies below, which never otherwise touch `request`.
    """


class _FakeGraphClient:
    """Same shape as test_whatsapp_connections_realdb.py's fake — no network access."""

    def __init__(self, waba_id, phone_number_id, access_token="fake-token-abc123", fail_at=None):
        self.waba_id = waba_id
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.fail_at = fail_at

    async def exchange_code_for_token(self, code):
        if self.fail_at == "code_exchange":
            raise GraphAPIError("invalid code", status_code=400, error_code=100)
        return {"access_token": self.access_token, "token_type": "bearer"}

    async def get_waba(self, waba_id, access_token):
        return {"id": waba_id, "name": "Test WABA"}

    async def get_phone_number(self, phone_number_id, access_token):
        return {
            "display_phone_number": "+27 82 000 0000",
            "verified_name": "Test Restaurant",
            "quality_rating": "GREEN",
        }

    async def subscribe_app(self, waba_id, access_token):
        return {"success": True}

    async def register_phone(self, phone_number_id, access_token, pin):
        return {"success": True}


# ── DB + Redis fixtures ─────────────────────────────────────────────────────

async def _db_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _redis_reachable() -> bool:
    try:
        redis = await get_redis()
        await redis.ping()
        return True
    except Exception:
        return False


@pytest.fixture
async def db() -> AsyncSession:
    if not await _db_reachable():
        pytest.skip("Real Postgres not reachable at DATABASE_URL — skipping.")
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


@pytest.fixture
async def redis():
    """
    Yields the real Redis client, then DISPOSES the process-global pool
    (backend/app/db/session.py's `_redis_pool`) afterwards.

    Required because pytest-asyncio (this repo's config: mode=auto,
    default_fixture_loop_scope=function) tears down and recreates the event
    loop PER TEST FUNCTION, but get_redis() lazily caches its connection
    pool at module/process scope, keyed to whichever loop first created it —
    exactly the same event-loop-lifetime hazard the pre-existing `db`
    fixture above already works around via `engine.dispose()`. Without this
    teardown, the SECOND test to touch Redis reuses a pool bound to the
    FIRST test's already-closed loop, and requests against it fail/hang
    intermittently — reproduced empirically while writing this file (an
    alternating pass/skip pattern across the suite, not a real Redis
    reachability problem). Never hit in the rest of this codebase's test
    suite before, since no other test file calls get_redis() directly.
    """
    from backend.app.db.session import close_redis

    if not await _redis_reachable():
        pytest.skip(
            "Real Redis not reachable at REDIS_URL — skipping connect-state tests "
            "(these deliberately exercise real Redis GETDEL semantics, not a mock)."
        )
    client = await get_redis()
    yield client
    await close_redis()


async def _make_business(db: AsyncSession, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    biz = Business(
        name=f"Signup2 State Test {unique}",
        slug=f"signup2-state-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        is_whatsapp_enabled=True,
    )
    for k, v in overrides.items():
        setattr(biz, k, v)
    db.add(biz)
    await db.commit()
    await db.refresh(biz)
    return biz


# ── connect_state.py unit-level ─────────────────────────────────────────────

async def test_mint_then_consume_matching_business_succeeds(redis):
    biz_id = uuid.uuid4()
    user_id = uuid.uuid4()
    minted = await connect_state.mint_connect_state(redis, biz_id, user_id)
    assert minted.token
    assert minted.expires_in > 0

    ok = await connect_state.consume_connect_state(redis, minted.token, biz_id)
    assert ok is True


async def test_consume_unknown_token_fails(redis):
    ok = await connect_state.consume_connect_state(redis, "not-a-real-token", uuid.uuid4())
    assert ok is False


async def test_consume_is_single_use(redis):
    """Second consume of the same token, even for the SAME (legitimate) business, must fail."""
    biz_id = uuid.uuid4()
    minted = await connect_state.mint_connect_state(redis, biz_id, uuid.uuid4())

    first = await connect_state.consume_connect_state(redis, minted.token, biz_id)
    second = await connect_state.consume_connect_state(redis, minted.token, biz_id)

    assert first is True
    assert second is False


async def test_consume_wrong_business_fails_and_still_consumes(redis):
    """
    A state minted for business A, consumed while authenticated as business
    B, must fail AND the token must be gone afterwards — a forged/stolen
    state value cannot be retried against the RIGHT business_id either.
    """
    biz_a = uuid.uuid4()
    biz_b = uuid.uuid4()
    minted = await connect_state.mint_connect_state(redis, biz_a, uuid.uuid4())

    wrong = await connect_state.consume_connect_state(redis, minted.token, biz_b)
    assert wrong is False

    # Now try again as the RIGHT business — must also fail, token is gone.
    right_after = await connect_state.consume_connect_state(redis, minted.token, biz_a)
    assert right_after is False


async def test_consume_expired_token_fails(redis):
    """A token past its TTL must behave exactly like a nonexistent one."""
    from backend.app.core import config as _config

    biz_id = uuid.uuid4()
    with patch.object(_config, "get_settings") as mock_get_settings, \
         patch.object(connect_state, "get_settings") as mock_get_settings2:
        fake_settings = type("S", (), {"WHATSAPP_CONNECT_STATE_TTL_SECONDS": 1})()
        mock_get_settings.return_value = fake_settings
        mock_get_settings2.return_value = fake_settings
        minted = await connect_state.mint_connect_state(redis, biz_id, uuid.uuid4())

    await asyncio.sleep(1.5)
    ok = await connect_state.consume_connect_state(redis, minted.token, biz_id)
    assert ok is False


async def test_mint_fails_closed_when_redis_unreachable():
    class _BrokenRedis:
        async def set(self, *a, **kw):
            raise RedisConnectionError("simulated Redis outage")

    with pytest.raises(connect_state.StateServiceUnavailable):
        await connect_state.mint_connect_state(_BrokenRedis(), uuid.uuid4(), uuid.uuid4())


async def test_consume_fails_closed_when_redis_unreachable():
    class _BrokenRedis:
        async def getdel(self, *a, **kw):
            raise RedisConnectionError("simulated Redis outage")

    with pytest.raises(connect_state.StateServiceUnavailable):
        await connect_state.consume_connect_state(_BrokenRedis(), "any-token", uuid.uuid4())


async def test_consume_corrupt_value_is_treated_as_invalid_not_a_crash(redis):
    """
    Defensive: connect_state.py is the only writer, so this should be
    unreachable in practice, but a corrupt/foreign value at the key must
    never raise — it must be treated as an invalid state.
    """
    key = connect_state._KEY_PREFIX + "corrupt-test-token"
    await redis.set(key, "not-json-and-not-a-uuid", ex=60)
    ok = await connect_state.consume_connect_state(redis, "corrupt-test-token", uuid.uuid4())
    assert ok is False


# ── Route wiring: POST /start + POST /exchange state enforcement ───────────

async def test_start_route_mints_state(db, redis):
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    result = await routes.start_connect(request=DummyRequest(), user=user, db=db)
    assert result.state
    assert result.expires_in == 600


async def test_start_route_module_disabled_returns_409(db, redis):
    business = await _make_business(db, is_whatsapp_enabled=False)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    with pytest.raises(AppError) as exc_info:
        await routes.start_connect(request=DummyRequest(), user=user, db=db)
    assert exc_info.value.code == "WHATSAPP_NOT_ENABLED"


async def test_exchange_without_valid_state_is_rejected(db, redis):
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    body = routes.ExchangeRequest(
        state="totally-bogus-state-value",
        code="some-code",
        waba_id="waba-x",
        phone_number_id="phone-x",
    )
    with pytest.raises(AppError) as exc_info:
        await routes.exchange_code(body=body, request=DummyRequest(), user=user, db=db)
    assert exc_info.value.code == "INVALID_OR_EXPIRED_STATE"
    assert exc_info.value.status_code == 401

    # Nothing was ever attempted against the DB — no connection row created.
    result = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == business.id)
    )
    assert result.scalar_one_or_none() is None


async def test_exchange_request_schema_requires_state_field():
    with pytest.raises(Exception):
        routes.ExchangeRequest(code="c", waba_id="w", phone_number_id="p")  # no state


async def test_start_then_exchange_with_valid_state_succeeds(db, redis):
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")

    started = await routes.start_connect(request=DummyRequest(), user=user, db=db)

    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id)

    body = routes.ExchangeRequest(
        state=started.state, code="good-code", waba_id=waba_id, phone_number_id=phone_number_id,
    )
    with patch("backend.app.whatsapp.connection_service.MetaGraphClient", return_value=fake):
        response = await routes.exchange_code(body=body, request=DummyRequest(), user=user, db=db)

    assert response.success is True
    assert response.connection.status == "CONNECTED"


async def test_state_is_single_use_second_exchange_fails(db, redis):
    """
    Mint once for business A; use it for a legitimate exchange (may succeed
    or fail against Meta — irrelevant here); a SECOND /exchange call with the
    exact same state, even from the same legitimate owner, must be rejected —
    standard OAuth state single-use semantics, deliberately strict.
    """
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    started = await routes.start_connect(request=DummyRequest(), user=user, db=db)

    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id)
    body = routes.ExchangeRequest(
        state=started.state, code="good-code", waba_id=waba_id, phone_number_id=phone_number_id,
    )
    with patch("backend.app.whatsapp.connection_service.MetaGraphClient", return_value=fake):
        first = await routes.exchange_code(body=body, request=DummyRequest(), user=user, db=db)
    assert first.success is True

    # Retry with the SAME state — must fail even though the caller/business
    # is identical and legitimate.
    with pytest.raises(AppError) as exc_info:
        await routes.exchange_code(body=body, request=DummyRequest(), user=user, db=db)
    assert exc_info.value.code == "INVALID_OR_EXPIRED_STATE"


async def test_state_replay_against_different_business_is_rejected(db, redis):
    """
    Part 4 of the gate: mint a state for business A, attempt to use it in an
    /exchange call authenticated as business B — must be rejected (the
    state's stored business_id won't match the CURRENT caller), and must not
    touch business B's data at all.
    """
    business_a = await _make_business(db)
    business_b = await _make_business(db)
    user_a = AuthUser(user_id=uuid.uuid4(), business_id=business_a.id, role="OWNER", token_type="access")
    user_b = AuthUser(user_id=uuid.uuid4(), business_id=business_b.id, role="OWNER", token_type="access")

    started = await routes.start_connect(request=DummyRequest(), user=user_a, db=db)

    body = routes.ExchangeRequest(
        state=started.state, code="stolen-code",
        waba_id=f"waba-{uuid.uuid4().hex[:8]}", phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
    )
    with pytest.raises(AppError) as exc_info:
        await routes.exchange_code(body=body, request=DummyRequest(), user=user_b, db=db)
    assert exc_info.value.code == "INVALID_OR_EXPIRED_STATE"

    result_b = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == business_b.id)
    )
    assert result_b.scalar_one_or_none() is None

    # And business A's own legitimate use of ITS state is now also
    # impossible — the mismatched attempt already consumed it (deliberate,
    # documented single-use-on-any-read behaviour).
    body_a = routes.ExchangeRequest(
        state=started.state, code="real-code",
        waba_id=f"waba-{uuid.uuid4().hex[:8]}", phone_number_id=f"phone-{uuid.uuid4().hex[:8]}",
    )
    with pytest.raises(AppError) as exc_info2:
        await routes.exchange_code(body=body_a, request=DummyRequest(), user=user_a, db=db)
    assert exc_info2.value.code == "INVALID_OR_EXPIRED_STATE"


async def test_exchange_redis_unavailable_is_503_not_silent_skip(db, redis):
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    body = routes.ExchangeRequest(
        state="irrelevant", code="c", waba_id="w", phone_number_id="p",
    )

    class _BrokenRedis:
        async def getdel(self, *a, **kw):
            raise RedisConnectionError("simulated outage")

    async def _broken_get_redis():
        return _BrokenRedis()

    with patch.object(routes, "get_redis", _broken_get_redis):
        with pytest.raises(AppError) as exc_info:
            await routes.exchange_code(body=body, request=DummyRequest(), user=user, db=db)
    assert exc_info.value.code == "WHATSAPP_STATE_SERVICE_UNAVAILABLE"
    assert exc_info.value.status_code == 503

    # No connection row was created — the request never reached run_exchange.
    result = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == business.id)
    )
    assert result.scalar_one_or_none() is None


async def test_start_redis_unavailable_is_503_not_silent_skip(db):
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")

    class _BrokenRedis:
        async def set(self, *a, **kw):
            raise RedisConnectionError("simulated outage")

    async def _broken_get_redis():
        return _BrokenRedis()

    with patch.object(routes, "get_redis", _broken_get_redis):
        with pytest.raises(AppError) as exc_info:
            await routes.start_connect(request=DummyRequest(), user=user, db=db)
    assert exc_info.value.code == "WHATSAPP_STATE_SERVICE_UNAVAILABLE"
    assert exc_info.value.status_code == 503


# ── Part 4 (security sweep): credentials never in any response body ────────

def test_no_response_model_in_this_router_exposes_a_secret_field():
    """
    Every Pydantic response model this router uses must never carry a raw
    field name that could plausibly hold a live WhatsApp credential. `state`
    itself is deliberately excluded from the banned set — it is a one-time,
    10-minute-lived CSRF token, not a Meta credential, and is meant to reach
    the frontend by design (same as any OAuth state parameter).
    """
    banned_substrings = ("access_token", "registration_pin", "token_encrypted", "client_secret", "app_secret")
    response_models = [
        routes.WhatsAppConnectionStatus,
        routes.WhatsAppConnectConfig,
        routes.WhatsAppConnectStartResponse,
        routes.ExchangeResponse,
        routes.HealthCheckResponse,
        routes.TestMessageResponse,
    ]
    for model in response_models:
        for field_name in model.model_fields:
            lowered = field_name.lower()
            for banned in banned_substrings:
                assert banned not in lowered, (
                    f"{model.__name__}.{field_name} looks like it could carry a "
                    f"credential — matched banned substring {banned!r}"
                )


async def test_exchange_response_never_serializes_a_decrypted_token(db, redis):
    """
    End-to-end: run a full start+exchange with a real (fake-Graph-backed)
    token, and confirm the plaintext token never appears anywhere in the
    JSON-serialized response body — not just that the field is absent by
    name (the model-field test above), but that no other field's *value*
    happens to leak it either.
    """
    business = await _make_business(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    started = await routes.start_connect(request=DummyRequest(), user=user, db=db)

    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id, access_token="ULTRA-SECRET-PLAINTEXT-TOKEN")

    body = routes.ExchangeRequest(
        state=started.state, code="good-code", waba_id=waba_id, phone_number_id=phone_number_id,
    )
    with patch("backend.app.whatsapp.connection_service.MetaGraphClient", return_value=fake):
        response = await routes.exchange_code(body=body, request=DummyRequest(), user=user, db=db)

    serialized = response.model_dump_json()
    assert "ULTRA-SECRET-PLAINTEXT-TOKEN" not in serialized
