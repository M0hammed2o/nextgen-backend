"""
Conversation Replay Framework.

Executes complete customer conversations through the real pipeline code.
All external I/O is replaced with deterministic in-process fakes:

  - LLM calls      → ScriptedLLMProvider (returns pre-scripted ParsedLLMResponse)
  - Database reads  → menu / specials / history injected from fixture data
  - Database writes → order_creator mocked; FakeOrder captured for assertions
  - WhatsApp send   → no-op (responses captured from _handle_message return value)
  - Redis SSE       → no-op

Usage
-----
    runner = ReplayRunner(conv, business, customer, menu_items)
    await runner.run()      # raises AssertionError on any mismatch

Each conversation JSON turn may contain:
    {
      "message":  str,                # customer's WhatsApp message
      "llm":      dict | null,        # scripted LLM response (null = expect DET)
      "before":   {"set_context": {}},# apply to session before this message
      "expect":   {                   # assertions (all optional)
        "state":              str,
        "cart":               list | null,       # null = skip check
        "confirmed_cart":     list | null,       # null = skip check
        "response_contains":  list[str],
        "response_not_contains": list[str],
      }
    }

Staff-action turns (no customer message):
    {"_type": "staff_action", "context_updates": {...}}

Final-order assertion (after all turns):
    {
      "final_order": {
        "items":         list[{"name":str, "quantity":int}],
        "subtotal_cents": int,     # optional
        "total_cents":    int,     # optional
        "order_mode":     str,     # optional
        "customer_name":  str,     # optional
      }
    }
"""

import copy
import json
import uuid
from contextlib import AsyncExitStack
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from backend.app.bot import state_machine
from backend.app.bot.normalizer import normalize
from backend.app.bot import intent_router
from backend.app.llm.provider import LLMProvider, LLMResponse
from tests.replay.fixtures import FakeBusiness, FakeCustomer, FakeMenuItem, FakeOrder


# ── Scripted LLM provider ──────────────────────────────────────────────────────

class ScriptedLLMProvider(LLMProvider):
    """
    Returns pre-scripted JSON responses instead of calling a real LLM.

    Each spec dict is encoded as the JSON action block that llm_parser expects.
    Format:
        {
          "action": "add_items" | "remove_item" | "replace_item" |
                    "ask_options" | "chitchat" | "confirm_order" |
                    "cancel_order" | "recommend_items" | "handoff",
          "message": "...",        # bot-facing text (used for ask_options/chitchat)
          "items": [ {...}, ... ]  # items list (structure matches ParsedItem)
        }
    """

    def __init__(self) -> None:
        self._queue: list[dict] = []
        self.call_count: int = 0

    def enqueue(self, spec: dict | None) -> None:
        if spec:
            self._queue.append(spec)

    async def complete(self, system_prompt: str, user_message: str, **kwargs) -> LLMResponse:
        return await self.complete_with_history(
            system_prompt, [{"role": "user", "content": user_message}]
        )

    async def complete_with_history(
        self,
        system_prompt: str,
        messages: list[dict],
        **kwargs,
    ) -> LLMResponse:
        self.call_count += 1

        if self._queue:
            spec = self._queue.pop(0)
        else:
            # No scripted response → return a clearly identifiable error chitchat
            spec = {
                "action": "chitchat",
                "message": (
                    "[REPLAY ERROR: no scripted LLM response for this turn "
                    "— add an 'llm' key to the conversation JSON]"
                ),
                "items": [],
            }

        raw_text = json.dumps({
            "action": spec.get("action", "chitchat"),
            "message": spec.get("message", ""),
            "items": spec.get("items", []),
        })

        return LLMResponse(
            text=raw_text,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            model="scripted",
            provider="replay",
            cost_cents=0,
        )


# ── Replay session ─────────────────────────────────────────────────────────────

class ReplaySession:
    """
    In-memory ConversationSession substitute.
    flag_modified is no-op because there is no SQLAlchemy session.
    """

    def __init__(self, state: str = "IDLE") -> None:
        self.id = uuid.uuid4()
        self.state = state
        self.context_json: dict = {}


# ── Cart / order assertion helpers ────────────────────────────────────────────

def _normalise_cart_item(item: dict) -> dict:
    """Normalise a cart-item dict for comparison."""
    return {
        "name": item["name"].lower().strip(),
        "quantity": int(item.get("quantity", 1)),
        "special_instructions": (item.get("special_instructions") or "").lower().strip(),
    }


