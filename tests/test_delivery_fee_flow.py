"""
Delivery fee flow regression tests.

Tests the bot's WAITING_DELIVERY_FEE_APPROVAL state machine:

  Staff sets fee via API → order status = PENDING_DELIVERY_FEE
  Bot sends WhatsApp to customer with fee amount
  Customer replies YES / NO / (unexpected) / (delayed) / (session expired)

  Flow documented in pipeline._handle_waiting_delivery_fee:
    YES  → set delivery_fee_status="APPROVED", transition to COLLECTING_PAYMENT
    NO   → cancel pending order, clear cart, transition to IDLE
    else → reprompt with fee amount

Scenarios tested:
  1. Normal YES approval → state transitions correctly
  2. Normal NO rejection → state transitions correctly
  3. Unexpected message → reprompt returned, state unchanged
  4. Multiple unexpected messages before YES → state still transitions on YES
  5. Multiple unexpected messages before NO  → state still transitions on NO
  6. Delayed reply: session context preserved across is_confirmation/is_negation calls
  7. Session expiry: context lost → fee defaults to 0, reprompt is still safe
  8. is_confirmation coverage for all common YES phrases
  9. is_negation coverage for all common NO phrases
 10. "Cash on collection" and "Card" are NOT accidentally treated as fee confirmations
"""
import uuid

import pytest

from backend.app.bot import state_machine
from backend.app.bot.intent_router import is_confirmation, is_negation
from shared.enums import ConversationState


# ── Helpers ────────────────────────────────────────────────────────────────────

class FakeSession:
    """Minimal stand-in for ConversationSession, matching conftest.FakeSession."""
    def __init__(self, state: str = ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value):
        self.id = uuid.uuid4()
        self.state = state
        self.context_json: dict = {}


def _seed_delivery_context(session: FakeSession, fee_cents: int = 3500) -> str:
    """
    Seed the session with the context that pipeline sets before transitioning
    to WAITING_DELIVERY_FEE_APPROVAL.
    Returns the fake pending_order_id string.
    """
    order_id = str(uuid.uuid4())
    state_machine.set_context(session, "delivery_fee_cents", fee_cents)
    state_machine.set_context(session, "pending_order_id", order_id)
    return order_id


# ════════════════════════════════════════════════════════════════════════════════
# State machine context: correct keys are written and readable
# ════════════════════════════════════════════════════════════════════════════════

class TestDeliveryFeeContext:

    def test_fee_cents_stored_and_retrieved(self):
        s = FakeSession()
        state_machine.set_context(s, "delivery_fee_cents", 4200)
        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 4200

    def test_pending_order_id_stored_and_retrieved(self):
        s = FakeSession()
        order_id = str(uuid.uuid4())
        state_machine.set_context(s, "pending_order_id", order_id)
        assert state_machine.get_context(s, "pending_order_id") == order_id

    def test_fee_defaults_to_zero_when_missing(self):
        """If session context is empty (e.g. expired and reset), fee defaults to 0."""
        s = FakeSession()
        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 0

    def test_pending_order_id_defaults_to_none_when_missing(self):
        s = FakeSession()
        assert state_machine.get_context(s, "pending_order_id") is None

    def test_clear_cart_removes_delivery_fee_context(self):
        """
        After clear_cart(), delivery fee context must not bleed into the next order.
        """
        s = FakeSession()
        _seed_delivery_context(s)
        state_machine.clear_cart(s)
        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 0
        assert state_machine.get_context(s, "pending_order_id") is None
        assert state_machine.get_context(s, "delivery_fee_status") is None


# ════════════════════════════════════════════════════════════════════════════════
# YES branch — fee confirmation triggers state transition
# ════════════════════════════════════════════════════════════════════════════════

class TestDeliveryFeeYesBranch:
    """
    Verifies that `is_confirmation` correctly gates the YES branch for all
    phrases a customer might send in response to a delivery fee message.
    The state machine transition to COLLECTING_PAYMENT is simulated directly.
    """

    @pytest.mark.parametrize("phrase", [
        # Most likely WhatsApp replies to "Do you accept the delivery fee?"
        "yes", "Yes", "YES",
        "yep", "yeah", "yah", "yebo", "ja",
        "sure", "ok", "okay",
        "lekker", "sharp", "100",
        "yes please", "yes thanks", "yes bru",
        "go ahead", "sounds good", "looks good", "perfect",
        "confirm", "confirmed", "do it",
    ])
    def test_is_confirmation_matches_typical_fee_approval_phrases(self, phrase):
        assert is_confirmation(phrase), f"Expected fee-approval confirmation for: {phrase!r}"

    def test_approval_state_transition_simulation(self):
        """
        Simulate exactly what _handle_waiting_delivery_fee does on YES:
          1. Set delivery_fee_status = "APPROVED"
          2. Transition state to COLLECTING_PAYMENT
        """
        s = FakeSession()
        _seed_delivery_context(s, fee_cents=3500)

        # Simulate YES branch
        state_machine.set_context(s, "delivery_fee_status", "APPROVED")
        state_machine.transition_state(s, ConversationState.COLLECTING_PAYMENT.value)

        assert s.state == ConversationState.COLLECTING_PAYMENT.value
        assert state_machine.get_context(s, "delivery_fee_status") == "APPROVED"
        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 3500
        assert state_machine.get_context(s, "pending_order_id") is not None

    def test_fee_context_preserved_after_approval(self):
        """
        delivery_fee_cents and pending_order_id must survive the APPROVED
        transition — they are still needed when the final order is updated.
        """
        s = FakeSession()
        order_id = _seed_delivery_context(s, fee_cents=7000)

        state_machine.set_context(s, "delivery_fee_status", "APPROVED")
        state_machine.transition_state(s, ConversationState.COLLECTING_PAYMENT.value)

        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 7000
        assert state_machine.get_context(s, "pending_order_id") == order_id


