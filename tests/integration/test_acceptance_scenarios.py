"""
The 22 essential acceptance scenarios (WhatsApp ordering reliability
directive, 2026-10-26, section "5. Essential acceptance scenarios") — each
built as its own real-Postgres conversation test against the REAL pipeline
(backend/app/bot/pipeline.py::process_inbound_message), with assertions on
the actual persisted ConversationSession cart and/or Order/OrderItem rows,
never on AI-generated reply text alone.

Scope notes, stated honestly rather than overclaimed:
  - Scenario 18 (payment methods/links): the cash/card choice capture and
    its persisted payment_status are tested end-to-end against real
    Postgres. Generating an actual ONLINE PAYMENT LINK requires a live,
    connected payment provider (Stripe/Yoco/etc.) — not available in this
    environment (no provider credentials configured here) — so that half
    of the scenario is explicitly NOT exercised here and must not be read
    as verified. See the final WhatsApp-reliability report for the full
    disclosure.
  - "Include the exact historical Mochachos conversation as a permanent
    regression" (directive, same section) is already satisfied by the
    pre-existing, currently-passing replay fixtures tagged
    regression_failure_1/2/3 (tests/replay/conversations/conv_002.json,
    conv_008.json, conv_013.json) — not duplicated here.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import delete, select, text
from unittest.mock import AsyncMock, patch

from backend.app.bot import state_machine
from backend.app.bot.pipeline import process_inbound_message
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.menu import MenuCategory, MenuItem
from shared.models.message import Message
from shared.models.order import Order, OrderItem
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

    async def send(self, text_body: str, wa_message_id: str | None = None) -> str:
        captured = {}

        async def _capture_send(*args, **kwargs):
            captured["text"] = kwargs.get("text")
            return "wamid.SENT"

        wmid = wa_message_id or f"wamid.{uuid.uuid4().hex[:12]}"
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
                    contact_name="Acceptance Scenario Test",
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

    async def pending_options(self) -> list[dict]:
        async with get_session_factory()() as db:
            cust = (await db.execute(
                select(Customer).where(Customer.business_id == self.business.id, Customer.wa_id == self.wa_id)
            )).scalar_one()
            sess = (await db.execute(
                select(ConversationSession).where(ConversationSession.customer_id == cust.id)
            )).scalar_one()
            return state_machine.get_context(sess, "pending_options") or []

    async def state(self) -> str:
        async with get_session_factory()() as db:
            cust = (await db.execute(
                select(Customer).where(Customer.business_id == self.business.id, Customer.wa_id == self.wa_id)
            )).scalar_one()
            sess = (await db.execute(
                select(ConversationSession).where(ConversationSession.customer_id == cust.id)
            )).scalar_one()
            return sess.state

    async def staff_set_context(self, **updates) -> None:
        """Simulate a staff-side action (e.g. setting a delivery fee) by
        writing directly into the session context, the same thing the
        replay framework's '_type: staff_action' turns do."""
        async with get_session_factory()() as db:
            cust = (await db.execute(
                select(Customer).where(Customer.business_id == self.business.id, Customer.wa_id == self.wa_id)
            )).scalar_one()
            sess = (await db.execute(
                select(ConversationSession).where(ConversationSession.customer_id == cust.id)
            )).scalar_one()
            for k, v in updates.items():
                state_machine.set_context(sess, k, v)
            await db.commit()

    async def orders(self) -> list[Order]:
        async with get_session_factory()() as db:
            rows = (await db.execute(
                select(Order).where(Order.business_id == self.business.id).order_by(Order.created_at)
            )).scalars().all()
            return list(rows)

    async def order_items(self, order_id) -> list[OrderItem]:
        async with get_session_factory()() as db:
            rows = (await db.execute(
                select(OrderItem).where(OrderItem.order_id == order_id)
            )).scalars().all()
            return list(rows)


async def _setup_business(test_business: Business, db, **overrides) -> Business:
    unique = uuid.uuid4().hex[:6]
    test_business.whatsapp_phone_number_id = f"acceptance-{unique}"
    test_business.is_active = True
    # Business.require_phone_number defaults to True -- most of these 22
    # scenarios are testing something other than detail-collection, so
    # default it off here and opt back in explicitly (scenario 16) where
    # the scenario is specifically about collecting those details.
    test_business.require_phone_number = False
    test_business.require_customer_name = False
    for k, v in overrides.items():
        setattr(test_business, k, v)
    db.add(test_business)

    cat = MenuCategory(business_id=test_business.id, name="Mains", sort_order=0)
    db.add(cat)
    await db.flush()
    db.add_all([
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Classic Smash Burger",
                 price_cents=7500, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Chicken Burger",
                 price_cents=6990, is_active=True, options_json=SPICE_LEVEL_OPTIONS),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Chips",
                 price_cents=3590, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Buddy 440ml",
                 price_cents=2890, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id,
                 name="Quarter Chicken, Beeg Chips and a Buddy", price_cents=10990, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Quarter Chicken",
                 price_cents=8990, is_active=True),
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


