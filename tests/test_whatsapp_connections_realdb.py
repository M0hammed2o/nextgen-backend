"""
Wave 0A / Milestone C — real-Postgres tests for the WhatsApp Embedded
Signup connect/disconnect/health-check flow, the token resolver, and the
account_update webhook handler.

Runs against the throwaway DB pointed to by DATABASE_URL (see worktree
README / task brief) and SKIPS CLEANLY if that database is not reachable,
matching the existing pattern in
tests/test_inventory_bot_and_cancellation_realdb.py:

    ENVIRONMENT=test \
    DATABASE_URL=postgresql+asyncpg://nextgen:wave0a_local_only@localhost:55444/nextgen_test_waba \
    python -m pytest tests/test_whatsapp_connections_realdb.py -q

No network calls are made anywhere in this file — MetaGraphClient is
replaced everywhere with a deterministic in-process fake (_FakeGraphClient).

Every fixture uses a uuid4-suffixed business, safe to re-run any number of
times against the same database.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.crypto import decrypt_credential
from backend.app.db.session import async_session_factory, engine
from backend.app.whatsapp import connection_service
from backend.app.whatsapp.graph_client import GraphAPIError
from backend.app.whatsapp.token_resolver import resolve_whatsapp_credentials
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection
from shared.utils import generate_business_code


# ── Fake Graph client — no network access anywhere in this file ────────────

class _FakeGraphClient:
    def __init__(self, waba_id, phone_number_id, access_token="fake-token-abc123",
                 fail_at=None, wrong_waba=False, expires_in=None):
        self.waba_id = waba_id
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.fail_at = fail_at
        self.wrong_waba = wrong_waba
        self.expires_in = expires_in
        self.calls: list[str] = []

    async def exchange_code_for_token(self, code):
        self.calls.append("exchange_code_for_token")
        if self.fail_at == "code_exchange":
            raise GraphAPIError("invalid or expired code", status_code=400, error_code=100)
        data = {"access_token": self.access_token, "token_type": "bearer"}
        if self.expires_in:
            data["expires_in"] = self.expires_in
        return data

    async def get_waba(self, waba_id, access_token):
        self.calls.append("get_waba")
        if self.fail_at == "verify_assets":
            raise GraphAPIError("token does not have access", status_code=401, error_code=190)
        if self.wrong_waba:
            return {"id": "waba-not-actually-yours", "name": "Someone else's WABA"}
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
        if self.fail_at == "subscribe_webhooks":
            raise GraphAPIError("could not subscribe", status_code=500, error_code=1)
        return {"success": True}

    async def register_phone(self, phone_number_id, access_token, pin):
        self.calls.append("register_phone")
        if self.fail_at == "register_phone":
            raise GraphAPIError("could not register", status_code=400, error_code=100)
        return {"success": True}

    async def unsubscribe_app(self, waba_id, access_token):
        self.calls.append("unsubscribe_app")
        return {"success": True}

    async def send_message(self, phone_number_id, access_token, payload):
        self.calls.append("send_message")
        if self.fail_at == "send_190":
            raise GraphAPIError("token invalid", status_code=401, error_code=190)
        return {"messages": [{"id": "wamid.test.123"}]}


# ── DB fixtures ──────────────────────────────────────────────────────────────

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
        pytest.skip(
            "Real Postgres not reachable at DATABASE_URL — skipping WhatsApp "
            "connection realdb tests (see file docstring for how to point "
            "this at the wave0a throwaway DB)."
        )
    async with async_session_factory() as session:
        yield session
        await session.rollback()
        await session.close()
    await engine.dispose()


async def _make_business(db: AsyncSession, **overrides) -> Business:
    unique = uuid.uuid4().hex[:8]
    biz = Business(
        name=f"Wave0a WABA Test {unique}",
        slug=f"wave0a-waba-{unique}",
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


# ── Happy path: encrypted token persisted, never plaintext ─────────────────

async def test_exchange_happy_path_persists_encrypted_token(db, caplog):
    # Explicitly configure a Fernet key for THIS test — CREDENTIALS_ENCRYPTION_KEY
    # is unset in the test environment by default (conftest.py), under which
    # encrypt_credential() intentionally stores plaintext (the documented
    # "development" fallback — see backend/app/core/crypto.py). This test
    # asserts the PRODUCTION (key-configured) behaviour specifically.
    from cryptography.fernet import Fernet as _Fernet

    from backend.app.core import crypto as _crypto
    key = _Fernet.generate_key().decode()

    import types
    fake_settings = types.SimpleNamespace(
        CREDENTIALS_ENCRYPTION_KEY=key, CREDENTIALS_ENCRYPTION_KEYS_OLD="",
    )
    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id, access_token="SUPER-SECRET-TOKEN-VALUE")

    import logging
    caplog.set_level(logging.DEBUG)

    with patch.object(_crypto, "get_settings", return_value=fake_settings):
        _crypto._fernet.cache_clear()
        try:
            outcome = await connection_service.run_exchange(
                db, business,
                code="fake-auth-code", waba_id=waba_id, phone_number_id=phone_number_id,
                connected_by_user_id=uuid.uuid4(), graph_client=fake,
            )

            assert outcome.success is True
            assert outcome.connection.status == "CONNECTED"
            assert outcome.failed_step is None

            # Ciphertext must differ from the plaintext token, and the
            # plaintext must never appear anywhere in captured logs.
            stored = outcome.connection.access_token_encrypted
            assert stored is not None
            assert stored != "SUPER-SECRET-TOKEN-VALUE"
            assert decrypt_credential(stored) == "SUPER-SECRET-TOKEN-VALUE"
            assert "SUPER-SECRET-TOKEN-VALUE" not in caplog.text
        finally:
            _crypto._fernet.cache_clear()

    await db.refresh(business)
    assert business.whatsapp_phone_number_id == phone_number_id
    assert business.whatsapp_business_account_id == waba_id

    assert fake.calls == [
        "exchange_code_for_token", "get_waba", "get_phone_number",
        "subscribe_app", "register_phone",
    ]


# ── Browser-claimed ids the token cannot access → rejected, nothing stored ──

async def test_exchange_rejects_waba_the_token_cannot_access(db):
    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id, wrong_waba=True)

    outcome = await connection_service.run_exchange(
        db, business,
        code="fake-auth-code", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake,
    )

    assert outcome.success is False
    assert outcome.failed_step == "verify_assets"
    assert outcome.connection.status == "NEEDS_ATTENTION"

    await db.refresh(business)
    assert business.whatsapp_phone_number_id is None  # nothing was claimed


# ── Partial failure at each step → resumable + correct step reported ───────

@pytest.mark.parametrize("fail_at", ["code_exchange", "verify_assets", "subscribe_webhooks", "register_phone"])
async def test_exchange_partial_failure_reports_correct_step(db, fail_at):
    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id, fail_at=fail_at)

    outcome = await connection_service.run_exchange(
        db, business,
        code="fake-auth-code", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake,
    )
    assert outcome.success is False
    assert outcome.failed_step == fail_at
    assert outcome.connection.status == "NEEDS_ATTENTION"
    assert outcome.message  # plain-English, non-empty

    # Resume: retry with a working client — must pick up and complete.
    fake_ok = _FakeGraphClient(waba_id, phone_number_id)
    outcome2 = await connection_service.run_exchange(
        db, business,
        code="fake-auth-code-2", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake_ok,
    )
    assert outcome2.success is True
    assert outcome2.connection.status == "CONNECTED"


# ── Number already owned by another business → 409-equivalent, other business untouched ─

async def test_exchange_rejects_number_already_owned_by_another_business(db):
    owner_biz = await _make_business(db)
    other_biz = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"

    fake1 = _FakeGraphClient(waba_id, phone_number_id)
    outcome1 = await connection_service.run_exchange(
        db, owner_biz,
        code="code-1", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake1,
    )
    assert outcome1.success is True

    # A second, different business tries to claim the SAME phone_number_id.
    fake2 = _FakeGraphClient(waba_id, phone_number_id)
    outcome2 = await connection_service.run_exchange(
        db, other_biz,
        code="code-2", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake2,
    )
    assert outcome2.success is False
    assert outcome2.failed_step == "claim_routing_number"
    assert outcome2.connection.status == "NEEDS_ATTENTION"

    # The first (rightful) owner is completely untouched.
    await db.refresh(owner_biz)
    assert owner_biz.whatsapp_phone_number_id == phone_number_id
    result = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == owner_biz.id)
    )
    assert result.scalar_one().status == "CONNECTED"

    await db.refresh(other_biz)
    assert other_biz.whatsapp_phone_number_id is None


# ── Resolver: connected business uses its own token, legacy uses platform ──

async def test_resolver_connected_business_uses_its_own_token(db):
    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id, access_token="BIZ-OWN-TOKEN")

    outcome = await connection_service.run_exchange(
        db, business,
        code="code", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake,
    )
    assert outcome.success is True
    await db.refresh(business)

    creds = await resolve_whatsapp_credentials(db, business)
    assert creds is not None
    assert creds.source == "business"
    assert creds.access_token == "BIZ-OWN-TOKEN"
    assert creds.phone_number_id == phone_number_id


async def test_resolver_legacy_business_uses_platform_token(db):
    business = await _make_business(db, whatsapp_phone_number_id=f"legacy-phone-{uuid.uuid4().hex[:8]}")
    creds = await resolve_whatsapp_credentials(db, business)
    assert creds is not None
    assert creds.source == "platform"
    assert creds.phone_number_id == business.whatsapp_phone_number_id


async def test_resolver_two_tenants_never_cross(db):
    """A CONNECTED business's token must never be returned for a different business."""
    biz_a = await _make_business(db)
    biz_b = await _make_business(db)
    waba_a = f"waba-a-{uuid.uuid4().hex[:8]}"
    phone_a = f"phone-a-{uuid.uuid4().hex[:8]}"
    fake_a = _FakeGraphClient(waba_a, phone_a, access_token="TOKEN-FOR-A-ONLY")

    outcome = await connection_service.run_exchange(
        db, biz_a,
        code="code-a", waba_id=waba_a, phone_number_id=phone_a,
        connected_by_user_id=uuid.uuid4(), graph_client=fake_a,
    )
    assert outcome.success is True
    await db.refresh(biz_a)
    await db.refresh(biz_b)

    creds_a = await resolve_whatsapp_credentials(db, biz_a)
    creds_b = await resolve_whatsapp_credentials(db, biz_b)

    assert creds_a.access_token == "TOKEN-FOR-A-ONLY"
    # biz_b has no connection and no legacy number — no usable credentials,
    # and CRITICALLY not biz_a's token either.
    assert creds_b is None


