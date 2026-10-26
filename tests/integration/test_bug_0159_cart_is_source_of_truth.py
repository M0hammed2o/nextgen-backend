"""
BUG-0159 (KNOWN_BUGS.md): the persisted backend cart must be the single
source of truth for an order -- an AI-generated reply's free-form text must
never display items/totals that diverge from what is actually committed.

Real, reported production symptom, 2026-10-26: a WhatsApp customer ordered
Half Chicken + Chips + Buddy 440ml, answered "Hot" for the Half Chicken's
required Spice Level, and the bot's own reply correctly displayed all three
items at R179.70, asking "Reply yes to confirm". Replying "Yes" then
produced a DIFFERENT, wrong 2-item cart (R150.80, no spice level recorded)
-- the confirmation text had been right, but what actually got committed to
the cart was the ORIGINAL, still-unresolved pending item, because nothing
in the "Hot" turn's handling had ever written the chosen spice level back
into session state. A later concurrency investigation (BUG-0157) ruled out
a race: this reproduces 100% of the time, fully sequential, no concurrent
delivery needed.

Root cause and fix (pipeline.py, CHOOSING_OPTIONS branch):
1. Resolving a single pending item's required option group(s) (e.g. a
   Spice Level answer) is now fully DETERMINISTIC -- matched directly
   against the pending item's own stored options_json, no LLM call
   involved, so there is nothing for a text/structured-data mismatch to
   attach to for this (by far the most common) case. Falls through to the
   LLM unchanged for anything this can't resolve (multiple simultaneous
   pending items, genuinely freeform phrasing).
2. A reconciliation guard at the "Yes"-commits-the-LLM's-proposal site: if
   `_proposed_items` (what the LLM structurally returned) doesn't name an
   item that was present in `_pending_opts` (what was validated and
   tracked before the LLM ran), that item is recovered from the pending
   state rather than silently dropped.
3. BUG-0155 (separate, pre-existing, found while building this fixture):
   the real catalog's own branded soft drink ("Buddy 440ml") had no
   `_ITEM_ALIASES` entry, unlike coke/sprite/fanta, so "a buddy" alone fell
   through to the LLM instead of matching deterministically. Fixed with a
   `"buddy": "buddy 440ml"` alias -- deliberately NOT bare "buddy", because
   real Mochachos combo names end in "...and a Buddy" (e.g. "Quarter
   Chicken, Beeg Chips and a Buddy") and a bare substring target matched
   the combo instead of the standalone drink (confirmed directly while
   building this fixture).

Every assertion here reads the REAL persisted `orders`/`order_items` rows
via direct SQL, not a mocked response -- the whole point is proving what's
actually committed, not what the bot merely said.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select, text

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
                {"id": "lemon-herb", "name": "Lemon and Herb", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                {"id": "mild", "name": "Mild", "price_delta_cents": 0, "sort_order": 1, "is_enabled": True},
                {"id": "hot", "name": "Hot", "price_delta_cents": 0, "sort_order": 2, "is_enabled": True},
                {"id": "extra-hot", "name": "Extra Hot", "price_delta_cents": 0, "sort_order": 3, "is_enabled": True},
                {"id": "dynamite", "name": "Dynamite", "price_delta_cents": 0, "sort_order": 4, "is_enabled": True},
            ],
        },
    ]
}

SIZE_OPTIONS = {
    "option_groups": [
        {
            "id": "size", "name": "Size", "required": True,
            "min_selections": 1, "max_selections": 1, "sort_order": 0, "is_enabled": True,
            "options": [
                {"id": "small", "name": "Small", "price_delta_cents": 0, "sort_order": 0, "is_enabled": True},
                {"id": "large", "name": "Large", "price_delta_cents": 500, "sort_order": 1, "is_enabled": True},
            ],
        },
    ]
}


class _Harness:
    """Shared send/inspect helpers for this file's tests."""

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
                    contact_name="BUG-0159 Test",
                )
        return captured.get("text") or ""

    async def session_row(self) -> ConversationSession:
        async with get_session_factory()() as db:
            cust = (await db.execute(
                select(Customer).where(Customer.business_id == self.business.id, Customer.wa_id == self.wa_id)
            )).scalar_one()
            return (await db.execute(
                select(ConversationSession).where(ConversationSession.customer_id == cust.id)
            )).scalar_one()

    async def latest_order_items(self) -> list[dict]:
        async with get_session_factory()() as db:
            rows = (await db.execute(text(
                "SELECT name_snapshot, quantity, special_instructions, line_total_cents "
                "FROM order_items WHERE order_id = ("
                "  SELECT id FROM orders WHERE business_id = :bid ORDER BY created_at DESC LIMIT 1"
                ")"
            ), {"bid": self.business.id})).mappings().all()
            return [dict(r) for r in rows]

    async def latest_order(self):
        async with get_session_factory()() as db:
            return (await db.execute(text(
                "SELECT order_number, subtotal_cents, total_cents, order_mode, status "
                "FROM orders WHERE business_id = :bid ORDER BY created_at DESC LIMIT 1"
            ), {"bid": self.business.id})).mappings().one()


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


