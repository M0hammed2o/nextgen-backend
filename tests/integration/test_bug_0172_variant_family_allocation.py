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
        assert "sorted" in reply2.lower(), f"expected resolution message, got: {reply2!r}"

        pending2 = await h.pending_allocation()
        assert pending2 is None, "allocation must be cleared once fully resolved"

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