# ── Error 190 on send → NEEDS_ATTENTION, no platform fallback ──────────────

async def test_send_error_190_marks_needs_attention_and_no_platform_fallback(db):
    from backend.app.bot.whatsapp_sender import _send_via_business_connection

    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id)
    outcome = await connection_service.run_exchange(
        db, business,
        code="code", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake,
    )
    assert outcome.success is True
    await db.refresh(business)

    failing_fake = _FakeGraphClient(waba_id, phone_number_id, fail_at="send_190")
    with patch("backend.app.whatsapp.graph_client.MetaGraphClient", return_value=failing_fake):
        wamid, error_detail = await _send_via_business_connection(
            db, business, {"to": "27821234567", "type": "text", "text": {"body": "hi"}}
        )
    assert wamid is None
    # Contract since the merge with meta_errors: a structured, classified error.
    assert error_detail is not None
    assert error_detail.code == 190
    assert error_detail.retryable is False

    result = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == business.id)
    )
    connection = result.scalar_one()
    assert connection.status == "NEEDS_ATTENTION"

    # Second call: resolver must now return NOTHING (no platform fallback),
    # since business.whatsapp_phone_number_id still points at this connection.
    creds = await resolve_whatsapp_credentials(db, business)
    assert creds is None


# ── Disconnect wipes token, clears routing only when it matches ────────────