# ── 1. Single item with no modifiers ──────────────────────────────────────

async def test_01_single_item_no_modifiers(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Can I get a Classic Smash Burger")
        cart = await h.cart()
        assert len(cart) == 1
        assert cart[0]["name"] == "Classic Smash Burger"
        assert cart[0]["quantity"] == 1
        assert cart[0].get("special_instructions") is None
    finally:
        await _cleanup(business.id)


# ── 2. Single item requiring spice-level selection ────────────────────────

async def test_02_single_item_requires_spice_level(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("I'll have a chicken burger")
        pending = await h.pending_options()
        assert len(pending) == 1 and pending[0]["name"] == "Chicken Burger", (
            f"an item needing a required option must be held as pending, not yet in the cart: {pending}"
        )
        assert await h.cart() == []
        await h.send("Hot")
        cart = await h.cart()
        assert len(cart) == 1
        assert cart[0]["special_instructions"] == "Hot"
    finally:
        await _cleanup(business.id)


# ── 3. Multiple different items ───────────────────────────────────────────

async def test_03_multiple_different_items(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("chips and a buddy 440ml")
        cart = await h.cart()
        names = {i["name"] for i in cart}
        assert names == {"Chips", "Buddy 440ml"}
    finally:
        await _cleanup(business.id)


# ── 4. Multiple identical items ───────────────────────────────────────────

async def test_04_multiple_identical_items(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("3 chips please")
        cart = await h.cart()
        assert len(cart) == 1
        assert cart[0]["name"] == "Chips" and cart[0]["quantity"] == 3
    finally:
        await _cleanup(business.id)


# ── 5. Multiple items with distinct modifiers ─────────────────────────────

async def test_05_multiple_items_with_distinct_modifiers(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("2 chicken burgers, one mild and one hot")
        cart = await h.cart()
        burgers = [i for i in cart if i["name"] == "Chicken Burger"]
        assert len(burgers) == 2
        assert {b["special_instructions"] for b in burgers} == {"Mild", "Hot"}
    finally:
        await _cleanup(business.id)


# ── 6. Combo with a similar name to individual items ─────────────────────

async def test_06_combo_with_similar_name_to_individual_items(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Quarter Chicken, Beeg Chips and a Buddy")
        cart = await h.cart()
        # Must resolve to the ONE combo line, never fragment into the
        # standalone Quarter Chicken / Chips / Buddy items it shares words
        # with, and never double-charge both the combo AND its components.
        assert len(cart) == 1, f"must match the combo as a single line, got: {cart}"
        assert cart[0]["name"] == "Quarter Chicken, Beeg Chips and a Buddy"
        assert cart[0]["price_cents"] == 10990
    finally:
        await _cleanup(business.id)


# ── 7. Existing cart quantity increase ────────────────────────────────────

async def test_07_existing_cart_quantity_increase(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("chips please")
        await h.send("Add another two chips")
        cart = await h.cart()
        assert len(cart) == 1 and cart[0]["quantity"] == 3
    finally:
        await _cleanup(business.id)


# ── 8. Existing cart quantity replacement ─────────────────────────────────

async def test_08_existing_cart_quantity_replacement(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("2 chips please")
        await h.send("Change chips to five")
        cart = await h.cart()
        assert len(cart) == 1 and cart[0]["quantity"] == 5
    finally:
        await _cleanup(business.id)


# ── 9. Partial cart deletion ───────────────────────────────────────────────

async def test_09_partial_cart_deletion(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("a classic smash burger, chips and a buddy 440ml")
        await h.send("Take out the chips")
        cart = await h.cart()
        names = {i["name"] for i in cart}
        assert names == {"Classic Smash Burger", "Buddy 440ml"}, f"only chips should be removed, got: {cart}"
    finally:
        await _cleanup(business.id)


# ── 10. Full cart cancellation ────────────────────────────────────────────

async def test_10_full_cart_cancellation(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("a classic smash burger and chips")
        await h.send("Cancel everything")
        cart = await h.cart()
        assert cart == []
        orders = await h.orders()
        assert orders == [], "cancelling before checkout must never place an order"
    finally:
        await _cleanup(business.id)


# ── 11. Incorrect or unavailable item ─────────────────────────────────────

async def test_11_incorrect_or_unavailable_item(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        response = await h.send("Can I get a large pepperoni pizza please")
        cart = await h.cart()
        assert cart == [], f"an item that doesn't exist on this menu must never be added, got: {cart}"
        assert response, "customer must get an honest reply, not silence"
    finally:
        await _cleanup(business.id)


# ── 12. Customer changes their mind midway ────────────────────────────────

async def test_12_customer_changes_their_mind_midway(test_business: Business, db):
    """'Replace X with Y' is natural-language understanding the pipeline
    deliberately routes to the LLM (action=replace_item) -- see conv_038's
    own proven phrasing/response shape, reused here against the REAL
    replace_item code path and a real persisted cart (not a replay fake)."""
    from unittest.mock import patch as _patch
    from backend.app.llm.provider import LLMProvider, LLMResponse

    class _OneShotReplace(LLMProvider):
        async def complete(self, system_prompt, user_message, **kwargs):
            return await self.complete_with_history(system_prompt, [])

        async def complete_with_history(self, system_prompt, messages, **kwargs):
            import json as _json
            return LLMResponse(
                text=_json.dumps({
                    "action": "replace_item",
                    "message": "Swapped Classic Smash Burger for Chicken Burger ✅",
                    "items": [{"remove": "Classic Smash Burger", "add": "Chicken Burger", "quantity": 1}],
                }),
                input_tokens=0, output_tokens=0, total_tokens=0,
                model="acceptance-test", provider="test", cost_cents=0,
            )

    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Can I get a classic smash burger")
        with _patch("backend.app.llm.provider.get_llm_provider", return_value=_OneShotReplace()):
            await h.send("Actually, replace that with a chicken burger instead")
        cart = await h.cart()
        names = {i["name"] for i in cart}
        assert "Classic Smash Burger" not in names, f"the original item must be gone, got: {cart}"
        assert "Chicken Burger" in names, f"the replacement must be present, got: {cart}"
    finally:
        await _cleanup(business.id)


# ── 13. Customer interrupts a modifier question ───────────────────────────

async def test_13_customer_interrupts_a_modifier_question(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("I'll have a chicken burger")  # triggers the spice-level question
        await h.send("what are your opening hours?")  # interrupts, doesn't answer
        pending_mid = await h.pending_options()
        assert len(pending_mid) == 1, f"the pending item must survive the interruption, got: {pending_mid}"
        await h.send("Hot")  # now answers the original question
        cart = await h.cart()
        assert len(cart) == 1
        assert cart[0]["special_instructions"] == "Hot", f"must still resolve correctly after the interruption: {cart}"
    finally:
        await _cleanup(business.id)


# ── 14. Customer asks a menu question halfway through ordering ───────────

async def test_14_customer_asks_a_menu_question_halfway_through(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("chips please")
        await h.send("what drinks do you have?")
        cart = await h.cart()
        assert len(cart) == 1 and cart[0]["name"] == "Chips", (
            f"a menu question must never disturb the existing cart, got: {cart}"
        )
        await h.send("add a buddy 440ml too")
        cart = await h.cart()
        names = {i["name"] for i in cart}
        assert names == {"Chips", "Buddy 440ml"}
    finally:
        await _cleanup(business.id)


# ── 15. Delivery versus pickup ────────────────────────────────────────────

async def test_15a_explicit_pickup_choice_sets_order_mode_pickup(test_business: Business, db):
    # delivery_enabled=True so the pipeline actually presents the explicit
    # pickup-vs-delivery choice (ORDER_MODE_CHECK: CHOOSING_ORDER_MODE only
    # fires when `_delivery_enabled`); this exercises the customer actively
    # choosing "Pickup" over "Delivery", not just the implicit default.
    business = await _setup_business(
        test_business, db,
        delivery_enabled=True, require_customer_name=False,
        require_phone_number=False, require_delivery_address=False,
    )
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Can I get a classic smash burger")
        await h.send("Yes")
        assert await h.state() == "CHOOSING_ORDER_MODE"
        await h.send("Pickup")
        for _ in range(3):
            if await h.state() not in ("COLLECTING_DETAILS",):
                break
            await h.send("skip")
        orders = await h.orders()
        assert len(orders) == 1
        assert orders[0].order_mode == "PICKUP"
    finally:
        await _cleanup(business.id)


async def test_15a2_default_business_with_no_delivery_implicitly_assumes_pickup(test_business: Business, db):
    # delivery_enabled defaults to False -- the pipeline skips the explicit
    # pickup/delivery question entirely and assumes pickup, going straight
    # to COLLECTING_DETAILS.
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Can I get a classic smash burger")
        await h.send("Yes")
        assert await h.state() == "COLLECTING_DETAILS"
        for _ in range(3):
            if await h.state() not in ("COLLECTING_DETAILS",):
                break
            await h.send("skip")
        orders = await h.orders()
        assert len(orders) == 1
        assert orders[0].order_mode == "PICKUP"
    finally:
        await _cleanup(business.id)


async def test_15b_delivery_flow_sets_order_mode_delivery_and_fee(test_business: Business, db):
    business = await _setup_business(
        test_business, db,
        delivery_enabled=True, require_customer_name=False,
        require_phone_number=False, require_delivery_address=False,
    )
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Can I get a classic smash burger")
        await h.send("Yes")
        await h.send("Delivery")
        await h.send("22 Kings Avenue")
        await h.send("skip")
        assert await h.state() == "WAITING_DELIVERY_FEE_APPROVAL"
        await h.staff_set_context(delivery_fee_cents=3000, delivery_fee_status="FEE_SENT")
        await h.send("Yes")
        assert await h.state() == "COLLECTING_PAYMENT"
        await h.send("Cash")
        orders = await h.orders()
        assert len(orders) == 1
        order = orders[0]
        assert order.order_mode == "DELIVERY"
        assert order.delivery_fee_cents == 3000
        assert order.subtotal_cents == 7500
        assert order.total_cents == 10500
    finally:
        await _cleanup(business.id)


# ── 16. Phone number / customer details ───────────────────────────────────

async def test_16_customer_details_are_collected_and_persisted(test_business: Business, db):
    business = await _setup_business(
        test_business, db, require_customer_name=True, require_phone_number=True,
    )
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Can I get a classic smash burger")
        await h.send("Yes")
        assert await h.state() == "COLLECTING_DETAILS"
        await h.send("Thandiwe")
        await h.send("0821234567")
        await h.send("skip")
        orders = await h.orders()
        assert len(orders) == 1
        assert orders[0].customer_name == "Thandiwe"
        assert orders[0].phone_number and "821234567" in orders[0].phone_number
    finally:
        await _cleanup(business.id)


# ── 17. Checkout ───────────────────────────────────────────────────────────

async def test_17_checkout_persists_correct_order_and_items(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("a classic smash burger and 2 chips")
        await h.send("Yes")
        for _ in range(3):
            if await h.state() not in ("COLLECTING_DETAILS",):
                break
            await h.send("skip")
        orders = await h.orders()
        assert len(orders) == 1
        order = orders[0]
        assert order.status in ("NEW", "ACCEPTED")
        expected_subtotal = 7500 + 2 * 3590
        assert order.subtotal_cents == expected_subtotal
        assert order.total_cents == expected_subtotal
        items = await h.order_items(order.id)
        by_name = {i.name_snapshot: i.quantity for i in items}
        assert by_name == {"Classic Smash Burger": 1, "Chips": 2}
        cart = await h.cart()
        assert cart == [], "cart must be cleared once the order is placed"
    finally:
        await _cleanup(business.id)


# ── 18. Payment methods (cash/card capture against real Postgres) ────────

async def test_18_payment_method_choice_is_captured_and_persisted(test_business: Business, db):
    """Scope note: exercises the cash/card CHOICE capture end-to-end against
    real Postgres. Does NOT exercise an actual online payment LINK — no
    live payment provider is connected in this environment. See module
    docstring."""
    business = await _setup_business(
        test_business, db,
        delivery_enabled=True, require_customer_name=False,
        require_phone_number=False, require_delivery_address=False,
    )
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Can I get a classic smash burger")
        await h.send("Yes")
        await h.send("Delivery")
        await h.send("22 Kings Avenue")
        await h.send("skip")
        await h.staff_set_context(delivery_fee_cents=2000, delivery_fee_status="FEE_SENT")
        await h.send("Yes")
        await h.send("Card")
        orders = await h.orders()
        assert len(orders) == 1
        assert orders[0].payment_status == "PENDING", (
            "a CARD choice (not cash-on-collection) must be recorded as PENDING, not auto-paid"
        )
    finally:
        await _cleanup(business.id)


# ── 19. Duplicate WhatsApp webhook events ─────────────────────────────────

async def test_19_duplicate_webhook_delivery_mutates_cart_exactly_once(test_business: Business, db):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        dup_id = f"wamid.{uuid.uuid4().hex[:16]}"
        await h.send("a classic smash burger", wa_message_id=dup_id)
        await h.send("a classic smash burger", wa_message_id=dup_id)  # EXACT same wa_message_id, redelivered
        cart = await h.cart()
        assert len(cart) == 1 and cart[0]["quantity"] == 1, (
            f"a redelivered webhook with the SAME wa_message_id must never double-add, got: {cart}"
        )
    finally:
        await _cleanup(business.id)


# ── 20. Out-of-order or concurrent worker processing ──────────────────────

async def test_20_concurrent_messages_for_the_same_customer_never_lose_or_duplicate_items(
    test_business: Business, db,
):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        # Deliberately no sequential "Hello" first -- a brand-new session
        # defaults to IDLE, which (unlike GREETING) is one of the states
        # pipeline.py's deterministic ORDER_ADD extraction covers, and
        # pipeline.py separately has a dedicated deterministic add-on path
        # for CONFIRMING_ORDER (whichever message loses the race lands
        # there once the winner locks the cart). This way both orderings
        # resolve deterministically, regardless of which message wins the
        # race -- this environment has no live LLM credentials, so a
        # message that falls through to the LLM here would hit a real
        # APIConnectionError, which is an environment limitation, not what
        # this concurrency test is checking.
        await asyncio.gather(
            h.send("add chips please"),
            h.send("add a buddy 440ml please"),
        )
        cart = await h.cart()
        names = {i["name"]: i["quantity"] for i in cart}
        assert names.get("Chips") == 1, f"concurrent delivery must never lose an item, got: {cart}"
        assert names.get("Buddy 440ml") == 1, f"concurrent delivery must never lose an item, got: {cart}"
    finally:
        await _cleanup(business.id)


# ── 21. Multiple restaurant tenants with conflicting product names ───────

async def test_21_multiple_tenants_with_the_same_item_name_never_cross_leak(db):
    unique_a = uuid.uuid4().hex[:8]
    unique_b = uuid.uuid4().hex[:8]
    from shared.utils import generate_business_code

    business_a = Business(
        name=f"Tenant A {unique_a}", slug=f"tenant-a-{unique_a}",
        business_code=generate_business_code(), currency="ZAR",
        timezone="Africa/Johannesburg", whatsapp_phone_number_id=f"acceptance-a-{unique_a}",
        is_active=True,
    )
    business_b = Business(
        name=f"Tenant B {unique_b}", slug=f"tenant-b-{unique_b}",
        business_code=generate_business_code(), currency="ZAR",
        timezone="Africa/Johannesburg", whatsapp_phone_number_id=f"acceptance-b-{unique_b}",
        is_active=True,
    )
    db.add_all([business_a, business_b])
    await db.flush()

    cat_a = MenuCategory(business_id=business_a.id, name="Mains", sort_order=0)
    cat_b = MenuCategory(business_id=business_b.id, name="Mains", sort_order=0)
    db.add_all([cat_a, cat_b])
    await db.flush()
    db.add_all([
        MenuItem(business_id=business_a.id, category_id=cat_a.id, name="Chips", price_cents=3590, is_active=True),
        MenuItem(business_id=business_b.id, category_id=cat_b.id, name="Chips", price_cents=5500, is_active=True),
    ])
    await db.commit()

    try:
        h_a = _Harness(business_a)
        h_b = _Harness(business_b)
        await h_a.send("Hello")
        await h_b.send("Hello")
        await h_a.send("chips please")
        await h_b.send("chips please")

        cart_a = await h_a.cart()
        cart_b = await h_b.cart()
        assert len(cart_a) == 1 and cart_a[0]["price_cents"] == 3590, f"tenant A price leaked: {cart_a}"
        assert len(cart_b) == 1 and cart_b[0]["price_cents"] == 5500, f"tenant B price leaked: {cart_b}"
    finally:
        for business_id in (business_a.id, business_b.id):
            await _cleanup(business_id)


# ── 22. Repeated confirmations and retries ────────────────────────────────

async def test_22_repeated_confirmation_after_order_placed_never_duplicates_the_order(
    test_business: Business, db,
):
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("a classic smash burger")
        await h.send("Yes")
        for _ in range(3):
            if await h.state() not in ("COLLECTING_DETAILS",):
                break
            await h.send("skip")
        orders_before = await h.orders()
        assert len(orders_before) == 1

        # Customer retries the SAME confirmation again after the order is
        # already placed (e.g. a slow/retried WhatsApp send on their end).
        await h.send("Yes")
        await h.send("Yes")
        orders_after = await h.orders()
        assert len(orders_after) == 1, (
            f"a repeated confirmation after the order is already placed must never create a "
            f"second order, got {len(orders_after)} orders"
        )
    finally:
        await _cleanup(business.id)