def _cart_item_matches(actual: dict, expected: dict) -> bool:
    """Check one expected item spec against one actual cart item."""
    if actual["name"] != expected["name"].lower().strip():
        return False
    if actual["quantity"] != int(expected.get("quantity", 1)):
        return False
    # Only check special_instructions when explicitly specified in expected
    if "special_instructions" in expected:
        exp_si = (expected["special_instructions"] or "").lower().strip()
        act_si = (actual.get("special_instructions") or "").lower().strip()
        if exp_si and exp_si not in act_si:
            return False
        if not exp_si and act_si:
            return False
    return True


def assert_carts_equal(
    actual: list[dict],
    expected: list[dict],
    label: str = "cart",
) -> None:
    """
    Assert that the actual cart matches the expected spec list.
    Items are compared sorted by name + quantity + special_instructions.
    """
    assert actual is not None, f"{label}: actual is None"
    assert len(actual) == len(expected), (
        f"{label}: expected {len(expected)} item(s), got {len(actual)}.\n"
        f"  actual:   {_fmt_cart(actual)}\n"
        f"  expected: {_fmt_cart(expected)}"
    )
    key = lambda x: (
        (x.get("name") or "").lower(),
        int(x.get("quantity") or 1),
        (x.get("special_instructions") or "").lower(),
    )
    actual_s = sorted(actual, key=key)
    expected_s = sorted(expected, key=key)

    for i, (act, exp) in enumerate(zip(actual_s, expected_s)):
        act_n = _normalise_cart_item(act)
        assert _cart_item_matches(act_n, exp), (
            f"{label}[{i}] mismatch.\n"
            f"  actual:   {act}\n"
            f"  expected: {exp}"
        )


def _fmt_cart(items: list) -> str:
    if not items:
        return "[]"
    parts = []
    for it in items:
        name = it.get("name", "?")
        qty = it.get("quantity", 1)
        si = it.get("special_instructions", "")
        parts.append(f"{qty}×{name}" + (f"({si})" if si else ""))
    return "[" + ", ".join(parts) + "]"


# ── Fake DB helper ─────────────────────────────────────────────────────────────

def _make_fake_db(orders: list[FakeOrder]) -> AsyncMock:
    """
    Return an AsyncMock that satisfies every DB call made during a turn:
      - db.execute(...) for UPDATE/SELECT queries
      - db.add(...)
      - db.flush()
    """
    db = AsyncMock()

    # A fake row returned by _finalize_pending_delivery_order's UPDATE RETURNING
    fake_row = MagicMock()
    fake_row.order_number = "BO-001"
    fake_row.subtotal_cents = 0   # replaced at assertion time

    result_mock = MagicMock()
    result_mock.one_or_none.return_value = fake_row
    result_mock.scalar_one.return_value = 1           # order_number_sequence
    result_mock.scalar_one_or_none.return_value = None
    result_mock.scalars.return_value.all.return_value = []

    db.execute.return_value = result_mock
    db.add = MagicMock()
    db.flush = AsyncMock()

    return db


# ── Main replay runner ─────────────────────────────────────────────────────────

