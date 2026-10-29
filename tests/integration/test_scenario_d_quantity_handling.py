"""
Scenario D (the latest reported production defect, per the WhatsApp
ordering reliability directive, 2026-10-26): multiple-identical-item and
quantity-handling correctness.

Root cause found while building these tests: intent_router's ORDER_START
pattern requires an explicit ordering verb ("can I get", "I want", "I'll
have", etc.) -- a bare "<quantity> <item>" statement with no verb ("3 half
chickens") matched NO intent pattern at all (intent=None), so it never
reached deterministic extraction and went to the LLM for everything, every
time, regardless of how unambiguous the item/quantity actually was.

Fixed in pipeline.py:
1. _det_eligible now also covers intent=None in IDLE/GREETING/BROWSING_MENU/
   BUILDING_CART -- safe by construction, since if deterministic extraction
   finds nothing, the message falls through to the LLM exactly as before.
2. _ITEM_ALIASES's pluralization only handled a bare "+s" suffix -- "three
   buddies" (a real catalog item needing a "buddy"->"buddy 440ml" alias)
   never matched, since English pluralizes a consonant+"y" ending to "-ies"
   not "-ys". Generalized, not Mochachos-specific.
3. The deterministic cart-removal path already parsed a stated quantity
   ("remove ONE half chicken") but never passed it to remove_from_cart at
   all -- every removal deleted the whole line regardless of what count was
   stated. Now passes it through, but ONLY when an explicit count word/digit
   is present in the message -- "remove the burger" (no count stated) stays
   a full removal.
4. "2 chicken burgers, one mild and one hot" captured both option names
   correctly as text ("Mild, Hot") but applied that combined string to a
   single qty=2 cart line instead of splitting into two distinct qty=1
   lines. Fixed with a conservative split: only when the modifier splits
   into exactly as many comma-separated pieces as the stated quantity, and
   every piece is a real, enabled option name for that item.

Every assertion here reads the REAL persisted ConversationSession cart
(and, where relevant, real persisted order rows) via the actual pipeline
functions -- not the AI-generated reply text.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select, text

from backend.app.bot import state_machine
from backend.app.bot.pipeline import process_inbound_message
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.menu import MenuCategory, MenuItem
from shared.models.message import Message
from tests.integration._db import get_session_factory

SPICE_LEVEL_OPTIONS = {
    "option_groups": [
        {
            "id": "spice-level", "name": "Spice Level", "required": True,
            "min_selections": 1, "max_selections": 1, "sort_order": 0, "is_enabled": True,
            "options": [
                {"id": "mild", "name": "Mild", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                {"id": "hot", "name": "Hot", "price_delta_cents": 0, "sort_order": 1, "is_enabled": True},
            ],
        },
    ]
}


class _Harness:
    def __init__(self, business: Business):
        self.business = business
        self.wa_id = f"27{uuid.uuid4().int % 10**9:09d}"

    async def send(self, text_body: str) -> str:
        captured = {}

        async def _capture_send(*args, **kwargs):
            captured["text"] = kwargs.get("text")
            return "wamid.SENT"

        wmid = f"wamid.{uuid.uuid4().hex[:12]}"
        async with get_session_factory()() as db:
            with patch("backend.app.bot.pipeline.whatsapp_sender.send_text_message", new=_capture_send), \
                 patch("backend.app.bot.pipeline.whatsapp_sender.send_image_message", new=AsyncMock(return_value=None)):
                await process_inbound_message(
                    db=db,
                    phone_number_id=self.business.whatsapp_phone_number_id,
                    wa_message_id=wmid,
                    wa_id=self.wa_id,
                    msg_text=text_body,
                    msg_type="text",
                    raw_payload={"id": wmid, "type": "text", "text": {"body": text_body}},
                    contact_name="Scenario D Test",
                )
        return captured.get("text") or ""

    async def cart(self) -> list[dict]:
        async with get_session_factory()() as db:
            cust = (await db.execute(
                select(Customer).where(Customer.business_id == self.business.id, Customer.wa_id == self.wa_id)
            )).scalar_one()
            sess = (await db.execute(
                select(ConversationSession).where(ConversationSession.customer_id == cust.id)
            )).scalar_one()
            return state_machine.get_cart(sess)


async def _setup_business(test_business: Business, db) -> Business:
    unique = uuid.uuid4().hex[:6]
    test_business.whatsapp_phone_number_id = f"scenario-d-{unique}"
    test_business.is_active = True
    db.add(test_business)

    cat = MenuCategory(business_id=test_business.id, name="Mains", sort_order=0)
    db.add(cat)
    await db.flush()
    db.add_all([
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Half Chicken",
                 price_cents=11490, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Chips",
                 price_cents=3590, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Buddy 440ml",
                 price_cents=2890, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Chicken Burger",
                 price_cents=6990, is_active=True, options_json=SPICE_LEVEL_OPTIONS),
        # Combo whose own name contains "Buddy" -- alias-collision guard.
        MenuItem(business_id=test_business.id, category_id=cat.id,
                 name="Quarter Chicken, Beeg Chips and a Buddy", price_cents=10990, is_active=True),
    ])
    await db.commit()
    return test_business


async def _cleanup(business_id):
    async with get_session_factory()() as db:
        await db.execute(text("DELETE FROM order_items WHERE business_id = :b"), {"b": business_id})
        await db.execute(text("DELETE FROM orders WHERE business_id = :b"), {"b": business_id})
        await db.execute(delete(Message).where(Message.business_id == business_id))
        await db.execute(delete(ConversationSession).where(ConversationSession.business_id == business_id))
        await db.execute(delete(Customer).where(Customer.business_id == business_id))
        await db.execute(delete(MenuItem).where(MenuItem.business_id == business_id))
        await db.execute(delete(MenuCategory).where(MenuCategory.business_id == business_id))
        await db.commit()


async def test_d1_numeral_quantity_bare_phrase(test_business: Business, db):
    """'3 half chickens' (no ordering verb) must record quantity 3."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("3 half chickens")
        cart = await h.cart()
        assert len(cart) == 1
        assert cart[0]["name"] == "Half Chicken"
        assert cart[0]["quantity"] == 3
    finally:
        await _cleanup(business.id)


