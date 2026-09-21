"""
whatsapp_sender.py send-failure diagnosability (Wave 0A P1).

Before this fix, _send_via_meta_api logged resp.text[:500] and returned
None on any failure — nothing distinguished an invalid token from an
outside-service-window customer from a rate limit, and send_text_message's
outbox_entry always ended up PENDING (queued for a retry that could never
succeed for a permanent failure).

Confined to the response-handling path — no change to how the platform
token or phone_number_id is resolved.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.bot.whatsapp_sender import (
    _send_via_meta_api,
    send_notification_message,
    send_text_message,
)
from shared.models.audit import MessageOutbox
from shared.models.message import Message


def _mock_client(status_code: int, json_body: dict | None = None, text: str = ""):
    import json as json_module

    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = text or (json_module.dumps(json_body) if json_body else "")
    mock_response.json.return_value = json_body or {}

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


class FakeDB:
    def __init__(self):
        self.added = []
        self.flushed = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1

    async def execute(self, stmt):
        raise AssertionError("no extra DB query expected")


# ── _send_via_meta_api: classified tuple return ─────────────────────────────

async def test_success_returns_wamid_and_no_error():
    client = _mock_client(200, {"messages": [{"id": "wamid.123"}]})
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        wamid, info = await _send_via_meta_api("PNID", "27821234567", {"type": "text"})
    assert wamid == "wamid.123"
    assert info is None


async def test_invalid_token_190_is_classified_permanent():
    body = {"error": {"code": 190, "message": "Error validating access token"}}
    client = _mock_client(401, body)
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        wamid, info = await _send_via_meta_api("PNID", "27821234567", {"type": "text"})
    assert wamid is None
    assert info.code == 190
    assert info.retryable is False


async def test_outside_service_window_131047_is_classified_permanent():
    body = {"error": {"code": 131047, "message": "outside 24 hour window"}}
    client = _mock_client(470, body)
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        wamid, info = await _send_via_meta_api("PNID", "27821234567", {"type": "text"})
    assert wamid is None
    assert info.code == 131047
    assert info.retryable is False


async def test_rate_limit_130429_is_classified_retryable():
    body = {"error": {"code": 130429, "message": "rate limited"}}
    client = _mock_client(429, body)
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        wamid, info = await _send_via_meta_api("PNID", "27821234567", {"type": "text"})
    assert wamid is None
    assert info.retryable is True


async def test_timeout_is_classified_retryable():
    import httpx as httpx_module
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=httpx_module.TimeoutException("timed out"))
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        wamid, info = await _send_via_meta_api("PNID", "27821234567", {"type": "text"})
    assert wamid is None
    assert info.retryable is True
    assert "TRANSPORT_ERROR" in info.last_error


# ── send_text_message: outbox reflects the classification ──────────────────

async def test_send_text_message_permanent_failure_marks_outbox_failed_immediately():
    db = FakeDB()
    body = {"error": {"code": 190, "message": "Error validating access token"}}
    client = _mock_client(401, body)
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        result = await send_text_message(
            db=db, business_id=uuid.uuid4(), customer_wa_id="27821234567",
            phone_number_id="PNID", text="hello",
        )
    assert result is None
    outbox = next(o for o in db.added if isinstance(o, MessageOutbox))
    assert outbox.status == "FAILED"
    assert "META_190" in outbox.last_error


async def test_send_text_message_retryable_failure_stays_pending_with_reason():
    db = FakeDB()
    body = {"error": {"code": 130429, "message": "rate limited"}}
    client = _mock_client(429, body)
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        result = await send_text_message(
            db=db, business_id=uuid.uuid4(), customer_wa_id="27821234567",
            phone_number_id="PNID", text="hello",
        )
    assert result is None
    outbox = next(o for o in db.added if isinstance(o, MessageOutbox))
    assert outbox.status == "PENDING"
    assert "130429" in outbox.last_error


async def test_send_text_message_success_unaffected():
    db = FakeDB()
    client = _mock_client(200, {"messages": [{"id": "wamid.abc"}]})
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        result = await send_text_message(
            db=db, business_id=uuid.uuid4(), customer_wa_id="27821234567",
            phone_number_id="PNID", text="hello",
        )
    assert result == "wamid.abc"
    outbox = next(o for o in db.added if isinstance(o, MessageOutbox))
    assert outbox.status == "SENT"


# ── send_notification_message: 131047 writes an OrderEvent when order known ─

async def test_notification_131047_with_order_context_writes_order_event():
    db = FakeDB()
    order_id = uuid.uuid4()
    business_id = uuid.uuid4()
    body = {"error": {"code": 131047, "message": "outside 24 hour window"}}
    client = _mock_client(470, body)
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        result = await send_notification_message(
            phone_number_id="PNID", recipient_wa_id="27821234567", text="hi",
            db=db, order_id=order_id, business_id=business_id, order_status="ACCEPTED",
        )
    assert result is None
    from shared.models.order import OrderEvent
    events = [o for o in db.added if isinstance(o, OrderEvent)]
    assert len(events) == 1
    assert events[0].order_id == order_id
    assert events[0].business_id == business_id
    assert "24-hour" in events[0].reason


async def test_notification_131047_without_order_context_writes_nothing():
    """Existing call sites that don't pass db/order_id must be unaffected."""
    body = {"error": {"code": 131047, "message": "outside 24 hour window"}}
    client = _mock_client(470, body)
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        result = await send_notification_message(
            phone_number_id="PNID", recipient_wa_id="27821234567", text="hi",
        )
    assert result is None  # no exception, no DB access attempted


async def test_notification_non_131047_failure_does_not_write_order_event():
    db = FakeDB()
    body = {"error": {"code": 190, "message": "bad token"}}
    client = _mock_client(401, body)
    with patch("backend.app.bot.whatsapp_sender.httpx.AsyncClient", return_value=client):
        await send_notification_message(
            phone_number_id="PNID", recipient_wa_id="27821234567", text="hi",
            db=db, order_id=uuid.uuid4(), business_id=uuid.uuid4(), order_status="NEW",
        )
    assert db.added == []
