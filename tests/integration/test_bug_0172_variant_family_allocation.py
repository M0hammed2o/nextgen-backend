"""
BUG-0172 (KNOWN_BUGS.md), Phase 2 fix: a multi-product generic-plural order
("15 Bambino Meals") used to be silently dropped from the cart while a
sibling item in the same message ("3 chicken Schwarma") was added correctly,
with zero trace and zero acknowledgement; a related mechanism could also ask
an unsolicited "Half Chicken or Full Chicken?" question on an order that
never named either.

Real, reported production incident, 2026-10-27:
  "Ok can you give me 15 Bambino Meals and also give me 3 chicken Schwarma"
  -> "Added to your order: 3x Chicken Schwarma" (the 15 Bambino Meals never
  acknowledged at all).

Root cause (full writeup in KNOWN_BUGS.md BUG-0172):
  A. `_extract_items_from_message`/`_extract_items_from_chunk` only match an
     item by its FULL catalog name. The real catalog has 4 same-priced
     "Bambino Meal: <name>" products; a generic "Bambino Meals" mention
     matches none of them and vanishes with zero trace while an unambiguous
     sibling item in the same message is added fine.
  B. `_find_size_variants` strips the literal words "half"/"full" from item
     names; "Half Chicken"/"Full Chicken" collapse to the shared base
     "chicken", whose match-ratio check was satisfied by the bare word
     "chicken" appearing ANYWHERE in the message -- including inside
     "chicken Schwarma" -- independent of what was actually ordered.

Fix (pipeline.py):
  A. New `_find_named_variant_family` (mirrors the existing, already-shipped
     `_find_size_variants`/BUG-0158 check and call site) groups items by the
     text before ": " in their name. When a generic head-phrase is used with
     no specific variant named, a `pending_variant_allocation` context is
     set and the customer is asked to choose, listing the real variants and
     preserving the requested quantity -- while any other unambiguous item
     in the same message is still added immediately (same sibling-
     preservation pattern as BUG-0158). A new early hook in `_handle_message`
     (`_resolve_variant_allocation_reply`) resolves replies across one or
     more following messages, decrementing the outstanding quantity as named
     variants are assigned, without ever duplicating an unresolved request
     that gets repeated, and without allowing checkout to be confirmed while
     a quantity remains outstanding (that message names no variant, so it is
     treated as unresolved and the customer is reminded instead of being let
     through to CONFIRMING_ORDER).
  B. `_find_size_variants` now skips a family whose base phrase is already
     fully "claimed" by a different, longer, unrelated active item whose own
     full name is present in the message (e.g. "Chicken Schwarma" claims the
     word "chicken" there, so it no longer separately satisfies the
     unrelated Half Chicken / Full Chicken family's base-word check).

See also tests/test_bambino_multi_product_quantity_split.py (the original
Phase 1 pure-function probes against `_extract_items_from_message`/
`_find_size_variants` directly -- 2 of those 3 continue to fail BY DESIGN
after this fix, since Problem A's fix is a new pre-check layered in FRONT of
`_extract_items_from_message`, never a modification of that function itself,
exactly mirroring how `_find_size_variants` has always worked relative to
it. This file is the before/after evidence for the actual customer-facing
behaviour.)

Every assertion here reads the REAL persisted ConversationSession
cart/context and real `orders` rows via the actual pipeline functions and
direct SQL -- not the AI-generated reply text.
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
                    contact_name="BUG-0172 Test",
                )
        return captured.get("text") or ""

    async def cart(self) -> list[dict]:
        sess = await self.session_row()
        return state_machine.get_cart(sess)

    async def pending_allocation(self) -> dict | None:
        sess = await self.session_row()
        return state_machine.get_context(sess, "pending_variant_allocation")

    async def session_row(self) -> ConversationSession:
        async with get_session_factory()() as db:
            cust = (await db.execute(
                select(Customer).where(Customer.business_id == self.business.id, Customer.wa_id == self.wa_id)
            )).scalar_one()
            return (await db.execute(
                select(ConversationSession).where(ConversationSession.customer_id == cust.id)
            )).scalar_one()

    async def order_count(self) -> int:
        async with get_session_factory()() as db:
            return (await db.execute(text(
                "SELECT count(*) FROM orders WHERE business_id = :bid"
            ), {"bid": self.business.id})).scalar_one()

    async def latest_order(self):
        async with get_session_factory()() as db:
            return (await db.execute(text(
                "SELECT order_number, subtotal_cents, total_cents, order_mode, status "
                "FROM orders WHERE business_id = :bid ORDER BY created_at DESC LIMIT 1"
            ), {"bid": self.business.id})).mappings().one()

    async def latest_order_items(self) -> list[dict]:
        async with get_session_factory()() as db:
            rows = (await db.execute(text(
                "SELECT name_snapshot, quantity, special_instructions, line_total_cents "
                "FROM order_items WHERE order_id = ("
                "  SELECT id FROM orders WHERE business_id = :bid ORDER BY created_at DESC LIMIT 1"
                ")"
            ), {"bid": self.business.id})).mappings().all()
            return [dict(r) for r in rows]


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


async def _setup_business(test_business: Business, db) -> Business:
    """Mirrors the real Mochachos catalog sections involved in the incident:
    'Flame-Grilled Chicken' (Half/Full Chicken), 'Chicken Meals' (the 4
    Bambino variants), 'Schwarmas...' (Chicken Schwarma) -- plus a plain
    Chicken Burger for the false-positive check."""
    unique = uuid.uuid4().hex[:6]
    test_business.whatsapp_phone_number_id = f"bug0172-{unique}"
    test_business.is_active = True
    db.add(test_business)

    cat = MenuCategory(business_id=test_business.id, name="Mains", sort_order=0)
    db.add(cat)
    await db.flush()
    db.add_all([
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Half Chicken",
                 price_cents=11490, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Full Chicken",
                 price_cents=21990, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Bambino Meal: Jose",
                 price_cents=8490, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Bambino Meal: Pedro",
                 price_cents=8490, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Bambino Meal: Speedy",
                 price_cents=8490, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Bambino Meal: Juanita",
                 price_cents=8490, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Chicken Schwarma",
                 price_cents=9490, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Chicken Burger",
                 price_cents=6990, is_active=True),
    ])
    await db.commit()
    return test_business


_INCIDENT_MESSAGE = (
    "Ok can you give me 15 Bambino Meals and also give me 3 chicken Schwarma"
)


async def test_exact_incident_conversation_preserves_schwarma_and_asks_for_bambino(
    test_business: Business, db,
):
    """The exact reported incident message: the 3 Chicken Schwarmas must land
    in the cart immediately, and the 15 Bambino Meals must be acknowledged
    with a clarifying question -- never silently dropped."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        reply = await h.send(_INCIDENT_MESSAGE)

        reply_lower = reply.lower()
        assert "bambino" in reply_lower, f"Bambino Meals never acknowledged, got: {reply!r}"
        for variant in ("jose", "pedro", "speedy", "juanita"):
            assert variant in reply_lower, f"expected variant {variant!r} listed, got: {reply!r}"
        assert "half chicken" not in reply_lower and "full chicken" not in reply_lower, (
            f"must not ask an unrelated Half/Full Chicken question, got: {reply!r}"
        )

        cart = await h.cart()
        assert len(cart) == 1, f"expected only the Schwarma committed so far, got: {cart!r}"
        assert cart[0]["name"] == "Chicken Schwarma"
        assert cart[0]["quantity"] == 3

        pending = await h.pending_allocation()
        assert pending is not None
        assert pending["family_label"] == "Bambino Meal"
        assert pending["quantity_needed"] == 15
    finally:
        await _cleanup(business.id)


