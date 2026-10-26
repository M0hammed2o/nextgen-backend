"""
Layer 3 adversarial testing (WhatsApp ordering reliability directive,
2026-10-26), pipeline-integration tier: proves the resilience/hallucination
guards hold end-to-end through the REAL pipeline against a real Postgres
cart/session, not just at the isolated llm_parser unit level (that coverage
already exists in tests/test_llm_parser_adversarial.py).

Each test patches backend.app.llm.provider.get_llm_provider so the exact
adversarial condition under test (hallucinated item, malformed JSON,
contradictory fields, a raised exception simulating a timeout/failed API
call) is the ONLY variable -- deterministic pre-LLM extraction is
deliberately bypassed by using item names/phrasing that don't exist in this
test business's own tiny catalog, forcing every turn through the real LLM
code path in backend/app/bot/pipeline.py::_handle_with_llm.

Every assertion reads the REAL persisted cart via state_machine.get_cart,
never the AI-generated reply text alone -- the whole point of this tier is
proving the backend stays the source of truth even when the "AI" misbehaves.
"""
import uuid

import pytest
from sqlalchemy import delete, text
from unittest.mock import AsyncMock, patch

from backend.app.bot import state_machine
from backend.app.bot.pipeline import process_inbound_message
from backend.app.llm.provider import LLMProvider, LLMResponse
from shared.models.business import Business
from shared.models.customer import ConversationSession, Customer
from shared.models.menu import MenuCategory, MenuItem
from shared.models.message import Message
from tests.integration._db import get_session_factory


class _QueuedProvider(LLMProvider):
    """Returns pre-queued raw response texts verbatim (no JSON re-encoding --
    unlike the replay suite's ScriptedLLMProvider, this lets a test hand the
    pipeline deliberately malformed/raw text). Call .raise_next(exc) to make
    the NEXT call raise instead of returning a queued response."""

    def __init__(self) -> None:
        self._queue: list[str] = []
        self._next_exception: Exception | None = None
        self.call_count = 0

    def enqueue_raw(self, raw_text: str) -> None:
        self._queue.append(raw_text)

    def raise_next(self, exc: Exception) -> None:
        self._next_exception = exc

    async def complete(self, system_prompt: str, user_message: str, **kwargs) -> LLMResponse:
        return await self.complete_with_history(
            system_prompt, [{"role": "user", "content": user_message}]
        )

    async def complete_with_history(self, system_prompt: str, messages: list[dict], **kwargs) -> LLMResponse:
        self.call_count += 1
        if self._next_exception is not None:
            exc, self._next_exception = self._next_exception, None
            raise exc
        raw = self._queue.pop(0) if self._queue else '{"action": "chitchat", "message": "...", "items": []}'
        return LLMResponse(
            text=raw, input_tokens=0, output_tokens=0, total_tokens=0,
            model="adversarial-test", provider="test", cost_cents=0,
        )


class _Harness:
    """Same real-pipeline harness pattern as test_scenario_d_quantity_handling.py."""

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
                    contact_name="Layer3 Adversarial Test",
                )
        return captured.get("text") or ""

    async def cart(self) -> list[dict]:
        from sqlalchemy import select
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
    test_business.whatsapp_phone_number_id = f"layer3-adversarial-{unique}"
    test_business.is_active = True
    db.add(test_business)

    cat = MenuCategory(business_id=test_business.id, name="Mains", sort_order=0)
    db.add(cat)
    await db.flush()
    db.add_all([
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Grilled Fish",
                 price_cents=12990, is_active=True),
        MenuItem(business_id=test_business.id, category_id=cat.id, name="Rice",
                 price_cents=2990, is_active=True),
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


async def test_hallucinated_item_is_never_added_to_the_real_cart(test_business: Business, db):
    """LLM claims it added an item that doesn't exist on this business's menu
    at all -- the persisted cart must stay empty, and the customer must be
    told honestly, never shown a fake success for a non-existent item."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    provider = _QueuedProvider()
    try:
        with patch("backend.app.llm.provider.get_llm_provider", return_value=provider):
            await h.send("Hello")
            provider.enqueue_raw(
                '{"action": "add_items", "message": "Added your Dragon Fruit Smoothie!", '
                '"items": [{"name": "Dragon Fruit Smoothie", "quantity": 1}]}'
            )
            response = await h.send("something that forces the LLM path, no real item named")
        cart = await h.cart()
        assert cart == [], f"a hallucinated, non-existent item must never reach the real cart, got: {cart}"
        assert "dragon fruit" not in response.lower() or "couldn't find" in response.lower() or "sorry" in response.lower(), (
            f"response must not present the hallucinated item as successfully added: {response!r}"
        )
    finally:
        await _cleanup(business.id)


async def test_malformed_json_response_never_crashes_and_leaves_cart_unchanged(test_business: Business, db):
    """A response that isn't valid JSON at all (truncated, prose-wrapped, or
    garbage) must be handled gracefully by llm_parser's fallback -- the whole
    pipeline call must complete, return SOME reply, and never mutate the
    cart from unparseable input."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    provider = _QueuedProvider()
    try:
        with patch("backend.app.llm.provider.get_llm_provider", return_value=provider):
            await h.send("Hello")
            provider.enqueue_raw(
                "Sure, let me help you with that! I think you might want "
                "the Grilled Fish but I'm not fully certain -- {\"action\": "
            )
            response = await h.send("something that forces the LLM path")
        assert response, "pipeline must return SOME reply, never silently swallow a malformed response"
        cart = await h.cart()
        assert cart == [], f"malformed/unparseable LLM output must never mutate the cart, got: {cart}"
    finally:
        await _cleanup(business.id)


