"""
BUG-0158 (KNOWN_BUGS.md): "Can I get a chicken burger and a Coke?" against a
catalog shaped like the real Mochachos menu (Single/Double Chicken Burger
needing type disambiguation, no Coca-Cola item at all -- only "Buddy 440ml"
is a standard soft drink there).

Two things this proves, against a real Postgres-backed catalog rather than
the shared SA_MENU fixture (which happens to have a Coca-Cola item and so
can't exercise the "genuinely no match exists" path -- see conv_336.json
for the sibling-item-IS-found case using that shared catalog):

1. The customer is told, in the SAME reply as the burger-type question,
   that "Coke" isn't on the menu -- not a silent drop that only surfaces
   two turns later as a dead-end "Sorry, I didn't catch the items
   properly" (the real, reported production behaviour before this fix).
2. No Message/response ever fabricates or guesses a product -- the note
   names the exact unmatched word, nothing more.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete

from backend.app.bot.pipeline import process_inbound_message
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.menu import MenuCategory, MenuItem
from shared.models.message import Message
from tests.integration._db import get_session_factory


async def test_coke_request_gets_an_honest_note_not_a_silent_drop(test_business: Business, db):
    unique = uuid.uuid4().hex[:6]
    test_business.whatsapp_phone_number_id = f"bug0158-{unique}"
    test_business.is_active = True
    db.add(test_business)

    category = MenuCategory(business_id=test_business.id, name="Burgers", sort_order=0)
    db.add(category)
    await db.flush()
    db.add_all([
        MenuItem(business_id=test_business.id, category_id=category.id,
                 name="Single Chicken Burger", price_cents=6990, is_active=True),
        MenuItem(business_id=test_business.id, category_id=category.id,
                 name="Double Chicken Burger", price_cents=8990, is_active=True),
        # Deliberately NO Coca-Cola / Coke item -- "Buddy 440ml" is the only
        # standard soft drink, matching the real Mochachos Westville menu.
        MenuItem(business_id=test_business.id, category_id=category.id,
                 name="Buddy 440ml", price_cents=2890, is_active=True),
    ])
    await db.commit()

    wa_id = f"27{uuid.uuid4().int % 10**9:09d}"
    wa_message_id = f"wamid.bug0158-{uuid.uuid4().hex[:12]}"
    captured = {}

    async def _capture_send(*args, **kwargs):
        captured["text"] = kwargs.get("text")
        return "wamid.SENT"

    async with get_session_factory()() as session_db:
        with patch("backend.app.bot.pipeline.whatsapp_sender.send_text_message", new=_capture_send), \
             patch("backend.app.bot.pipeline.whatsapp_sender.send_image_message", new=AsyncMock(return_value=None)):
            await process_inbound_message(
                db=session_db,
                phone_number_id=test_business.whatsapp_phone_number_id,
                wa_message_id=wa_message_id,
                wa_id=wa_id,
                msg_text="Can I get a chicken burger and a Coke?",
                msg_type="text",
                raw_payload={"id": wa_message_id, "type": "text", "text": {"body": "Can I get a chicken burger and a Coke?"}},
                contact_name="BUG-0158 Test",
            )

    reply = captured.get("text") or ""
    assert "Which" in reply and "Chicken Burger" in reply, (
        f"expected the burger-type disambiguation question, got: {reply!r}"
    )
    assert "Coke" in reply, (
        f"expected the reply to explicitly acknowledge the unmatched 'Coke' request "
        f"(not silently drop it), got: {reply!r}"
    )
    assert "don't have" in reply.lower(), (
        f"expected an honest 'we don't have this' note, got: {reply!r}"
    )
    # Must never fabricate a specific substitute product by name.
    assert "coca-cola" not in reply.lower(), (
        f"must not invent a product name that isn't in this business's catalog, got: {reply!r}"
    )

    async with get_session_factory()() as cleanup_db:
        await cleanup_db.execute(delete(Message).where(Message.business_id == test_business.id))
        await cleanup_db.execute(delete(ConversationSession).where(ConversationSession.business_id == test_business.id))
        await cleanup_db.execute(delete(Customer).where(Customer.business_id == test_business.id))
        await cleanup_db.execute(delete(MenuItem).where(MenuItem.business_id == test_business.id))
        await cleanup_db.execute(delete(MenuCategory).where(MenuCategory.business_id == test_business.id))
        await cleanup_db.commit()
