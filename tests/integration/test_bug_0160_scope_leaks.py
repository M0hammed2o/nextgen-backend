"""
Regression tests for two High-severity findings from independent
verification (2026-10-26) of BUG-0160's price-ambiguity gate and BUG-0171's
contradiction/resilience guards. Both are permanent regressions against the
REAL pipeline and a real Postgres cart -- not just unit-level.

R1: the ORDER_REMOVE deterministic path (_det_remove_matches in
pipeline.py, around the "Deterministic ingredient-modifier interception
for ORDER_REMOVE" section) called _extract_items_from_message WITHOUT
ignore_price_ambiguity=True. Against a catalog with two differently-priced
Coke variants, "Remove the Coke" no longer matched "coke" as a real menu
item at all, fell through to the ingredient-modifier fallback, and
incorrectly applied "no coke" as a special_instructions modifier to an
unrelated cart item while the real Coke stayed in the cart, still charged.
Fixed by adding ignore_price_ambiguity=True at that call site (matching an
EXISTING cart line is never a new price commitment).

R2: CART_CORRECTION and ONLY_WANT clear the cart and transition state
BEFORE calling _handle_with_llm, expecting the LLM to rebuild it. If the
LLM call then fails, or its response is blocked as contradictory, the
function returned a normal (non-exception) graceful-fallback reply -- so
the OUTER caller committed the transaction with the cart left genuinely
empty, and the customer was never told anything was lost. Fixed by having
callers that destructively clear the cart in anticipation of an LLM
rebuild pass a `pre_mutation_snapshot`, restored by `_handle_with_llm`
whenever the anticipated rebuild doesn't happen.
"""
import uuid

import pytest
from sqlalchemy import delete, select, text
from unittest.mock import AsyncMock, patch

from backend.app.bot import state_machine
from backend.app.bot.pipeline import process_inbound_message
from backend.app.llm.provider import LLMProvider, LLMResponse
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.menu import MenuCategory, MenuItem
from shared.models.message import Message
from tests.integration._db import get_session_factory


class _FailingProvider(LLMProvider):
    """Raises on every call -- simulates a real provider outage/timeout."""

    async def complete(self, system_prompt, user_message, **kwargs):
        return await self.complete_with_history(system_prompt, [])

    async def complete_with_history(self, system_prompt, messages, **kwargs):
        raise ConnectionError("simulated provider outage")


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
                    contact_name="Scope Leak Regression Test",
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
    test_business.whatsapp_phone_number_id = f"scope-leak-{unique}"
    test_business.is_active = True
    db.add(test_business)

    cat = MenuCategory(business_id=test_business.id, name="Mains", sort_order=0)
    db.add(cat)
    await db.flush()
    db.add_all([
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Classic Smash Burger",
                 price_cents=7500, is_active=True),
        # Two differently-priced Coke variants -- the exact condition
        # BUG-0160's price-ambiguity gate requires disambiguation for.
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Coca-Cola (330ml)",
                 price_cents=2000, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Coca-Cola (500ml)",
                 price_cents=2500, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Chips",
                 price_cents=3590, is_active=True),
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


async def test_r1_remove_bare_coke_against_price_ambiguous_catalog_removes_the_coke_not_a_modifier(
    test_business: Business, db,
):
    """'Remove the Coke' against a catalog with 330ml/500ml variants must
    remove the ALREADY-IN-CART Coke line entirely -- it must NOT fall
    through to the ingredient-modifier fallback and apply "no coke" to an
    unrelated cart item while leaving the real Coke in the cart, charged."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("Can I get a Classic Smash Burger and a Coke (330ml)")
        cart_before = await h.cart()
        assert {i["name"] for i in cart_before} == {"Classic Smash Burger", "Coca-Cola (330ml)"}

        await h.send("Remove the Coke")
        cart_after = await h.cart()

        names_after = {i["name"] for i in cart_after}
        assert "Coca-Cola (330ml)" not in names_after, f"the Coke must actually be removed, got: {cart_after}"
        assert names_after == {"Classic Smash Burger"}, f"only the burger should remain, got: {cart_after}"
        burger = next(i for i in cart_after if i["name"] == "Classic Smash Burger")
        assert burger.get("special_instructions") != "no coke", (
            f"the burger must never be silently modified with 'no coke', got: {burger}"
        )
    finally:
        await _cleanup(business.id)


async def test_r2a_only_want_with_a_failing_llm_never_commits_an_emptied_cart(test_business: Business, db):
    """ONLY_WANT clears the cart BEFORE calling the LLM, expecting it to
    rebuild. If the LLM call fails, the pre-clear cart must be restored --
    the customer must never silently lose their order because of a
    transient provider failure."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("2 chips and a classic smash burger")
        cart_before = await h.cart()
        assert len(cart_before) == 2

        with patch("backend.app.llm.provider.get_llm_provider", return_value=_FailingProvider()):
            response = await h.send("I only want the burger")

        cart_after = await h.cart()
        assert cart_after == cart_before, (
            f"a failed LLM call during ONLY_WANT must restore the pre-clear cart, "
            f"not leave it emptied: before={cart_before}, after={cart_after}"
        )
        assert "trouble" in response.lower() or "try again" in response.lower(), (
            f"expected the graceful fallback message, got: {response!r}"
        )
    finally:
        await _cleanup(business.id)


async def test_r2b_cart_correction_with_a_failing_llm_never_commits_an_emptied_cart(test_business: Business, db):
    """Same as R2a, for the CART_CORRECTION path ('actually I want...' style
    full-order restatement)."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    try:
        await h.send("Hello")
        await h.send("2 chips and a classic smash burger")
        cart_before = await h.cart()
        assert len(cart_before) == 2

        with patch("backend.app.llm.provider.get_llm_provider", return_value=_FailingProvider()):
            # Deliberately names no real menu item -- so pipeline.py's OWN
            # internal deterministic pre-LLM extraction (inside
            # _handle_with_llm, used for ORDER_START) finds nothing and the
            # real LLM provider genuinely must be called, exercising the
            # restore-on-failure path rather than being resolved
            # deterministically before ever reaching the provider.
            response = await h.send("Actually make it something else entirely")

        cart_after = await h.cart()
        assert cart_after == cart_before, (
            f"a failed LLM call during CART_CORRECTION must restore the pre-clear cart, "
            f"not leave it emptied: before={cart_before}, after={cart_after}"
        )
        assert "trouble" in response.lower() or "try again" in response.lower(), (
            f"expected the graceful fallback message, got: {response!r}"
        )
    finally:
        await _cleanup(business.id)
