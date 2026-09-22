"""
BUG-0074 — a cross-tenant WABA claim must return the documented 409 and
leave an audit trail, not crash with MissingGreenlet (500).

When business B completes an Embedded Signup exchange claiming a waba_id
already held by business A, run_exchange correctly hits the partial UNIQUE
index from migration 0037 and calls db.rollback(). That rollback expires
EVERY ORM instance in the session's identity map — including the `business`
instance the ROUTE still holds — so the route's next attribute read
(business.id, while building its AuditEvent) triggers a lazy reload outside
greenlet context and raises sqlalchemy.exc.MissingGreenlet. The owner sees
"An unexpected error occurred" and, because the audit write is the thing
that crashed, no audit row is written for the rejected attempt at all.

Tenant isolation itself was never in question here (A's connection is
untouched) — these tests pin the rejection PATH: clean 409, A untouched,
audit row present.

Real Postgres + real Redis (the route consumes a real connect-state token).
Skips cleanly if either is unreachable:

    ENVIRONMENT=test
    DATABASE_URL=postgresql+asyncpg://nextgen:...@localhost:55444/nextgen_test_gatefix
    REDIS_URL=redis://localhost:56999/0
    python -m pytest tests/test_whatsapp_waba_hijack_realdb.py -q

No network call is made — MetaGraphClient is replaced with the same kind of
deterministic in-process fake used by tests/test_whatsapp_connections_realdb.py.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select, text

from backend.app.api.v1.routes_whatsapp_connect import ExchangeRequest, exchange_code
from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser
from backend.app.db.session import async_session_factory, engine, get_redis
from backend.app.whatsapp import connection_service
from backend.app.whatsapp.connect_state import mint_connect_state
from backend.app.whatsapp.graph_client import GraphAPIError
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection
from shared.utils import generate_business_code


class _FakeGraphClient:
    """Deterministic stand-in for MetaGraphClient — never touches the network."""

    def __init__(self, waba_id, phone_number_id, access_token="fake-token-abc123"):
        self.waba_id = waba_id
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.calls: list[str] = []

    async def exchange_code_for_token(self, code):
        self.calls.append("exchange_code_for_token")
        return {"access_token": self.access_token, "token_type": "bearer"}

    async def get_waba(self, waba_id, access_token):
        self.calls.append("get_waba")
        return {"id": waba_id, "name": "Test WABA"}

    async def get_phone_number(self, phone_number_id, access_token):
        self.calls.append("get_phone_number")
        return {
            "display_phone_number": "+27 82 000 0000",
            "verified_name": "Test Restaurant",
            "quality_rating": "GREEN",
            "code_verification_status": "VERIFIED",
        }

    async def subscribe_app(self, waba_id, access_token):
        self.calls.append("subscribe_app")
        return {"success": True}

    async def register_phone(self, phone_number_id, access_token, pin):
        self.calls.append("register_phone")
        return {"success": True}

    async def unsubscribe_app(self, waba_id, access_token):
        self.calls.append("unsubscribe_app")
        return {"success": True}


# ── Fixtures ────────────────────────────────────────────────────────────────

async def _db_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


@pytest.fixture
async def db():
    if not await _db_reachable():
        pytest.skip("Real Postgres not reachable at DATABASE_URL — see file docstring.")
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


@pytest.fixture
async def redis():
    """
    Real Redis client, then DISPOSES the process-global pool afterwards —
    same event-loop-lifetime workaround (and for the same reason) as the
    `redis` fixture in tests/test_whatsapp_connect_state.py: get_redis()
    caches its pool at process scope keyed to whichever event loop created
    it, while pytest-asyncio gives every test function a fresh loop.
    """
    from backend.app.db.session import close_redis

    try:
        client = await get_redis()
        await client.ping()
    except Exception:
        await close_redis()
        pytest.skip("Real Redis not reachable at REDIS_URL — see file docstring.")
    yield client
    await close_redis()


async def _make_business(db) -> Business:
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"Hijack Test {unique}",
        slug=f"hijack-test-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        is_whatsapp_enabled=True,
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


async def _cleanup(db, *business_ids):
    """
    Takes PLAIN UUIDs, never ORM instances: run_exchange's conflict path
    rolls back, which expires every instance in the identity map, so reading
    `business.id` in here would itself raise MissingGreenlet — the very
    footgun BUG-0074 is about.
    """
    await db.rollback()
    for business_id in business_ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_id))
        await db.execute(
            delete(WhatsAppConnection).where(WhatsAppConnection.business_id == business_id)
        )
        await db.execute(delete(Business).where(Business.id == business_id))
    await db.commit()


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(
        user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access",
    )


async def _attempt_hijack(db, redis, victim_waba, attacker):
    """
    Business B (`attacker`) runs a full, otherwise-valid Embedded Signup
    exchange claiming `victim_waba` — already held by A — with its own fresh
    phone_number_id. Returns whatever exchange_code() does.
    """
    minted = await mint_connect_state(redis, attacker.id, uuid.uuid4())
    attacker_phone = f"phone-b-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(victim_waba, attacker_phone)

    body = ExchangeRequest(
        state=minted.token,
        code="attacker-own-valid-code",
        waba_id=victim_waba,
        phone_number_id=attacker_phone,
    )
    with patch(
        "backend.app.whatsapp.connection_service.MetaGraphClient", return_value=fake
    ):
        return await exchange_code(
            body=body, request=None, user=_owner(attacker.id), db=db,
        )


# ── The hijack attempt ──────────────────────────────────────────────────────

class TestCrossTenantWabaClaim:
    async def test_hijack_returns_clean_409_not_500(self, db, redis):
        """
        The rejection path itself must not crash. Before the fix this raised
        sqlalchemy.exc.MissingGreenlet (an unhandled 500) from
        `business_id=business.id` inside the AuditEvent, because
        run_exchange's IntegrityError rollback had expired `business`.
        """
        victim = await _make_business(db)
        attacker = await _make_business(db)
        victim_id, attacker_id = victim.id, attacker.id
        try:
            victim_waba = f"waba-victim-{uuid.uuid4().hex[:8]}"
            victim_phone = f"phone-a-{uuid.uuid4().hex[:8]}"
            ok = await connection_service.run_exchange(
                db, victim,
                code="victim-code", waba_id=victim_waba, phone_number_id=victim_phone,
                connected_by_user_id=uuid.uuid4(),
                graph_client=_FakeGraphClient(victim_waba, victim_phone),
            )
            assert ok.success is True

            with pytest.raises(AppError) as exc_info:
                await _attempt_hijack(db, redis, victim_waba, attacker)

            assert exc_info.value.status_code == 409
            assert exc_info.value.code == "WHATSAPP_NUMBER_ALREADY_CONNECTED"
            assert exc_info.value.details == {"failed_step": "claim_routing_number"}
        finally:
            await _cleanup(db, victim_id, attacker_id)

    async def test_hijack_leaves_the_rightful_owner_completely_unchanged(self, db, redis):
        victim = await _make_business(db)
        attacker = await _make_business(db)
        victim_id, attacker_id = victim.id, attacker.id
        try:
            victim_waba = f"waba-victim-{uuid.uuid4().hex[:8]}"
            victim_phone = f"phone-a-{uuid.uuid4().hex[:8]}"
            await connection_service.run_exchange(
                db, victim,
                code="victim-code", waba_id=victim_waba, phone_number_id=victim_phone,
                connected_by_user_id=uuid.uuid4(),
                graph_client=_FakeGraphClient(victim_waba, victim_phone),
            )
            await db.refresh(victim)
            before = (
                victim.whatsapp_phone_number_id,
                victim.whatsapp_business_account_id,
            )
            victim_conn_before = (
                await db.execute(
                    select(
                        WhatsAppConnection.status,
                        WhatsAppConnection.waba_id,
                        WhatsAppConnection.phone_number_id,
                        WhatsAppConnection.access_token_encrypted,
                    ).where(WhatsAppConnection.business_id == victim_id)
                )
            ).one()

            with pytest.raises(AppError):
                await _attempt_hijack(db, redis, victim_waba, attacker)

            victim = (
                await db.execute(select(Business).where(Business.id == victim_id))
            ).scalar_one()
            assert (victim.whatsapp_phone_number_id, victim.whatsapp_business_account_id) == before

            victim_conn_after = (
                await db.execute(
                    select(
                        WhatsAppConnection.status,
                        WhatsAppConnection.waba_id,
                        WhatsAppConnection.phone_number_id,
                        WhatsAppConnection.access_token_encrypted,
                    ).where(WhatsAppConnection.business_id == victim_id)
                )
            ).one()
            assert victim_conn_after == victim_conn_before
            assert victim_conn_after.status == "CONNECTED"

            # The attacker is left visibly NEEDS_ATTENTION, holding nothing.
            attacker_conn = (
                await db.execute(
                    select(WhatsAppConnection).where(
                        WhatsAppConnection.business_id == attacker_id
                    )
                )
            ).scalar_one()
            assert attacker_conn.status == "NEEDS_ATTENTION"
            assert attacker_conn.waba_id != victim_waba
        finally:
            await _cleanup(db, victim_id, attacker_id)

    async def test_hijack_attempt_is_audited(self, db, redis):
        """
        The rejected attempt must leave a trail naming the attacking business
        and the waba_id it tried to take — and no token/secret.
        """
        victim = await _make_business(db)
        attacker = await _make_business(db)
        victim_id, attacker_id = victim.id, attacker.id
        try:
            victim_waba = f"waba-victim-{uuid.uuid4().hex[:8]}"
            victim_phone = f"phone-a-{uuid.uuid4().hex[:8]}"
            await connection_service.run_exchange(
                db, victim,
                code="victim-code", waba_id=victim_waba, phone_number_id=victim_phone,
                connected_by_user_id=uuid.uuid4(),
                graph_client=_FakeGraphClient(victim_waba, victim_phone),
            )

            with pytest.raises(AppError):
                await _attempt_hijack(db, redis, victim_waba, attacker)

            events = (
                await db.execute(
                    select(AuditEvent).where(
                        AuditEvent.business_id == attacker_id,
                        AuditEvent.action == "whatsapp.connection.connect_failed",
                    )
                )
            ).scalars().all()
            assert len(events) == 1, "rejected hijack attempt wrote no audit trail"

            event = events[0]
            assert event.diff_json["success"] is False
            assert event.diff_json["failed_step"] == "claim_routing_number"
            assert event.diff_json["waba_id"] == victim_waba
            assert event.created_at is not None

            # Never a credential.
            blob = str(event.diff_json)
            assert "fake-token-abc123" not in blob
            assert "access_token" not in blob
        finally:
            await _cleanup(db, victim_id, attacker_id)