async def test_contradictory_add_items_action_with_empty_items_never_claims_a_phantom_addition(test_business: Business, db):
    """action='add_items' but the items list is empty -- a self-contradictory
    response (claims a mutation action with nothing to mutate). The cart must
    stay empty and the reply must never claim something concrete was added."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    provider = _QueuedProvider()
    try:
        with patch("backend.app.llm.provider.get_llm_provider", return_value=provider):
            await h.send("Hello")
            provider.enqueue_raw(
                '{"action": "add_items", "message": "Added to your order! Total is now R250.00", "items": []}'
            )
            response = await h.send("something that forces the LLM path")
        cart = await h.cart()
        assert cart == [], f"action=add_items with an empty items list must never mutate the cart, got: {cart}"
        assert "r250" not in response.lower().replace(" ", ""), (
            f"must never relay a fabricated total for a mutation that never happened: {response!r}"
        )
    finally:
        await _cleanup(business.id)


async def test_chitchat_falsely_claiming_a_cart_update_is_blocked_not_relayed(test_business: Business, db):
    """BUG-0157/BO-000001 guard, exercised at the full pipeline level: once a
    real cart exists, an LLM 'chitchat' reply that falsely claims a quantity
    update happened (no structural mutation occurred) must be intercepted,
    never shown verbatim to the customer."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    provider = _QueuedProvider()
    try:
        with patch("backend.app.llm.provider.get_llm_provider", return_value=provider):
            await h.send("Hello")
            provider.enqueue_raw(
                '{"action": "add_items", "message": "Added 1 Grilled Fish", '
                '"items": [{"name": "Grilled Fish", "quantity": 1}]}'
            )
            await h.send("something that forces the LLM path for the first add")
            cart_before = await h.cart()
            assert len(cart_before) == 1 and cart_before[0]["quantity"] == 1

            provider.enqueue_raw(
                '{"action": "chitchat", "message": "Got it! Updated to 5 Grilled Fish. '
                'Your total is now R649.50.", "items": []}'
            )
            # Deliberately avoids BO-000001's own correction-signal phrases
            # ("instead of" / "rather than" / "going to be" / "not N") and
            # _detect_named_quantity_set's verb+single-number shape, so this
            # message is NOT intercepted deterministically -- it must
            # genuinely reach the LLM for this test to exercise the
            # CHITCHAT_WITH_ACTIVE_CART guard rather than the (separately
            # and correctly tested) deterministic quantity-correction path.
            response = await h.send("what about the grilled fish, can we sort that out")
        cart_after = await h.cart()
        assert cart_after == cart_before, (
            f"a chitchat reply with NO real structural mutation must never be allowed to silently "
            f"change what the customer believes is in their cart: before={cart_before}, after={cart_after}"
        )
        assert "r649.50" not in response.lower().replace(" ", ""), (
            f"the hallucinated new total must never be relayed verbatim to the customer: {response!r}"
        )
    finally:
        await _cleanup(business.id)


async def test_llm_provider_exception_returns_a_graceful_reply_not_silence(test_business: Business, db):
    """A raised provider exception (timeout, rate limit, auth error, network
    error -- all realistic in production) must never propagate as an
    unhandled crash and must never result in total silence to the customer.
    The cart must also be completely unaffected, since nothing was about to
    mutate it."""
    business = await _setup_business(test_business, db)
    h = _Harness(business)
    provider = _QueuedProvider()
    try:
        with patch("backend.app.llm.provider.get_llm_provider", return_value=provider):
            await h.send("Hello")
            provider.raise_next(TimeoutError("simulated provider timeout"))
            response = await h.send("something that forces the LLM path and then times out")
        assert response, "a provider exception must still produce a graceful reply, never silence"
        assert "trouble" in response.lower() or "sorry" in response.lower() or "try again" in response.lower(), (
            f"expected an honest, actionable fallback message, got: {response!r}"
        )
        cart = await h.cart()
        assert cart == [], f"a failed LLM call must never leave a partial/corrupt cart mutation, got: {cart}"

        # The session must still be perfectly usable on the NEXT message --
        # proves the exception didn't leave the DB transaction/session in a
        # broken state.
        provider.enqueue_raw(
            '{"action": "add_items", "message": "Added 1 Grilled Fish", '
            '"items": [{"name": "Grilled Fish", "quantity": 1}]}'
        )
        with patch("backend.app.llm.provider.get_llm_provider", return_value=provider):
            await h.send("something that forces the LLM path, this time succeeding")
        recovered_cart = await h.cart()
        assert len(recovered_cart) == 1 and recovered_cart[0]["name"] == "Grilled Fish", (
            f"the session must recover cleanly on the next message after a provider failure, got: {recovered_cart}"
        )
    finally:
        await _cleanup(business.id)