async def test_disconnect_wipes_token_and_clears_matching_routing_number(db):
    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id)
    outcome = await connection_service.run_exchange(
        db, business,
        code="code", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake,
    )
    assert outcome.success is True
    await db.refresh(business)
    assert business.whatsapp_phone_number_id == phone_number_id

    disconnect_fake = _FakeGraphClient(waba_id, phone_number_id)
    connection = await connection_service.disconnect(db, business, graph_client=disconnect_fake)

    assert connection.status == "DISCONNECTED"
    assert connection.access_token_encrypted is None
    assert connection.registration_pin_encrypted is None
    assert "unsubscribe_app" in disconnect_fake.calls

    await db.refresh(business)
    assert business.whatsapp_phone_number_id is None


async def test_disconnect_does_not_clear_routing_number_if_it_was_reassigned(db):
    """
    If the business's routing number was already changed to something else
    (e.g. manually, or by a later reconnect) before disconnect runs, a stale
    disconnect must NOT clear the NEW number.
    """
    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id)
    await connection_service.run_exchange(
        db, business,
        code="code", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake,
    )
    await db.refresh(business)

    # Simulate the number having moved on (e.g. platform reassignment).
    reassigned_number = f"reassigned-{uuid.uuid4().hex[:8]}"
    business.whatsapp_phone_number_id = reassigned_number
    await db.commit()

    disconnect_fake = _FakeGraphClient(waba_id, phone_number_id)
    await connection_service.disconnect(db, business, graph_client=disconnect_fake)

    await db.refresh(business)
    assert business.whatsapp_phone_number_id == reassigned_number