class ReplayRunner:
    """
    Drives a full conversation through the real _handle_message pipeline.

    All DB and LLM I/O is replaced with in-process fakes.
    After run(), inspect .created_orders and .responses for assertions.
    """

    def __init__(
        self,
        conv: dict,
        business: FakeBusiness,
        customer: FakeCustomer,
        menu_items: list[FakeMenuItem],
    ) -> None:
        self.conv = conv
        self.business = business
        self.customer = customer
        self.menu_items = menu_items

        self.session = ReplaySession()
        self.scripted_llm = ScriptedLLMProvider()
        self.created_orders: list[FakeOrder] = []
        self.responses: list[str] = []   # bot response per turn (customer turns only)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Execute all turns. Raises AssertionError on any mismatch."""
        with patch("backend.app.bot.state_machine.flag_modified"):
            for idx, turn in enumerate(self.conv["turns"]):
                await self._process_turn(idx, turn)

        if "final_order" in self.conv:
            self._assert_final_order(self.conv["final_order"])

    # ── Turn processing ───────────────────────────────────────────────────────

    async def _process_turn(self, idx: int, turn: dict) -> None:
        """Process one turn (staff action or customer message)."""

        # Staff-action turns update session context without sending a message
        if turn.get("_type") == "staff_action":
            ctx_updates = turn.get("context_updates", {})
            for k, v in ctx_updates.items():
                state_machine.set_context(self.session, k, v)
            return

        # Apply any pre-message setup (e.g. staff sets delivery fee)
        for k, v in (turn.get("before", {}).get("set_context", {}) or {}).items():
            state_machine.set_context(self.session, k, v)

        msg_text = turn["message"]

        # Pre-load scripted LLM response for this turn
        llm_spec = turn.get("llm")
        self.scripted_llm.enqueue(llm_spec)

        response_text = await self._execute(msg_text)
        self.responses.append(response_text)

        if "expect" in turn:
            self._assert_turn(idx, turn["expect"], response_text)

    # ── Message execution ─────────────────────────────────────────────────────

    async def _execute(self, msg_text: str) -> str:
        """Run msg_text through _handle_message with all I/O mocked."""
        from backend.app.bot.pipeline import _handle_message

        norm_text = normalize(msg_text)
        intent = intent_router.match_intent(norm_text)

        # ── Build fake DB ─────────────────────────────────────────────────────
        fake_db = _make_fake_db(self.created_orders)

        # ── Mock order creator ────────────────────────────────────────────────
        async def _fake_create_order(
            db: Any,
            business: Any,
            customer: Any,
            session: Any,
            initial_status: str = "NEW",
        ) -> FakeOrder:
            ctx = session.context_json or {}
            live_cart = ctx.get("cart", [])
            confirmed_cart = ctx.get("confirmed_cart")
            cart = live_cart or confirmed_cart or []
            subtotal = sum(it.get("line_total_cents", 0) for it in cart)
            fee = int(ctx.get("delivery_fee_cents") or 0)
            order = FakeOrder(
                order_number="BO-001",
                status=initial_status,
                order_mode=ctx.get("order_mode", "PICKUP"),
                subtotal_cents=subtotal,
                delivery_fee_cents=fee,
                total_cents=subtotal + fee,
                currency=business.currency,
                customer_name=ctx.get("customer_name") or getattr(customer, "display_name", None),
                phone_number=ctx.get("phone_number") or getattr(customer, "phone_number", None),
                delivery_address=ctx.get("delivery_address"),
                items=copy.deepcopy(cart),
            )
            # Patch fake_db row's subtotal so finalize response text is accurate
            fake_db.execute.return_value.one_or_none.return_value.subtotal_cents = subtotal
            fake_db.execute.return_value.one_or_none.return_value.order_number = order.order_number
            self.created_orders.append(order)
            return order

        async def _fake_get_last_order(*_args, **_kwargs) -> FakeOrder | None:
            return self.created_orders[-1] if self.created_orders else None

        async def _fake_finalize_delivery(
            db: Any, business: Any, customer: Any, session: Any
        ) -> tuple:
            """
            Replaces _finalize_pending_delivery_order for replay tests.

            The real function does a DB UPDATE RETURNING to apply delivery_fee
            and flip the order to NEW.  We replicate that effect directly on the
            in-memory FakeOrder so final_order assertions can check the correct
            total, while avoiding any live DB calls or recursion.
            """
            from shared.utils.money import format_currency
            ctx = session.context_json or {}
            fee = int(ctx.get("delivery_fee_cents") or 0)
            payment_method = ctx.get("payment_method", "CASH_ON_COLLECTION")

            if self.created_orders:
                last = self.created_orders[-1]
                last.delivery_fee_cents = fee
                last.total_cents = last.subtotal_cents + fee
                last.status = "NEW"
                last.payment_status = payment_method
                order_number = last.order_number
                subtotal = last.subtotal_cents
            else:
                order_number = "BO-001"
                subtotal = 0

            state_machine.clear_cart(session)
            state_machine.transition_state(session, "ORDER_PLACED")

            return (
                f"✅ *Order Confirmed!*\n\nOrder Number: *{order_number}*\n"
                f"Delivery fee: {format_currency(fee, business.currency)}\n"
                f"💰 *Total: {format_currency(subtotal + fee, business.currency)}*\n"
                f"Payment: *{'Cash on delivery' if payment_method == 'CASH_ON_COLLECTION' else 'Card'}*\n\n"
                f"🚗 We'll be on our way once your order is ready!",
                False, None, None, None,
            )

        # ── Patch everything ──────────────────────────────────────────────────
        with patch("backend.app.bot.pipeline._load_menu",
                   return_value=([], self.menu_items)):
            with patch("backend.app.bot.pipeline._load_specials",
                       return_value=[]):
                with patch("backend.app.bot.pipeline._load_conversation_history",
                           return_value=[]):
                    with patch("backend.app.bot.pipeline.usage_tracker.check_daily_limit"):
                        with patch("backend.app.bot.pipeline._get_last_order",
                                   side_effect=_fake_get_last_order):
                            with patch("backend.app.bot.pipeline._cancel_prior_live_orders"):
                                with patch("backend.app.bot.pipeline._cancel_superseded_order"):
                                    with patch("backend.app.bot.pipeline.whatsapp_sender"):
                                        with patch("backend.app.bot.pipeline.order_creator"
                                                   ".create_order_from_cart",
                                                   side_effect=_fake_create_order):
                                            with patch(
                                                "backend.app.bot.pipeline"
                                                "._finalize_pending_delivery_order",
                                                side_effect=_fake_finalize_delivery,
                                            ):
                                              with patch(
                                                "backend.app.llm.provider.get_llm_provider",
                                                return_value=self.scripted_llm,
                                              ):
                                                result = await _handle_message(
                                                    db=fake_db,
                                                    business=self.business,
                                                    customer=self.customer,
                                                    session=self.session,
                                                    msg_text=msg_text,
                                                    norm_text=norm_text,
                                                    intent=intent,
                                                )

        response_text, *_ = result
        return response_text or ""

    # ── Per-turn assertions ───────────────────────────────────────────────────

    def _assert_turn(self, idx: int, expect: dict, response: str) -> None:
        prefix = f"[turn {idx}]"

        if "state" in expect:
            assert self.session.state == expect["state"], (
                f"{prefix} state: expected {expect['state']!r}, "
                f"got {self.session.state!r}"
            )

        if "cart" in expect and expect["cart"] is not None:
            live = state_machine.get_cart(self.session)
            assert_carts_equal(live, expect["cart"], label=f"{prefix} cart")

        if "confirmed_cart" in expect and expect["confirmed_cart"] is not None:
            cc = state_machine.get_context(self.session, "confirmed_cart") or []
            assert_carts_equal(cc, expect["confirmed_cart"],
                               label=f"{prefix} confirmed_cart")

        # confirmed_cart must be ABSENT when expect value is explicitly False
        if expect.get("confirmed_cart") is False:
            cc = state_machine.get_context(self.session, "confirmed_cart")
            assert not cc, (
                f"{prefix} confirmed_cart: expected empty/None, got {cc}"
            )

        resp_lower = response.lower()
        for phrase in expect.get("response_contains", []):
            assert phrase.lower() in resp_lower, (
                f"{prefix} response_contains {phrase!r} not found.\n"
                f"  response: {response[:300]}"
            )
        for phrase in expect.get("response_not_contains", []):
            assert phrase.lower() not in resp_lower, (
                f"{prefix} response_not_contains {phrase!r} WAS found.\n"
                f"  response: {response[:300]}"
            )

    # ── Final order assertion ─────────────────────────────────────────────────

    def _assert_final_order(self, expected: dict) -> None:
        assert self.created_orders, (
            "final_order specified but no order was created during the replay"
        )
        order = self.created_orders[-1]

        if "items" in expected:
            assert_carts_equal(
                order.items, expected["items"], label="final_order.items"
            )

        if "subtotal_cents" in expected:
            assert order.subtotal_cents == expected["subtotal_cents"], (
                f"final_order.subtotal_cents: "
                f"expected {expected['subtotal_cents']}, got {order.subtotal_cents}"
            )

        if "total_cents" in expected:
            assert order.total_cents == expected["total_cents"], (
                f"final_order.total_cents: "
                f"expected {expected['total_cents']}, got {order.total_cents}"
            )

        if "order_mode" in expected:
            assert order.order_mode == expected["order_mode"], (
                f"final_order.order_mode: "
                f"expected {expected['order_mode']!r}, got {order.order_mode!r}"
            )

        if "customer_name" in expected:
            assert (order.customer_name or "").lower() == expected["customer_name"].lower(), (
                f"final_order.customer_name: "
                f"expected {expected['customer_name']!r}, got {order.customer_name!r}"
            )

        if "delivery_fee_cents" in expected:
            assert order.delivery_fee_cents == expected["delivery_fee_cents"], (
                f"final_order.delivery_fee_cents: "
                f"expected {expected['delivery_fee_cents']}, got {order.delivery_fee_cents}"
            )