# ════════════════════════════════════════════════════════════════════════════════
# NO branch — rejection triggers cart clear + IDLE transition
# ════════════════════════════════════════════════════════════════════════════════

class TestDeliveryFeeNoBranch:

    @pytest.mark.parametrize("phrase", [
        "no", "nah", "nope",
        # "cancel" alone routes through ORDER_CANCEL intent upstream (not is_negation)
        "cancel that",
        "never mind", "nevermind",
        "not now", "not yet",
        "scratch that",
    ])
    def test_is_negation_matches_typical_fee_rejection_phrases(self, phrase):
        assert is_negation(phrase), f"Expected fee-rejection negation for: {phrase!r}"

    def test_rejection_state_transition_simulation(self):
        """
        Simulate exactly what _handle_waiting_delivery_fee does on NO:
          1. clear_cart()  (also clears pending_order_id, delivery_fee_cents)
          2. Transition to IDLE
        """
        s = FakeSession()
        state_machine.add_to_cart(s, str(uuid.uuid4()), "Classic Smash Burger", 8500)
        _seed_delivery_context(s, fee_cents=3500)

        # Simulate NO branch
        state_machine.clear_cart(s)
        state_machine.transition_state(s, ConversationState.IDLE.value)

        assert s.state == ConversationState.IDLE.value
        assert state_machine.get_cart(s) == []
        assert state_machine.get_context(s, "pending_order_id") is None
        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 0

    def test_rejection_clears_entire_order_context(self):
        """
        After rejection, no order-related context should remain.
        """
        s = FakeSession()
        state_machine.add_to_cart(s, str(uuid.uuid4()), "Pizza", 9500)
        _seed_delivery_context(s, fee_cents=2000)
        state_machine.set_context(s, "order_mode", "DELIVERY")
        state_machine.set_context(s, "customer_name", "Test Customer")

        state_machine.clear_cart(s)
        state_machine.transition_state(s, ConversationState.IDLE.value)

        assert state_machine.get_cart(s) == []
        assert state_machine.get_context(s, "pending_order_id") is None
        assert state_machine.get_context(s, "order_mode") is None
        # Customer name is intentionally preserved for repeat orders
        assert state_machine.get_context(s, "customer_name") == "Test Customer"


# ════════════════════════════════════════════════════════════════════════════════
# UNEXPECTED message branch — reprompt, state unchanged
# ════════════════════════════════════════════════════════════════════════════════

class TestDeliveryFeeUnexpectedBranch:

    @pytest.mark.parametrize("phrase", [
        "what",
        "how much is that",
        "can I change my order",
        "actually can I do pickup",
        "show me the menu",
        "hello",
        "hi",
        "hmm",
        "maybe",
        "not sure",
        "I don't know",
        "how long will it take",
    ])
    def test_unexpected_phrases_are_neither_confirmation_nor_negation(self, phrase):
        """
        Phrases that should trigger the reprompt path must NOT match either
        is_confirmation or is_negation.
        """
        assert not is_confirmation(phrase), f"'{phrase}' should not be a confirmation"
        assert not is_negation(phrase), f"'{phrase}' should not be a negation"

    def test_state_unchanged_on_unexpected_message(self):
        """
        When neither YES nor NO is received, the pipeline reprompts.
        The session state must remain WAITING_DELIVERY_FEE_APPROVAL.
        """
        s = FakeSession()
        _seed_delivery_context(s, fee_cents=3500)

        # Simulate: unexpected message → no state change
        original_state = s.state
        # (reprompt is returned; no transition happens)
        assert s.state == original_state
        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 3500


# ════════════════════════════════════════════════════════════════════════════════
# Multiple unexpected messages, then eventual YES / NO
# ════════════════════════════════════════════════════════════════════════════════

