"""
Gate hardening pass, 2026-09-22 — §2 failure-mode sweep: "/health-check and
/test-message: confirm both correctly reflect and update connection state on
failure (re-verify against real behaviour, don't assume last night's report
was exhaustive)."

Found: NEITHER connection_service.health_check() nor the /health-check or
/test-message ROUTES had any behavioural test coverage before this file —
tests/test_whatsapp_connect_routes.py only confirmed the RBAC dependency
wiring for these routes, never exercised them. This file closes that gap.

Real throwaway DB (skips cleanly if unreachable). No network calls — Meta is
always a deterministic in-process fake, either via health_check()'s own
graph_client override (function-level tests) or by patching MetaGraphClient
at the module the route actually constructs it in (route-level tests).
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1 import routes_whatsapp_connect as routes
from backend.app.core.rbac import AuthUser
from backend.app.db.session import async_session_factory, engine
from backend.app.whatsapp import connection_service
from backend.app.whatsapp.graph_client import GraphAPIError
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection
from shared.utils import generate_business_code


class DummyRequest:
    pass


class _FakeHealthGraphClient:
    def __init__(self, fail=None, error_code=None):
        self.fail = fail  # None | "190" | "other"
        self.error_code = error_code
        self.calls = []

    async def get_phone_number(self, phone_number_id, access_token):
        self.calls.append("get_phone_number")
        if self.fail == "190":
            raise GraphAPIError("token invalid", status_code=401, error_code=190)
        if self.fail == "other":
            raise GraphAPIError("server error", status_code=500, error_code=1)
        return {
            "display_phone_number": "+27 82 000 0000",
            "verified_name": "Healthy Restaurant",
            "quality_rating": "GREEN",
            "code_verification_status": "VERIFIED",
        }

    async def send_message(self, phone_number_id, access_token, payload):
        self.calls.append("send_message")
        if self.fail == "190":
            raise GraphAPIError("token invalid", status_code=401, error_code=190)
        if self.fail == "other":
            raise GraphAPIError("server error", status_code=500, error_code=1)
        return {"messages": [{"id": "wamid.test-msg"}]}


async def _db_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
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


async def _make_business_with_connection(db: AsyncSession, **overrides) -> tuple[Business, WhatsAppConnection]:
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"Health Test {unique}",
        slug=f"health-test-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        is_whatsapp_enabled=True,
    )
    db.add(business)
    await db.flush()

    defaults = dict(
        business_id=business.id,
        waba_id=f"waba-{unique}",
        phone_number_id=f"phone-{unique}",
        status="CONNECTED",
        # No "enc1:" prefix: CREDENTIALS_ENCRYPTION_KEY is unset in the test
        # env (conftest.py doesn't set it), so decrypt_credential() would
        # raise CredentialDecryptionError for a prefixed value with no key
        # configured. A bare value passes through unchanged (legacy-plaintext
        # path) — exactly matching the "development" behaviour documented in
        # backend/app/core/crypto.py.
        access_token_encrypted="plaintext-test-token",
    )
    defaults.update(overrides)
    connection = WhatsAppConnection(**defaults)
    db.add(connection)
    await db.commit()
    await db.refresh(business)
    await db.refresh(connection)
    return business, connection


# ── connection_service.health_check() — function-level state transitions ───

async def test_health_check_success_keeps_connected_and_updates_fields(db):
    business, connection = await _make_business_with_connection(db)
    fake = _FakeHealthGraphClient()

    result_conn, result = await connection_service.health_check(db, business, graph_client=fake)

    assert result["ok"] is True
    assert result_conn.status == "CONNECTED"
    assert result_conn.verified_name == "Healthy Restaurant"
    assert result_conn.last_health_check_at is not None


async def test_health_check_190_marks_needs_attention(db):
    business, connection = await _make_business_with_connection(db)
    fake = _FakeHealthGraphClient(fail="190")

    result_conn, result = await connection_service.health_check(db, business, graph_client=fake)

    assert result["ok"] is False
    assert result_conn.status == "NEEDS_ATTENTION"
    assert "error 190" in result_conn.status_reason
    assert result["technical_details"]["error_code"] == 190


async def test_health_check_generic_graph_error_marks_needs_attention(db):
    business, connection = await _make_business_with_connection(db)
    fake = _FakeHealthGraphClient(fail="other")

    result_conn, result = await connection_service.health_check(db, business, graph_client=fake)

    assert result["ok"] is False
    assert result_conn.status == "NEEDS_ATTENTION"
    assert "Health check failed" in result_conn.status_reason


async def test_health_check_self_heals_needs_attention_back_to_connected(db):
    """A subsequent SUCCESSFUL health-check on a NEEDS_ATTENTION connection restores CONNECTED."""
    business, connection = await _make_business_with_connection(db, status="NEEDS_ATTENTION", status_reason="stale")
    fake = _FakeHealthGraphClient()

    result_conn, result = await connection_service.health_check(db, business, graph_client=fake)

    assert result["ok"] is True
    assert result_conn.status == "CONNECTED"
    assert result_conn.status_reason is None


async def test_health_check_no_stored_token_marks_needs_attention_without_a_graph_call(db):
    business, connection = await _make_business_with_connection(db, access_token_encrypted=None)
    fake = _FakeHealthGraphClient()

    result_conn, result = await connection_service.health_check(db, business, graph_client=fake)

    assert result["ok"] is False
    assert result_conn.status == "NEEDS_ATTENTION"
    assert fake.calls == [], "must not call Meta at all with no stored token"


async def test_health_check_disconnected_is_a_noop_no_graph_call(db):
    business, connection = await _make_business_with_connection(db, status="DISCONNECTED", access_token_encrypted=None)
    fake = _FakeHealthGraphClient()

    result_conn, result = await connection_service.health_check(db, business, graph_client=fake)

    assert result["ok"] is False
    assert result_conn.status == "DISCONNECTED"
    assert fake.calls == []


# ── ROUTE level: run_health_check wires state + audit correctly ────────────

async def test_health_check_route_failure_writes_audit_event_and_reflects_status(db):
    business, connection = await _make_business_with_connection(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    fake = _FakeHealthGraphClient(fail="190")

    with patch("backend.app.whatsapp.connection_service.MetaGraphClient", return_value=fake):
        response = await routes.run_health_check(user=user, db=db)

    assert response.ok is False
    assert response.connection.status == "NEEDS_ATTENTION"

    from shared.models.audit import AuditEvent
    from sqlalchemy import select
    events = (await db.execute(
        select(AuditEvent).where(
            AuditEvent.business_id == business.id,
            AuditEvent.action == "whatsapp.connection.health_check_failed",
        )
    )).scalars().all()
    assert len(events) == 1


async def test_health_check_route_success_writes_no_failure_audit(db):
    business, connection = await _make_business_with_connection(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    fake = _FakeHealthGraphClient()

    with patch("backend.app.whatsapp.connection_service.MetaGraphClient", return_value=fake):
        response = await routes.run_health_check(user=user, db=db)

    assert response.ok is True
    assert response.connection.status == "CONNECTED"

    from shared.models.audit import AuditEvent
    from sqlalchemy import select
    events = (await db.execute(
        select(AuditEvent).where(
            AuditEvent.business_id == business.id,
            AuditEvent.action == "whatsapp.connection.health_check_failed",
        )
    )).scalars().all()
    assert len(events) == 0


# ── ROUTE level: send_test_message wires state + audit correctly ───────────

async def test_test_message_route_190_marks_needs_attention_and_audits(db):
    business, connection = await _make_business_with_connection(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    fake = _FakeHealthGraphClient(fail="190")
    body = routes.TestMessageRequest(to="27821234567")

    with patch("backend.app.api.v1.routes_whatsapp_connect.MetaGraphClient", return_value=fake):
        response = await routes.send_test_message(body=body, request=DummyRequest(), user=user, db=db)

    assert response.success is False

    await db.refresh(connection)
    assert connection.status == "NEEDS_ATTENTION"

    from shared.models.audit import AuditEvent
    from sqlalchemy import select
    events = (await db.execute(
        select(AuditEvent).where(
            AuditEvent.business_id == business.id,
            AuditEvent.action == "whatsapp.connection.test_message_failed",
        )
    )).scalars().all()
    assert len(events) == 1


async def test_test_message_route_success_audits_and_masks_recipient(db):
    business, connection = await _make_business_with_connection(db)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    fake = _FakeHealthGraphClient()
    body = routes.TestMessageRequest(to="27821234567")

    with patch("backend.app.api.v1.routes_whatsapp_connect.MetaGraphClient", return_value=fake):
        response = await routes.send_test_message(body=body, request=DummyRequest(), user=user, db=db)

    assert response.success is True

    from shared.models.audit import AuditEvent
    from sqlalchemy import select
    event = (await db.execute(
        select(AuditEvent).where(
            AuditEvent.business_id == business.id,
            AuditEvent.action == "whatsapp.connection.test_message_sent",
        )
    )).scalar_one()
    # Recipient number must be masked in the audit trail, never stored in full.
    assert event.diff_json["to_masked"] != "27821234567"
    assert event.diff_json["to_masked"].endswith("***")


async def test_test_message_route_not_connected_returns_409(db):
    business, connection = await _make_business_with_connection(db, status="DISCONNECTED", access_token_encrypted=None)
    user = AuthUser(user_id=uuid.uuid4(), business_id=business.id, role="OWNER", token_type="access")
    body = routes.TestMessageRequest(to="27821234567")

    from backend.app.core.errors import AppError
    with pytest.raises(AppError) as exc_info:
        await routes.send_test_message(body=body, request=DummyRequest(), user=user, db=db)
    assert exc_info.value.code == "WHATSAPP_NOT_CONNECTED"
    assert exc_info.value.status_code == 409