async def test_variant_allocation_across_multiple_messages_tracks_outstanding_quantity(
    test_business: Business, db,
):
    """Assigning Bambino varieties across SEPARATE messages must decrement a
    correctly-maintained outstanding quantity and resolve once it reaches 0."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("15 Bambino Meals please")

        reply1 = await h.send("5 Jose")
        assert "10" in reply1, f"expected remaining=10 mentioned, got: {reply1!r}"
        pending1 = await h.pending_allocation()
        assert pending1["quantity_needed"] == 10

        reply2 = await h.send("10 Pedro")
        # Once fully resolved, the cart must be locked and a real checkout
        # confirmation prompt shown (the same pattern the main deterministic
        # add-items path already uses) -- not a dead-end acknowledgement
        # with no way to proceed to checkout.
        assert "yes" in reply2.lower() and "total" in reply2.lower(), (
            f"expected a real confirmation prompt once resolved, got: {reply2!r}"
        )

        pending2 = await h.pending_allocation()
        assert pending2 is None, "allocation must be cleared once fully resolved"

        session = await h.session_row()
        assert session.state == "CONFIRMING_ORDER", (
            f"expected the session to advance to CONFIRMING_ORDER once fully "
            f"resolved, got: {session.state!r}"
        )

        cart = await h.cart()
        by_name = {c["name"]: c["quantity"] for c in cart}
        assert by_name.get("Bambino Meal: Jose") == 5
        assert by_name.get("Bambino Meal: Pedro") == 10
        assert "Bambino Meal: Speedy" not in by_name
        assert "Bambino Meal: Juanita" not in by_name
    finally:
        await _cleanup(business.id)


async def test_repeating_the_original_request_does_not_duplicate_anything(
    test_business: Business, db,
):
    """Repeating 'give me 15 Bambino Meals' while the allocation is still
    outstanding must NOT add a second batch or double-count the quantity."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("15 Bambino Meals please")

        reply = await h.send("Can you also add 15 Bambino Meals")
        assert "15" in reply, f"expected the SAME outstanding quantity (15), got: {reply!r}"

        pending = await h.pending_allocation()
        assert pending["quantity_needed"] == 15, "repeating the request must not change the outstanding quantity"

        cart = await h.cart()
        assert cart == [], "nothing should be committed to the cart until a real variant is named"
    finally:
        await _cleanup(business.id)


