"""
Regression suite for two related production bugs where a WhatsApp customer
asking for the menu got NO response at all, while "Hi" and "Specials"
worked fine.

BUG-0146 (first fix, insufficient on its own): `send_image_message()` never
raises on a Meta-API-level failure -- it only signals that via its return
value (None). The MENU_REQUEST handler awaited it and returned an empty
response the instant the await didn't raise, without checking that value.

BUG-0147 (this fix, the actual production-confirmed cause): fixing BUG-0146
alone did not resolve the live symptom, because Meta's synchronous API
response (a 200 with a message ID) only means the image was *accepted for
sending* -- not that it was *delivered*. Real delivery outcome arrives
later via an async status webhook (`_handle_status_update` in
routes_webhook.py) that this pipeline never acted on; a "failed" status was
only ever logged. So even when `send_image_message()` returns a real
wamid, the image can still silently fail to reach the customer's phone,
and the old code had already returned an empty response believing success.

Fix: a menu image, whatever happens to it, must NEVER suppress the text
menu. The pipeline now always builds and sends the real text menu as well
-- the image send is purely a best-effort bonus, fire-and-forget, never a
substitute. Large menus that would exceed WhatsApp's message-length limit
are split safely at category/item boundaries (`responses.build_menu_messages`),
with each extra chunk's send result checked and logged, never assumed.
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.bot.pipeline import _handle_message
from backend.app.bot.responses import WHATSAPP_TEXT_SAFE_MAX_CHARS, build_menu_messages
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


# ── 1. A menu image must never suppress the text menu ───────────────────────

class TestImageNeverSuppressesText:
    @pytest.mark.asyncio
    async def test_successful_image_submission_still_sends_the_text_menu(self):
        """The exact production bug: Meta accepts the image (returns a real
        wamid) but that is not proof of delivery -- the text menu must still
        be sent, not suppressed."""
        business = _business_with_image("https://cdn.example.com/menu.jpg")
        items = [FakeMenuItem("Margherita Pizza", 8500)]

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_image_message",
            new=AsyncMock(return_value="wamid.REAL123"),
        ) as mock_image, patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_text_message",
            new=AsyncMock(return_value="wamid.CHUNK"),
        ) as mock_text_chunks:
            text, is_llm, *_ = await _run_menu_request(business, items)

        mock_image.assert_awaited_once()
        assert text != ""
        assert "Margherita Pizza" in text
        assert is_llm is False
        # The menu fits in one message, so no *extra* chunk sends happen --
        # the single chunk flows back through the normal return value.
        mock_text_chunks.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejected_image_submission_none_return_still_sends_text_menu(self):
        business = _business_with_image("https://cdn.example.com/dead-link.jpg")
        items = [FakeMenuItem("Margherita Pizza", 8500)]

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_image_message",
            new=AsyncMock(return_value=None),
        ) as mock_image:
            text, *_ = await _run_menu_request(business, items)

        mock_image.assert_awaited_once()
        assert "Margherita Pizza" in text

    @pytest.mark.asyncio
    async def test_image_send_delivery_failure_via_exception_still_sends_text_menu(self):
        business = _business_with_image("https://cdn.example.com/menu.jpg")
        items = [FakeMenuItem("Margherita Pizza", 8500)]

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_image_message",
            new=AsyncMock(side_effect=RuntimeError("network blew up")),
        ) as mock_image:
            text, *_ = await _run_menu_request(business, items)

        mock_image.assert_awaited_once()
        assert "Margherita Pizza" in text


# ── 2. Empty menu ─────────────────────────────────────────────────────────

class TestEmptyMenuHandling:
    @pytest.mark.asyncio
    async def test_menu_request_with_no_active_items_shows_unavailable_message_not_silence(self):
        business = _business_with_image(None)

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_image_message",
        ) as mock_image, patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_text_message",
        ) as mock_text_chunks:
            text, *_ = await _run_menu_request(business, [])

        mock_image.assert_not_called()
        mock_text_chunks.assert_not_called()
        assert text != ""
        assert "updated" in text.lower()


# ── 3. Large menus: safe chunking ────────────────────────────────────────────

class TestLargeMenuChunking:
    def test_build_menu_messages_returns_one_chunk_for_a_normal_menu(self):
        cat = _fake_category("Burgers", 0)
        items = [FakeMenuItem("Classic Smash Burger", 7500, category_name="Burgers")]
        items[0].category_id = cat.id
        messages = build_menu_messages([cat], items, "ZAR")
        assert len(messages) == 1
        assert "Classic Smash Burger" in messages[0]

    def test_build_menu_messages_splits_a_large_menu_without_cutting_items(self):
        cat = _fake_category("Pizzas", 0)
        items = []
        for i in range(200):
            item = FakeMenuItem(
                f"Specialty Pizza Number {i:03d}",
                9900 + i,
                category_name="Pizzas",
            )
            item.category_id = cat.id
            item.description = (
                f"A long, delicious description of pizza number {i:03d} with "
                "extra toppings and a detailed origin story for every topping."
            )
            items.append(item)

        messages = build_menu_messages([cat], items, "ZAR")

        assert len(messages) > 1, "a 200-item menu with long descriptions must be split"
        for msg in messages:
            assert len(msg) <= WHATSAPP_TEXT_SAFE_MAX_CHARS + 200  # small slack for the cont. header

        # Every item name and price must appear exactly once across all
        # chunks combined -- nothing dropped, nothing duplicated.
        full_text = "\n".join(messages)
        for i in range(200):
            name = f"Specialty Pizza Number {i:03d}"
            assert full_text.count(name) == 1, f"{name} should appear exactly once"

        # No item's description is ever separated from its own item line
        # onto a different message than its price line.
        for msg in messages:
            lines = msg.split("\n")
            for idx, line in enumerate(lines):
                if line.strip().startswith("_") and line.strip().endswith("_"):
                    # A description line must be immediately preceded, in
                    # THIS SAME message, by its item's price line.
                    assert idx > 0 and "—" in lines[idx - 1]

    @pytest.mark.asyncio
    async def test_large_menu_sends_extra_chunks_directly_with_result_checked(self):
        business = _business_with_image(None)
        items = []
        for i in range(200):
            item = FakeMenuItem(f"Specialty Pizza Number {i:03d}", 9900 + i)
            item.description = "A very long description " * 10
            items.append(item)

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_text_message",
            new=AsyncMock(return_value="wamid.CHUNK"),
        ) as mock_text_chunks:
            text, *_ = await _run_menu_request(business, items)

        assert text != ""
        # At least one extra chunk had to be sent directly beyond the first,
        # which flows back through the normal return value.
        assert mock_text_chunks.await_count >= 1

    @pytest.mark.asyncio
    async def test_large_menu_logs_but_does_not_crash_when_a_chunk_send_fails(self):
        business = _business_with_image(None)
        items = []
        for i in range(200):
            item = FakeMenuItem(f"Specialty Pizza Number {i:03d}", 9900 + i)
            item.description = "A very long description " * 10
            items.append(item)

        with patch(
            "backend.app.bot.pipeline.whatsapp_sender.send_text_message",
            new=AsyncMock(return_value=None),
        ) as mock_text_chunks:
            text, *_ = await _run_menu_request(business, items)

        assert text != ""
        assert mock_text_chunks.await_count >= 1


# ── 4. No cross-tenant menu leakage ─────────────────────────────────────────

class TestNoCrossTenantLeakage:
    def test_two_businesses_menus_never_mix_in_chunked_output(self):
        cat_a = _fake_category("Burgers", 0)
        items_a = [FakeMenuItem("Tenant A Secret Burger", 5000, category_name="Burgers")]
        items_a[0].category_id = cat_a.id

        cat_b = _fake_category("Pizzas", 0)
        items_b = [FakeMenuItem("Tenant B Secret Pizza", 6000, category_name="Pizzas")]
        items_b[0].category_id = cat_b.id

        messages_a = build_menu_messages([cat_a], items_a, "ZAR")
        messages_b = build_menu_messages([cat_b], items_b, "ZAR")

        assert "Tenant B Secret Pizza" not in "\n".join(messages_a)
        assert "Tenant A Secret Burger" not in "\n".join(messages_b)


def _fake_category(name: str, sort_order: int):
    import uuid

    class _Cat:
        def __init__(self):
            self.id = uuid.uuid4()
            self.name = name
            self.sort_order = sort_order

    return _Cat()
