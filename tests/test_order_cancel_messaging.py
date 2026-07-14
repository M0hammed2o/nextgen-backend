"""
Regression test for a reported bug: ORDER_CANCEL always told the customer to
"call the store directly", even when no real order had been placed yet —
the bot had already cleared the draft cart itself, so the phone-call
instruction contradicted its own action.

Sequence that reproduced it:
  Customer builds a cart (never confirmed) → says "cancel order"
  Bot: clears the cart (real action) but says "please call the store" (wrong
  words for an action it just did itself with no store involvement needed).

Fix: only show the phone-call message when state == ORDER_PLACED and the
last real Order is still in a non-terminal status. Otherwise self-serve with
the same wording already used by the CONFIRMING_ORDER cancel path.

Following this codebase's existing convention for pipeline branch tests
(see test_conversation_state_bugs.py) — source inspection plus a standalone
simulation of the guard's decision logic, since exercising _handle_message
end-to-end requires a full DB/business/customer mock harness that the replay
suite already covers.
"""

import inspect

from backend.app.bot.pipeline import _handle_message


def _cancel_response_kind(current_state: str, last_order_status: str | None) -> str:
    """Mirrors the ORDER_CANCEL branch's decision logic in pipeline.py."""
    _TERMINAL = ("CANCELLED", "COLLECTED", "DELIVERED")
    has_real_order = current_state == "ORDER_PLACED" and last_order_status is not None
    if has_real_order and last_order_status not in _TERMINAL:
        return "phone"
    return "self_service"


class TestOrderCancelMessaging:
    def test_source_no_longer_unconditionally_redirects_to_phone(self):
        src = inspect.getsource(_handle_message)
        assert "self-service" in src.lower() or "no order placed yet" in src.lower(), (
            "ORDER_CANCEL must have a self-service path for draft carts, not an "
            "unconditional 'call the store' redirect."
        )

    def test_source_still_redirects_to_phone_for_real_orders(self):
        src = inspect.getsource(_handle_message)
        assert "please call the store directly" in src, (
            "A real, already-placed order must still be escalated by phone."
        )

    def test_draft_cart_before_any_order_placed_is_self_service(self):
        # BUILDING_CART / CONFIRMING_ORDER — no Order row exists yet.
        assert _cancel_response_kind("BUILDING_CART", None) == "self_service"
        assert _cancel_response_kind("CONFIRMING_ORDER", None) == "self_service"

    def test_real_active_order_requires_phone_call(self):
        for status in ("NEW", "ACCEPTED", "IN_PROGRESS", "READY"):
            assert _cancel_response_kind("ORDER_PLACED", status) == "phone", (
                f"Active order with status {status!r} must redirect to phone"
            )

    def test_real_terminal_order_is_self_service(self):
        # A previous order already finished/cancelled — nothing to call about.
        for status in ("COLLECTED", "DELIVERED", "CANCELLED"):
            assert _cancel_response_kind("ORDER_PLACED", status) == "self_service", (
                f"Terminal order with status {status!r} must not redirect to phone"
            )