async def _setup_mochachos_style_business(test_business: Business, db) -> Business:
    """Deliberately NOT tenant-specific -- any business gets this same correctness,
    per requirement 12. Named like Mochachos's real catalog only because that's
    the reported case, but nothing here is Mochachos-specific code."""
    unique = uuid.uuid4().hex[:6]
    test_business.whatsapp_phone_number_id = f"bug0159-{unique}"
    test_business.is_active = True
    db.add(test_business)

    cat = MenuCategory(business_id=test_business.id, name="Mains", sort_order=0)
    db.add(cat)
    await db.flush()
    db.add_all([
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Half Chicken",
                 price_cents=11490, is_active=True, options_json=SPICE_LEVEL_OPTIONS),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Chips",
                 price_cents=3590, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Buddy 440ml",
                 price_cents=2890, is_active=True),
        # Combo whose own name contains "Buddy" -- regression guard for the
        # alias-collision mistake found while building this fixture.
        MenuItem(business_id=test_business.id, category_id=cat.id,
                 name="Quarter Chicken, Beeg Chips and a Buddy", price_cents=10990,
                 is_active=True, options_json=SPICE_LEVEL_OPTIONS),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Garden Salad",
                 price_cents=4990, is_active=True, options_json=SIZE_OPTIONS),
    ])
    await db.commit()
    return test_business


async def test_requirement_9_full_order_flow_half_chicken_chips_buddy(test_business: Business, db):
    """
    Hello -> Half Chicken + Chips + Buddy -> Hot -> Yes -> Pickup ->
    Customer Details -> Order Placed, exactly as specified.
    """
    business = await _setup_mochachos_style_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")

        reply = await h.send("Please can I get one half chicken, chips and a buddy.")
        assert "spice level" in reply.lower() and "Half Chicken" in reply

        reply = await h.send("Hot")
        assert "R179.70" in reply or "179.70" in reply, f"expected the correct R179.70 total, got: {reply!r}"
        assert "Hot" in reply
        assert "Chips" in reply and "Buddy" in reply, (
            f"expected all three items (Chips, Buddy, Half Chicken) in the confirmation, got: {reply!r}"
        )
        assert "yes" in reply.lower()

        session = await h.session_row()
        assert session.state == "CONFIRMING_ORDER"

        reply = await h.send("Yes")
        # Must advance toward checkout, never re-show an "Added to your
        # order" / "Reply yes to confirm" loop a second time (the exact
        # reported bug).
        assert "added to your order" not in reply.lower(), (
            f"BUG-0159 regression: 'Yes' re-triggered an add-items response instead of "
            f"advancing to checkout: {reply!r}"
        )

        await h.send("0821234567")
        await h.send("Pickup")
        await h.send("Thabo")
        await h.send("skip")

        order = await h.latest_order()
        assert order["total_cents"] == 17970, f"expected total 17970 (R179.70), got {order['total_cents']}"
        assert order["subtotal_cents"] == 17970
        assert order["order_mode"] == "PICKUP"

        items = await h.latest_order_items()
        by_name = {i["name_snapshot"]: i for i in items}
        assert set(by_name) == {"Half Chicken", "Chips", "Buddy 440ml"}, (
            f"expected exactly these 3 items, got: {set(by_name)}"
        )
        assert by_name["Half Chicken"]["special_instructions"] == "Hot", (
            "spice level must survive from pending -> confirmed -> persisted order"
        )
        assert by_name["Half Chicken"]["line_total_cents"] == 11490
        assert by_name["Chips"]["line_total_cents"] == 3590
        assert by_name["Buddy 440ml"]["line_total_cents"] == 2890
    finally:
        await _cleanup(business.id)