# ── account_update webhook marks the right connection ───────────────────────

async def test_account_update_webhook_marks_matching_connection_disconnected(db):
    from backend.app.api.v1.routes_webhook import _handle_account_update_event

    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id)
    await connection_service.run_exchange(
        db, business,
        code="code", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake,
    )

    await _handle_account_update_event(
        db, {"event": "PARTNER_APP_UNINSTALLED", "waba_info": {"waba_id": waba_id}}
    )

    result = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == business.id)
    )
    connection = result.scalar_one()
    assert connection.status == "DISCONNECTED"
    assert connection.status_reason
    assert connection.access_token_encrypted is None


async def test_account_update_webhook_unknown_waba_is_a_noop(db):
    """A waba_id with no matching connection must not raise or create rows."""
    from backend.app.api.v1.routes_webhook import _handle_account_update_event

    before = (await db.execute(select(WhatsAppConnection))).scalars().all()
    await _handle_account_update_event(
        db, {"event": "PARTNER_APP_UNINSTALLED", "waba_info": {"waba_id": "nonexistent-waba"}}
    )
    after = (await db.execute(select(WhatsAppConnection))).scalars().all()
    assert len(before) == len(after)


async def test_stamp_last_webhook_updates_connection_and_business(db):
    from backend.app.api.v1.routes_webhook import _stamp_last_webhook

    business = await _make_business(db)
    waba_id = f"waba-{uuid.uuid4().hex[:8]}"
    phone_number_id = f"phone-{uuid.uuid4().hex[:8]}"
    fake = _FakeGraphClient(waba_id, phone_number_id)
    await connection_service.run_exchange(
        db, business,
        code="code", waba_id=waba_id, phone_number_id=phone_number_id,
        connected_by_user_id=uuid.uuid4(), graph_client=fake,
    )

    await _stamp_last_webhook(db, phone_number_id)

    result = await db.execute(
        select(WhatsAppConnection).where(WhatsAppConnection.business_id == business.id)
    )
    connection = result.scalar_one()
    assert connection.last_webhook_at is not None

    await db.refresh(business)
    assert business.last_webhook_received_at is not None
