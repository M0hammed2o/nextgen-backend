"""
Souk WhatsApp gate: an event vendor never receives the platform token.

The vendor must message customers through its OWN Embedded-Signup WABA so
Meta bills the vendor, not NextGen. A restaurant's Model-1 platform-token
fallback is unchanged.
"""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.whatsapp.token_resolver import resolve_whatsapp_credentials

pytestmark = pytest.mark.asyncio


def _db(connection=None):
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = connection
    db.execute = AsyncMock(return_value=result)
    return db


def _biz(plan, number="1234567890"):
    return SimpleNamespace(id=uuid.uuid4(), plan=plan, whatsapp_phone_number_id=number,
                           whatsapp_business_account_id=None)


async def test_event_vendor_without_own_connection_gets_no_credentials():
    assert await resolve_whatsapp_credentials(_db(), _biz("EVENT")) is None


async def test_event_vendor_with_disconnected_row_still_gets_no_platform_token():
    conn = SimpleNamespace(id=uuid.uuid4(), phone_number_id="other", status="DISCONNECTED",
                           access_token_encrypted=None)
    assert await resolve_whatsapp_credentials(_db(conn), _biz("EVENT")) is None


async def test_event_vendor_with_own_connected_waba_uses_its_own_token():
    conn = SimpleNamespace(id=uuid.uuid4(), phone_number_id="1234567890", status="CONNECTED",
                           access_token_encrypted="vendor-token")
    creds = await resolve_whatsapp_credentials(_db(conn), _biz("EVENT"))
    assert creds.source == "business" and creds.access_token == "vendor-token"


@pytest.mark.parametrize("plan", ["STARTER", "GROWTH", "PRO"])
async def test_restaurant_keeps_platform_token_fallback(plan):
    creds = await resolve_whatsapp_credentials(_db(), _biz(plan))
    assert creds is not None and creds.source == "platform"
    assert creds.phone_number_id == "1234567890"