async def test_spice_level_answer_never_touches_already_cart_items(test_business: Business, db):
    """
    Requirement 4: answering the spice level must update ONLY the relevant
    pending item -- Chips (already silently committed to cart during the
    first message, since it has no required options) must be completely
    untouched by the "Hot" turn.
    """
    business = await _setup_mochachos_style_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Please can I get one half chicken, chips and a buddy.")
        await h.send("Hot")

        session = await h.session_row()
        from backend.app.bot import state_machine
        cart = state_machine.get_cart(session)
        chips = next(i for i in cart if i["name"] == "Chips")
        assert chips["quantity"] == 1
        assert chips.get("special_instructions") is None, (
            "the spice level answer must never bleed onto an unrelated cart item"
        )
    finally:
        await _cleanup(business.id)


async def test_combo_whose_own_name_contains_buddy_is_not_confused_with_the_drink(test_business: Business, db):
    """
    Regression guard for the alias-collision mistake found and fixed while
    building this fixture: "buddy" as a bare alias target matched the combo
    "Quarter Chicken, Beeg Chips and a Buddy" (since active_sorted tries
    longest names first) instead of the standalone "Buddy 440ml" drink.
    """
    business = await _setup_mochachos_style_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        reply = await h.send("I'll have a buddy")
        session = await h.session_row()
        from backend.app.bot import state_machine
        cart = state_machine.get_cart(session)
        names = {i["name"] for i in cart}
        assert "Buddy 440ml" in names or "buddy 440ml" in {n.lower() for n in names}, (
            f"expected the standalone drink to match, got cart: {cart}"
        )
        assert "Quarter Chicken, Beeg Chips and a Buddy" not in names, (
            f"must not match the unrelated combo just because its name contains 'Buddy': {cart}"
        )
    finally:
        await _cleanup(business.id)


async def test_requirement_10_different_modifier_size_selection(test_business: Business, db):
    """A different option-group shape (Size, not Spice Level) resolves the
    same deterministic way -- not special-cased to spice levels specifically."""
    business = await _setup_mochachos_style_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        reply = await h.send("Can I get a garden salad")
        assert "size" in reply.lower()

        reply = await h.send("Large")
        assert "R54.90" in reply or "54.90" in reply, f"expected Large's +R5 delta applied, got: {reply!r}"

        session = await h.session_row()
        from backend.app.bot import state_machine
        cart = state_machine.get_cart(session)
        assert len(cart) == 1
        assert cart[0]["name"] == "Garden Salad"
    finally:
        await _cleanup(business.id)


async def test_requirement_10_quantity_change_after_confirmation_pending(test_business: Business, db):
    """Quantity correction still works correctly alongside the new
    deterministic CHOOSING_OPTIONS resolution -- not broken by it."""
    business = await _setup_mochachos_style_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("I'll have 2 chips")
        session = await h.session_row()
        from backend.app.bot import state_machine
        cart = state_machine.get_cart(session)
        assert cart[0]["quantity"] == 2
    finally:
        await _cleanup(business.id)


async def test_requirement_10_cancellation_during_pending_options(test_business: Business, db):
    """Declining a required-option question with no safe default cancels
    the pending item cleanly, same as before this fix."""
    business = await _setup_mochachos_style_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("half chicken please")
        reply = await h.send("no")
        session = await h.session_row()
        from backend.app.bot import state_machine
        cart = state_machine.get_cart(session)
        assert cart == [], f"declining a required option must not leave a half-configured item in cart: {cart}"
    finally:
        await _cleanup(business.id)