async def test_d2_two_different_items_with_distinct_quantities(test_business: Business, db):
    """'Two chips and three buddies' must record quantities 2 and 3."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Two chips and three buddies")
        cart = await h.cart()
        by_name = {i["name"]: i["quantity"] for i in cart}
        assert by_name.get("Chips") == 2
        assert by_name.get("Buddy 440ml") == 3
    finally:
        await _cleanup(business.id)


async def test_d2b_buddy_alias_does_not_match_the_unrelated_combo(test_business: Business, db):
    """Regression guard: 'buddy'/'buddies' must never match the combo whose
    own name happens to contain the word 'Buddy'."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("I'll have a buddy")
        cart = await h.cart()
        names = {i["name"] for i in cart}
        assert "Buddy 440ml" in names
        assert "Quarter Chicken, Beeg Chips and a Buddy" not in names
    finally:
        await _cleanup(business.id)


async def test_d3_same_modifier_for_all_units_stays_one_line(test_business: Business, db):
    """'2 chicken burgers, both hot' must preserve quantity 2 as ONE line
    (both units share the same modifier, unlike D4)."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("2 chicken burgers, both hot")
        cart = await h.cart()
        assert len(cart) == 1, f"both units share one modifier -- must stay a single line, got: {cart}"
        assert cart[0]["quantity"] == 2
        assert cart[0]["special_instructions"] == "Hot"
    finally:
        await _cleanup(business.id)


async def test_d4_distinct_modifiers_split_into_separate_lines(test_business: Business, db):
    """'2 chicken burgers, one mild and one hot' must preserve distinct
    modifiers as TWO separate qty=1 lines, not one merged qty=2 line."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("2 chicken burgers, one mild and one hot")
        cart = await h.cart()
        burgers = [i for i in cart if i["name"] == "Chicken Burger"]
        assert len(burgers) == 2, f"expected 2 separate lines (distinct modifiers), got: {burgers}"
        mods = {b["special_instructions"] for b in burgers}
        assert mods == {"Mild", "Hot"}
        assert all(b["quantity"] == 1 for b in burgers)
    finally:
        await _cleanup(business.id)


async def test_d5_add_another_increments_existing_quantity(test_business: Business, db):
    """'Add another two chips' must increment the existing line by 2."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("chips please")
        await h.send("Add another two chips")
        cart = await h.cart()
        assert len(cart) == 1
        assert cart[0]["quantity"] == 3
    finally:
        await _cleanup(business.id)


async def test_d6_change_quantity_sets_absolute_value(test_business: Business, db):
    """'Change chips to four' must SET the total quantity to 4, not add 4."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("chips please")
        await h.send("Change chips to four")
        cart = await h.cart()
        assert len(cart) == 1
        assert cart[0]["quantity"] == 4
    finally:
        await _cleanup(business.id)


async def test_d7_remove_one_decrements_not_deletes_the_line(test_business: Business, db):
    """'Remove one burger' against a qty=2 cart must decrement to 1, never
    delete the whole line."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("2 half chickens please")
        await h.send("Remove one half chicken")
        cart = await h.cart()
        assert len(cart) == 1, f"must still have the item, just decremented: {cart}"
        assert cart[0]["quantity"] == 1
    finally:
        await _cleanup(business.id)


async def test_d7b_remove_with_no_stated_count_removes_the_whole_line(test_business: Business, db):
    """Regression guard: 'remove the half chicken' (no count stated) must
    still fully remove the line -- the D7 fix must not make this a partial
    decrement by 1 instead."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("2 half chickens please")
        await h.send("Remove the half chicken")
        cart = await h.cart()
        assert cart == [], f"no count stated -- must remove the whole line, got: {cart}"
    finally:
        await _cleanup(business.id)


async def test_d8_cancel_everything_clears_cart_without_placing_order(test_business: Business, db):
    """'Cancel everything' must clear the cart without placing an order."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("half chicken and chips")
        await h.send("Cancel everything")
        cart = await h.cart()
        assert cart == []

        async with get_session_factory()() as verify_db:
            count = (await verify_db.execute(
                text("SELECT COUNT(*) FROM orders WHERE business_id = :b"), {"b": business.id}
            )).scalar_one()
            assert count == 0, "cancelling must never place an order"
    finally:
        await _cleanup(business.id)
