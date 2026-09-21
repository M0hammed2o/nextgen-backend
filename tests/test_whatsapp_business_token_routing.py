"""
Milestone C follow-up: every live WhatsApp send that has the business loaded
must go through the per-business credential resolver
(`_send_via_business_connection`), so a restaurant that connected its own
WhatsApp Business Account is answered with ITS token on the first attempt —
not with the platform token (which has no rights on a customer-owned WABA)
and a ~5 s outbox retry.

Callers that do not pass a business keep the unchanged platform path.
The resolver itself (connected -> business token, otherwise platform token,
never another tenant's token) is covered against real Postgres in
tests/test_whatsapp_connections_realdb.py.
"""

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.bot import whatsapp_sender


def _fake_db():
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    return db


def _business():
    return types.SimpleNamespace(
        id=uuid.uuid4(), whatsapp_phone_number_id="PNID-1", is_whatsapp_enabled=True
    )


async def test_send_text_with_business_uses_the_business_resolver():
    business = _business()
    via_business = AsyncMock(return_value=("wamid.biz", None))
    via_platform = AsyncMock(return_value=("wamid.platform", None))
    with patch.object(whatsapp_sender, "_send_via_business_connection", via_business), \
         patch.object(whatsapp_sender, "_send_via_meta_api", via_platform):
        wamid = await whatsapp_sender.send_text_message(
            db=_fake_db(), business_id=business.id, customer_wa_id="27820000000",
            phone_number_id=business.whatsapp_phone_number_id, text="hi",
            business=business,
        )
    assert wamid == "wamid.biz"
    via_business.assert_awaited_once()
    assert via_business.await_args.args[1] is business
    via_platform.assert_not_awaited()


async def test_send_text_without_business_keeps_platform_path():
    via_business = AsyncMock(return_value=("wamid.biz", None))
    via_platform = AsyncMock(return_value=("wamid.platform", None))
    with patch.object(whatsapp_sender, "_send_via_business_connection", via_business), \
         patch.object(whatsapp_sender, "_send_via_meta_api", via_platform):
        wamid = await whatsapp_sender.send_text_message(
            db=_fake_db(), business_id=uuid.uuid4(), customer_wa_id="27820000000",
            phone_number_id="PNID-1", text="hi",
        )
    assert wamid == "wamid.platform"
    via_business.assert_not_awaited()


async def test_send_image_with_business_uses_the_business_resolver():
    business = _business()
    via_business = AsyncMock(return_value=("wamid.img", None))
    via_platform = AsyncMock(return_value=("wamid.platform", None))
    with patch.object(whatsapp_sender, "_send_via_business_connection", via_business), \
         patch.object(whatsapp_sender, "_send_via_meta_api", via_platform):
        wamid = await whatsapp_sender.send_image_message(
            phone_number_id="PNID-1", recipient_wa_id="27820000000",
            image_url="https://example.invalid/menu.jpg", caption="Menu",
            db=_fake_db(), business=business,
        )
    assert wamid == "wamid.img"
    payload = via_business.await_args.args[2]
    assert payload["type"] == "image"
    assert payload["image"]["link"] == "https://example.invalid/menu.jpg"
    via_platform.assert_not_awaited()


async def test_send_notification_with_business_uses_the_business_resolver():
    business = _business()
    via_business = AsyncMock(return_value=("wamid.note", None))
    via_platform = AsyncMock(return_value=("wamid.platform", None))
    with patch.object(whatsapp_sender, "_send_via_business_connection", via_business), \
         patch.object(whatsapp_sender, "_send_via_meta_api", via_platform):
        wamid = await whatsapp_sender.send_notification_message(
            phone_number_id="PNID-1", recipient_wa_id="27820000000", text="Ready!",
            db=_fake_db(), business=business,
        )
    assert wamid == "wamid.note"
    via_platform.assert_not_awaited()


async def test_notification_outside_window_records_order_event_with_business_only():
    """business_id defaults to business.id, so the 131047 OrderEvent is still written."""
    from backend.app.bot.meta_errors import parse_meta_error

    business = _business()
    db = _fake_db()
    window = parse_meta_error(400, '{"error": {"code": 131047, "message": "Re-engagement message"}}')
    with patch.object(
        whatsapp_sender, "_send_via_business_connection", AsyncMock(return_value=(None, window))
    ):
        wamid = await whatsapp_sender.send_notification_message(
            phone_number_id="PNID-1", recipient_wa_id="27820000000", text="Ready!",
            db=db, business=business, order_id=uuid.uuid4(), order_status="READY",
        )
    assert wamid is None
    event = db.add.call_args.args[0]
    assert event.business_id == business.id
    assert "24-hour" in event.reason


async def test_pipeline_live_reply_passes_the_business():
    """The bot's live reply (first attempt) must carry the business to the sender."""
    from backend.app.bot import pipeline

    business = _business()
    customer = types.SimpleNamespace(id=uuid.uuid4(), wa_id="27820000000")
    send = AsyncMock(return_value="wamid.x")
    with patch.object(pipeline.whatsapp_sender, "send_text_message", send):
        await pipeline._send_response(_fake_db(), business, customer, "27820000000", "hello")
    send.assert_awaited_once()
    assert send.await_args.kwargs["business"] is business
