"""
Gate hardening pass, 2026-09-22 — §2 failure-mode sweep: "repeated Meta
failures must never hammer Meta with a broken credential."

token_resolver.py already returns None (no usable credentials) for a
business whose active routing number belongs to a NEEDS_ATTENTION/
DISCONNECTED connection, and _send_via_business_connection already returns
early on None credentials without constructing a Graph client at all — this
was true by construction, but the task brief specifically asks to prove it
with a test that asserts ZERO HTTP calls are attempted in that state, not
just that the send "fails" (a send could still fail AFTER a real, wasted
Graph API call — a materially different, worse failure mode this test rules
out). Patches both potential HTTP entry points
(httpx.AsyncClient, used by the platform-token path, and
MetaGraphClient.send_message, used by the business-connection path) to
raise if invoked at all.

Real throwaway DB (skips cleanly if unreachable).
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.bot.whatsapp_sender import _send_via_business_connection
from backend.app.db.session import async_session_factory, engine
from backend.app.whatsapp.graph_client import MetaGraphClient
from shared.models.business import Business
from shared.models.whatsapp_connection import WhatsAppConnection
from shared.utils import generate_business_code


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


class _MustNotBeCalled:
    """Raises AssertionError if invoked — proves a code path never fires."""

    def __call__(self, *a, **kw):
        raise AssertionError("HTTP call was attempted against Meta while the connection was not CONNECTED")

    async def __aenter__(self, *a, **kw):
        raise AssertionError("httpx.AsyncClient was constructed while the connection was not CONNECTED")

    async def __aexit__(self, *a, **kw):
        return False


@pytest.mark.parametrize("status", ["NEEDS_ATTENTION", "DISCONNECTED"])
async def test_no_http_call_attempted_when_connection_not_connected(db, status):
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"FailFast Test {unique}",
        slug=f"failfast-{unique}",
        business_code=generate_business_code(),
        currency="ZAR",
        timezone="Africa/Johannesburg",
        is_whatsapp_enabled=True,
        whatsapp_phone_number_id=f"phone-{unique}",
    )
    db.add(business)
    await db.flush()

    connection = WhatsAppConnection(
        business_id=business.id,
        waba_id=f"waba-{unique}",
        phone_number_id=business.whatsapp_phone_number_id,  # connection OWNS the routing number
        status=status,
        access_token_encrypted=None if status == "DISCONNECTED" else "enc1:irrelevant-if-never-decrypted",
    )
    db.add(connection)
    await db.commit()
    await db.refresh(business)

    payload = {"to": "27821234567", "type": "text", "text": {"body": "hi"}}

    with patch.object(httpx, "AsyncClient", _MustNotBeCalled), \
         patch.object(MetaGraphClient, "send_message", side_effect=AssertionError(
             "MetaGraphClient.send_message was called while the connection was not CONNECTED"
         )):
        wamid, error_detail = await _send_via_business_connection(db, business, payload)

    assert wamid is None
    assert error_detail is not None
    assert error_detail.retryable is False