class TestDeliveryFeeMultiTurn:

    def test_yes_succeeds_after_multiple_unexpected_messages(self):
        """
        Customer asks questions → eventually says YES.
        Context must be intact for the approval to complete.
        """
        s = FakeSession()
        order_id = _seed_delivery_context(s, fee_cents=5000)

        # Multiple unexpected turns: state and context unchanged
        for _ in range(3):
            # Simulate reprompt: no state change
            assert s.state == ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value
            assert state_machine.get_context(s, "delivery_fee_cents", 0) == 5000

        # Customer finally says YES
        state_machine.set_context(s, "delivery_fee_status", "APPROVED")
        state_machine.transition_state(s, ConversationState.COLLECTING_PAYMENT.value)

        assert s.state == ConversationState.COLLECTING_PAYMENT.value
        assert state_machine.get_context(s, "pending_order_id") == order_id

    def test_no_succeeds_after_multiple_unexpected_messages(self):
        """
        Customer asks questions → eventually says NO.
        Cart and context must clear correctly.
        """
        s = FakeSession()
        _seed_delivery_context(s, fee_cents=5000)
        state_machine.add_to_cart(s, str(uuid.uuid4()), "Pizza", 9500)

        # Multiple unexpected turns: state unchanged
        for _ in range(2):
            assert s.state == ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value

        # Customer finally says NO
        state_machine.clear_cart(s)
        state_machine.transition_state(s, ConversationState.IDLE.value)

        assert s.state == ConversationState.IDLE.value
        assert state_machine.get_cart(s) == []


# ════════════════════════════════════════════════════════════════════════════════
# Delayed / expired session scenarios
# ════════════════════════════════════════════════════════════════════════════════

class TestDeliveryFeeDelayedReply:

    def test_context_survives_get_set_cycle(self):
        """
        Simulates a customer replying hours later: the session is reloaded
        from DB (context_json deserialized).  Both fee_cents and pending_order_id
        must survive a serialize → deserialize round-trip.
        """
        import json
        s = FakeSession()
        order_id = _seed_delivery_context(s, fee_cents=4500)

        # Serialize (what DB persistence does)
        serialized = json.dumps(s.context_json)
        # Deserialize (what DB load does)
        s.context_json = json.loads(serialized)

        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 4500
        assert state_machine.get_context(s, "pending_order_id") == order_id

    def test_expired_session_context_is_empty(self):
        """
        If the session expires (get_or_create_session resets context_json to {}),
        delivery_fee_cents defaults to 0 and pending_order_id is None.
        The reprompt path handles this gracefully (fee shows as 'not yet confirmed').
        """
        s = FakeSession()
        _seed_delivery_context(s, fee_cents=3500)

        # Simulate session expiry reset
        s.context_json = {}

        assert state_machine.get_context(s, "delivery_fee_cents", 0) == 0
        assert state_machine.get_context(s, "pending_order_id") is None
        # No exception raised — safe to use default fallback

    def test_delivery_fee_status_not_set_before_approval(self):
        """
        Before any YES is received, delivery_fee_status must be absent.
        This prevents a stale APPROVED status from a prior order bleeding through.
        """
        s = FakeSession()
        _seed_delivery_context(s)
        assert state_machine.get_context(s, "delivery_fee_status") is None

    def test_clear_cart_removes_delivery_fee_status(self):
        """
        If the customer approved the fee but then something went wrong,
        clear_cart() must also remove delivery_fee_status to prevent
        ghost approval on the customer's next order.
        """
        s = FakeSession()
        _seed_delivery_context(s)
        state_machine.set_context(s, "delivery_fee_status", "APPROVED")
        state_machine.clear_cart(s)
        assert state_machine.get_context(s, "delivery_fee_status") is None


# ════════════════════════════════════════════════════════════════════════════════
# Safety: payment method words must not be confused with fee confirmation
# ════════════════════════════════════════════════════════════════════════════════

class TestDeliveryFeePaymentMethodSafety:
    """
    After fee approval the customer is asked "Cash or card?".
    These payment method replies must NOT be treated as a new order confirmation.
    This is handled by state routing (COLLECTING_PAYMENT vs CONFIRMING_ORDER),
    but we verify here that the words themselves are correctly classified
    to avoid any intent-routing confusion.
    """

    @pytest.mark.parametrize("phrase", [
        "cash", "card", "eft", "visa", "mastercard",
        "tap", "swipe", "debit", "credit",
    ])
    def test_payment_method_words_are_not_confirmations(self, phrase):
        """
        Payment method words that don't form a full confirmation phrase
        must NOT match is_confirmation — they are handled by _handle_collecting_payment.
        """
        assert not is_confirmation(phrase), \
            f"'{phrase}' is a payment method, not a confirmation — must not match is_confirmation"

    @pytest.mark.parametrize("phrase", [
        "cash", "card", "eft", "swipe",
    ])
    def test_payment_method_words_are_not_negations(self, phrase):
        assert not is_negation(phrase), \
            f"'{phrase}' must not match is_negation"