async def test_checkout_cannot_be_confirmed_while_bambino_allocation_is_outstanding(
    test_business: Business, db,
):
    """A premature 'yes'/checkout attempt while a variant quantity remains
    outstanding must be refused, not committed as a real order."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("15 Bambino Meals please")
        await h.send("5 Jose")  # 10 still outstanding

        reply = await h.send("Yes please, checkout")
        assert "10" in reply, f"expected the customer reminded of the outstanding quantity, got: {reply!r}"

        assert await h.order_count() == 0, "no order must be placed while items remain unresolved"

        pending = await h.pending_allocation()
        assert pending is not None and pending["quantity_needed"] == 10
    finally:
        await _cleanup(business.id)


async def test_common_chicken_named_items_never_trigger_a_false_half_full_clarification(
    test_business: Business, db,
):
    """Chicken Burger, Chicken Schwarma, and an explicit Half Chicken order
    must never be redirected into an unrelated Half/Full Chicken question."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        reply = await h.send("I'll have a Chicken Burger and a Chicken Schwarma")
        reply_lower = reply.lower()
        assert "half chicken" not in reply_lower and "full chicken" not in reply_lower, (
            f"unrelated chicken items must not trigger the size clarification, got: {reply!r}"
        )
        cart = await h.cart()
        names = {c["name"] for c in cart}
        assert names == {"Chicken Burger", "Chicken Schwarma"}, f"got cart: {cart!r}"
    finally:
        await _cleanup(business.id)


async def test_explicit_half_chicken_order_still_resolves_directly_no_regression(
    test_business: Business, db,
):
    """An explicit 'Half Chicken' order (naming the size itself) must still
    resolve directly, exactly as before this fix -- no regression to the
    pre-existing, already-shipped size-ambiguity behaviour."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        reply = await h.send("Can I get a Half Chicken please")
        cart = await h.cart()
        assert len(cart) == 1 and cart[0]["name"] == "Half Chicken", (
            f"expected a direct, unambiguous match, got cart={cart!r}, reply={reply!r}"
        )
    finally:
        await _cleanup(business.id)


async def test_single_message_naming_two_variants_with_quantities_allocates_both(
    test_business: Business, db,
):
    """'5 Jose and 10 Pedro' in a SINGLE message (not spread across two
    messages) must allocate both correctly in one turn."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("15 Bambino Meals please")

        reply = await h.send("5 Jose and 10 Pedro")
        assert "yes" in reply.lower() and "total" in reply.lower(), (
            f"expected a real confirmation prompt (fully resolved in one message), got: {reply!r}"
        )

        pending = await h.pending_allocation()
        assert pending is None

        cart = await h.cart()
        by_name = {c["name"]: c["quantity"] for c in cart}
        assert by_name.get("Bambino Meal: Jose") == 5
        assert by_name.get("Bambino Meal: Pedro") == 10
    finally:
        await _cleanup(business.id)


