"""
Gate hardening pass, 2026-09-22 — §2 failure-mode sweep finding: confirm
EVERY send path in whatsapp_sender.py honours token_resolver.py's "no
platform-token fallback for a customer-owned WABA" rule when a caller passes
`business`/`db`, and behaves exactly as before (platform token, unchanged)
when it doesn't.

send_text_message/send_notification_message/send_image_message were already
resolver-aware; send_template_message/send_interactive_buttons were found,
while auditing every send path per the task brief, to unconditionally use
the platform token regardless of `business` — because they had (and have)
NO live callers anywhere in the bot pipeline, this was never a live
tenant-isolation bypass, but it was a real gap that would have silently
reactivated the moment a future caller wired either of them up without also
threading resolver-awareness through, same as every other sender function.
Fixed alongside this test as part of the same completeness sweep.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from backend.app.bot import whatsapp_sender


async def test_send_template_message_uses_business_connection_when_business_and_db_given():
    fake_business = object()
    fake_db = object()
    with patch.object(
        whatsapp_sender, "_send_via_business_connection", new=AsyncMock(return_value=("wamid.1", None))
    ) as mock_biz, patch.object(
        whatsapp_sender, "_send_via_meta_api", new=AsyncMock(return_value=("should-not-be-used", None))
    ) as mock_platform:
        result = await whatsapp_sender.send_template_message(
            phone_number_id="platform-phone-id",
            recipient_wa_id="27821234567",
            template_name="hello_world",
            db=fake_db,
            business=fake_business,
        )
    assert result == "wamid.1"
    mock_biz.assert_awaited_once()
    mock_platform.assert_not_awaited()


async def test_send_template_message_uses_platform_token_when_business_omitted():
    """Every existing/omitted caller keeps today's exact behaviour — zero regression."""
    with patch.object(
        whatsapp_sender, "_send_via_meta_api", new=AsyncMock(return_value=("wamid.platform", None))
    ) as mock_platform, patch.object(
        whatsapp_sender, "_send_via_business_connection", new=AsyncMock()
    ) as mock_biz:
        result = await whatsapp_sender.send_template_message(
            phone_number_id="platform-phone-id",
            recipient_wa_id="27821234567",
            template_name="hello_world",
        )
    assert result == "wamid.platform"
    mock_platform.assert_awaited_once()
    mock_biz.assert_not_awaited()


async def test_send_interactive_buttons_uses_business_connection_when_business_and_db_given():
    fake_business = object()
    fake_db = object()
    with patch.object(
        whatsapp_sender, "_send_via_business_connection", new=AsyncMock(return_value=("wamid.2", None))
    ) as mock_biz, patch.object(
        whatsapp_sender, "_send_via_meta_api", new=AsyncMock(return_value=("should-not-be-used", None))
    ) as mock_platform:
        result = await whatsapp_sender.send_interactive_buttons(
            phone_number_id="platform-phone-id",
            recipient_wa_id="27821234567",
            body_text="Confirm your order?",
            buttons=[{"id": "yes", "title": "Yes"}],
            db=fake_db,
            business=fake_business,
        )
    assert result == "wamid.2"
    mock_biz.assert_awaited_once()
    mock_platform.assert_not_awaited()


async def test_send_interactive_buttons_uses_platform_token_when_business_omitted():
    with patch.object(
        whatsapp_sender, "_send_via_meta_api", new=AsyncMock(return_value=("wamid.platform2", None))
    ) as mock_platform, patch.object(
        whatsapp_sender, "_send_via_business_connection", new=AsyncMock()
    ) as mock_biz:
        result = await whatsapp_sender.send_interactive_buttons(
            phone_number_id="platform-phone-id",
            recipient_wa_id="27821234567",
            body_text="Confirm your order?",
            buttons=[{"id": "yes", "title": "Yes"}],
        )
    assert result == "wamid.platform2"
    mock_platform.assert_awaited_once()
    mock_biz.assert_not_awaited()
