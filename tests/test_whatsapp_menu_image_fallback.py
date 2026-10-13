"""
Regression suite for the production bug: a WhatsApp customer asking for the
menu ("Menu", "Can I see your menu") got NO response at all, while "Hi" and
"Specials" worked fine.

Root cause (see KNOWN_BUGS.md BUG-0146 / WORKLOG.md for the full writeup):
`whatsapp_sender.send_image_message()` never raises on a Meta-API-level
failure (bad/unreachable image URL, rejected payload, timeout) — per its own
docstring, it "Returns the wamid on success, None on failure." The
MENU_REQUEST handler in `backend/app/bot/pipeline.py` awaited that call,
never checked its return value, and unconditionally returned an empty
response the moment the `await` didn't raise — so a connected business with
a `menu_image_url` that Meta couldn't actually deliver got no image AND no
text fallback, even though the `except Exception` branch already existed to
provide that fallback for a genuine Python-level error.

Fix: check the returned wamid; treat a falsy return exactly like the
existing exception path (fall through to the text menu).
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.bot.pipeline import _handle_message
from shared.enums import MessageIntent

from tests.replay.fixtures import FakeMenuItem, make_business, make_customer
from tests.replay.framework import ReplaySession


def _business_with_image(menu_image_url: str | None):
    return make_business({"menu_image_url": menu_image_url})


async def _run_menu_request(business, items):
    customer = make_customer(business_id=business.id)
    session = ReplaySession(state="IDLE")
    with patch(
        "backend.app.bot.pipeline._load_menu",
        new=AsyncMock(return_value=([], items)),
    ):
        return await _handle_message(
            db=AsyncMock(),
            business=business,
            customer=customer,
            session=session,
            msg_text="Menu",
            norm_text="menu",
            intent=MessageIntent.MENU_REQUEST,
        )


class TestMenuImageSendSuccess:
    @pytest.mark.asyncio
    async def test_successful_image_delivery_skips_the_text_menu(self):
        business = _business_with_image("https://cdn.example.com/menu.jpg")
        items = [FakeMenuItem("Margherita Pizza", 8500)]

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_image_message",
            new=AsyncMock(return_value="wamid.REAL123"),
        ) as mock_send:
            text, is_llm, *_ = await _run_menu_request(business, items)

        mock_send.assert_awaited_once()
        assert text == ""
        assert is_llm is False


class TestMenuImageSendFailure:
    @pytest.mark.asyncio
    async def test_failed_image_delivery_none_return_falls_back_to_text_menu(self):
        """The actual production bug: send_image_message() returns None
        (Meta rejected it / unreachable URL) without raising. The customer
        must still get the text menu, not silence."""
        business = _business_with_image("https://cdn.example.com/dead-link.jpg")
        items = [FakeMenuItem("Margherita Pizza", 8500)]

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_image_message",
            new=AsyncMock(return_value=None),
        ) as mock_send:
            text, *_ = await _run_menu_request(business, items)

        mock_send.assert_awaited_once()
        assert text != ""
        assert "Margherita Pizza" in text

    @pytest.mark.asyncio
    async def test_failed_image_delivery_via_exception_still_falls_back(self):
        """The pre-existing exception path must keep working unchanged."""
        business = _business_with_image("https://cdn.example.com/menu.jpg")
        items = [FakeMenuItem("Margherita Pizza", 8500)]

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_image_message",
            new=AsyncMock(side_effect=RuntimeError("network blew up")),
        ) as mock_send:
            text, *_ = await _run_menu_request(business, items)

        mock_send.assert_awaited_once()
        assert text != ""
        assert "Margherita Pizza" in text


class TestEmptyMenuHandling:
    @pytest.mark.asyncio
    async def test_menu_request_with_no_active_items_shows_unavailable_message_not_silence(self):
        """No menu_image_url at all, and zero active items — the pre-existing
        defensive fallback in responses.menu_response() must still produce a
        real message end-to-end through the pipeline, never an empty one."""
        business = _business_with_image(None)

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_image_message",
        ) as mock_send:
            text, *_ = await _run_menu_request(business, [])

        mock_send.assert_not_called()
        assert text != ""
        assert "updated" in text.lower()