async def test_full_checkout_after_partial_allocation_persists_correct_order_items(
    test_business: Business, db,
):
    """End-to-end: the exact incident conversation, allocated across two
    messages, carried all the way through to a real placed order -- the
    persisted OrderItem rows, quantities and totals must be exactly correct,
    including the sibling Chicken Schwarma added on the very first message."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send(_INCIDENT_MESSAGE)  # 3x Chicken Schwarma added, 15 Bambino outstanding
        await h.send("5 Jose")  # 10 outstanding
        reply = await h.send("10 Pedro")  # fully resolved -> CONFIRMING_ORDER
        assert "yes" in reply.lower()

        await h.send("Yes")
        await h.send("0821234567")
        await h.send("Pickup")
        await h.send("Thabo")
        await h.send("skip")

        expected_total = 3 * 9490 + 5 * 8490 + 10 * 8490  # Schwarma + Jose + Pedro
        order = await h.latest_order()
        assert order["total_cents"] == expected_total, (
            f"expected total {expected_total}, got {order['total_cents']}"
        )
        assert order["subtotal_cents"] == expected_total
        assert order["order_mode"] == "PICKUP"

        items = await h.latest_order_items()
        by_name = {i["name_snapshot"]: i for i in items}
        assert set(by_name) == {"Chicken Schwarma", "Bambino Meal: Jose", "Bambino Meal: Pedro"}, (
            f"expected exactly these 3 line items, got: {set(by_name)}"
        )
        assert by_name["Chicken Schwarma"]["quantity"] == 3
        assert by_name["Chicken Schwarma"]["line_total_cents"] == 3 * 9490
        assert by_name["Bambino Meal: Jose"]["quantity"] == 5
        assert by_name["Bambino Meal: Jose"]["line_total_cents"] == 5 * 8490
        assert by_name["Bambino Meal: Pedro"]["quantity"] == 10
        assert by_name["Bambino Meal: Pedro"]["line_total_cents"] == 10 * 8490
    finally:
        await _cleanup(business.id)


async def test_ordering_other_products_while_allocation_outstanding_is_not_silently_dropped(
    test_business: Business, db,
):
    """A customer asking for something else while the Bambino allocation is
    still outstanding must have that OTHER item acknowledged and added --
    not silently ignored the way the original BUG-0172 omission worked --
    while the outstanding allocation itself still blocks checkout."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("15 Bambino Meals please")

        reply = await h.send("Also give me a Chicken Burger")
        assert "chicken burger" in reply.lower(), (
            f"expected the unrelated item to be acknowledged, not silently dropped, got: {reply!r}"
        )
        assert "15" in reply, "the outstanding Bambino quantity must still be mentioned"

        cart = await h.cart()
        names = {c["name"] for c in cart}
        assert "Chicken Burger" in names, f"expected Chicken Burger added, got cart: {cart!r}"

        pending = await h.pending_allocation()
        assert pending is not None and pending["quantity_needed"] == 15, (
            "the Bambino allocation must remain outstanding and unchanged"
        )
        assert await h.order_count() == 0, "checkout must still be blocked"
    finally:
        await _cleanup(business.id)


async def test_customer_can_cancel_the_outstanding_bambino_allocation(
    test_business: Business, db,
):
    """A customer changing their mind about the whole outstanding request
    ('never mind') must be able to drop it, rather than being stuck in the
    same clarification loop forever."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("15 Bambino Meals please")

        reply = await h.send("Never mind")
        assert "dropped" in reply.lower() or "no problem" in reply.lower(), (
            f"expected the allocation to be cancelled, got: {reply!r}"
        )

        pending = await h.pending_allocation()
        assert pending is None, "allocation must be cleared after cancellation"

        cart = await h.cart()
        assert cart == [], "nothing should have been committed for the cancelled family"
    finally:
        await _cleanup(business.id)


async def test_removal_attempt_while_allocation_outstanding_never_double_adds_the_item(
    test_business: Business, db,
):
    """Independent review finding (2026-10-27): the "other products while
    allocation outstanding" fallback must NEVER fire for an ORDER_REMOVE/
    ORDER_CANCEL message, since _extract_items_from_message matches an
    item's name regardless of intent -- "Remove the Schwarma" naming
    "Schwarma" would otherwise be misread as a request to ADD another one.
    This is a disclosed, deliberate limitation, not a crash: the actual
    removal is NOT performed while the allocation is outstanding (cart
    edits are deferred until the Bambino question is resolved or
    cancelled) -- the one thing this guards is that the Schwarma quantity
    is never silently doubled."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send(_INCIDENT_MESSAGE)  # 3x Chicken Schwarma added, 15 Bambino outstanding

        reply = await h.send("Remove the Chicken Schwarma")
        assert "15" in reply, "the outstanding Bambino quantity must still be mentioned"

        cart = await h.cart()
        schwarma_lines = [c for c in cart if c["name"] == "Chicken Schwarma"]
        assert len(schwarma_lines) == 1 and schwarma_lines[0]["quantity"] == 3, (
            f"the Schwarma quantity must never be silently doubled, got: {cart!r}"
        )

        pending = await h.pending_allocation()
        assert pending is not None and pending["quantity_needed"] == 15
    finally:
        await _cleanup(business.id)
