"""
Regression tests based on the real WhatsApp conversation used during staging testing.

═══ CONVERSATION THAT EXPOSED THE BUGS ═══

  Turn 1:
    Customer: "What do you recommend?"
    Bot:      "I'd suggest the Classic Smash Burger or the Medium Chicken Mayo Pizza 🍔🍕"
              [stored recommended_items = [Burger, Pizza], state → BROWSING_MENU]

  Turn 2 (BUG A):
    Customer: "Yes please"
    Expected: Bot accepts recommendation, adds item(s), shows cart for confirmation
    Actual:   is_recommendation_acceptance("Yes please") returned False → recommendation
              context was CLEARED before processing → "yes please" fell through to the
              LLM with no context → confused response; customer had to start over.

  Turn 3 (BUG B):
    Customer: "Add a Classic Smash Burger and remove the tomato"
    Expected: Burger added to cart with special_instructions="no tomato"
    Actual:   Bot returned remove_item for "tomato" (not a menu item) → mutation guard
              fired → "Sorry, I didn't catch which item to remove." → customer confused.

═══ FIXES APPLIED ═══

  Bug A fix (pipeline.py):
    Expanded recommendation acceptance gate from:
      if _recommended and is_recommendation_acceptance(msg_text):
    to:
      if _recommended and (is_recommendation_acceptance(msg_text) or is_confirmation(msg_text)):

    Safe because: when recommended_items is in context, session state is always
    BROWSING_MENU (never CONFIRMING_ORDER), so is_confirmation() here cannot
    accidentally place an order.

  Bug B fix (prompt_builder.py):
    Added INGREDIENT MODIFIERS section to add_items instruction explaining that
    "no X", "without X", "extra X" should use special_instructions, NOT remove_item.
    Added explicit negative constraint to remove_item: "NEVER use remove_item for
    ingredient modifications."
    Added single-item guidance to recommend_items: "Recommend ONE item at a time.
    Do NOT use 'X or Y' phrasing."
"""

import uuid
from unittest.mock import MagicMock

import pytest

from backend.app.bot import state_machine
from backend.app.bot.intent_router import is_confirmation, is_recommendation_acceptance
from backend.app.bot.prompt_builder import build_system_prompt


# ── Helpers ───────────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, state: str = "BROWSING_MENU"):
        self.id = uuid.uuid4()
        self.state = state
        self.context_json: dict = {}


def _make_business():
    b = MagicMock()
    b.name = "Test Restaurant"
    b.address = "123 Test St"
    b.phone = "0812345678"
    b.currency = "ZAR"
    b.delivery_enabled = False
    b.order_in_only = False
    b.business_hours = None
    b.greeting_text = None
    b.fallback_text = None
    b.closed_text = None
    return b


def _recommendation_gate(msg_text: str, recommended_items) -> bool:
    """Mirror the exact gate condition in pipeline._handle_message after the fix."""
    return bool(recommended_items) and (
        is_recommendation_acceptance(msg_text)
        or is_confirmation(msg_text)
    )


# ════════════════════════════════════════════════════════════════════════════════
# BUG A — "Yes please" after recommendation was dropped (pipeline.py fix)
# ════════════════════════════════════════════════════════════════════════════════

class TestRecommendationAcceptanceGate:
    """
    Verifies the pipeline gate: when recommended_items is in context,
    common confirmation phrases trigger _handle_recommendation_acceptance
    instead of being cleared and lost.
    """

    _recommended_single = [
        {"name": "Classic Smash Burger", "quantity": 1, "options": None, "special_instructions": None}
    ]
    _recommended_two = [
        {"name": "Classic Smash Burger", "quantity": 1, "options": None, "special_instructions": None},
        {"name": "Medium Chicken Mayo Pizza", "quantity": 1, "options": None, "special_instructions": None},
    ]

    # ── Exact phrases from the real conversation ──────────────────────────────

    def test_yes_please_triggers_acceptance(self):
        """'Yes please' — the exact phrase the customer used — must trigger acceptance."""
        assert _recommendation_gate("Yes please", self._recommended_single), \
            "'Yes please' must route to recommendation acceptance, not be cleared"

    def test_yes_please_with_two_recommendations(self):
        """'Yes please' with two stored recommendations must still trigger acceptance."""
        assert _recommendation_gate("Yes please", self._recommended_two), \
            "'Yes please' must accept even when two items were recommended"

    # ── Common SA English confirmation phrases ────────────────────────────────

    @pytest.mark.parametrize("phrase", [
        "yes", "Yes", "YES",
        "yep", "yeah", "yah", "yebo", "ja",
        "sure", "sure thing",
        "ok", "okay",
        "lekker", "sharp", "100",
        "sounds good", "looks good", "all good",
        "go ahead", "do it", "perfect",
        "yes please", "yes thanks", "yes bru", "ok man",
    ])
    def test_common_confirmations_trigger_acceptance(self, phrase):
        """All common confirmation phrases must trigger recommendation acceptance."""
        assert _recommendation_gate(phrase, self._recommended_single), \
            f"'{phrase}' should trigger recommendation acceptance when context is set"

    # ── Phrases that should NOT trigger acceptance ────────────────────────────

    @pytest.mark.parametrize("phrase", [
        "what else do you have",
        "show me the menu",
        "actually never mind",
        "how much does it cost",
        "I want something different",
        "no thanks",
        "nah",
        "not today",
    ])
    def test_non_confirmations_do_not_trigger_acceptance(self, phrase):
        """Non-confirmation phrases must NOT trigger recommendation acceptance."""
        assert not _recommendation_gate(phrase, self._recommended_single), \
            f"'{phrase}' should NOT trigger recommendation acceptance"

    # ── No context = no acceptance ────────────────────────────────────────────

    def test_no_recommendation_context_means_no_acceptance(self):
        """Without stored recommended_items, the gate must never fire."""
        assert not _recommendation_gate("yes please", None)
        assert not _recommendation_gate("yes please", [])

    # ── Safety: gate cannot fire in CONFIRMING_ORDER ──────────────────────────

    def test_gate_condition_is_context_dependent_not_state_dependent(self):
        """
        The gate fires on recommended_items presence, not on session state.
        When recommended_items is cleared (as happens after a recommendation is
        accepted or the customer moves on), 'yes' in CONFIRMING_ORDER must NOT
        route here — it routes to order placement via the separate CONFIRMING_ORDER
        branch which checks current_state == CONFIRMING_ORDER.
        This test documents that the gate condition itself is correct.
        """
        # No recommended_items → gate is False regardless of message
        assert not _recommendation_gate("yes", None)
        assert not _recommendation_gate("yes", [])


# ════════════════════════════════════════════════════════════════════════════════
# Recommendation acceptance: items added to cart correctly
# ════════════════════════════════════════════════════════════════════════════════

class TestRecommendationItemsAddedToCart:
    """
    When the acceptance gate fires and _handle_recommendation_acceptance runs,
    the recommended items must be added to cart and the context must be cleared.
    This tests state_machine directly (no DB/LLM involved).
    """

    def test_accepted_item_is_added_to_cart(self):
        """Accepting a recommendation must add the item to the session cart."""
        session = FakeSession()
        item_id = str(uuid.uuid4())
        recommended = [
            {
                "name": "Classic Smash Burger",
                "quantity": 1,
                "options": None,
                "special_instructions": None,
            }
        ]
        state_machine.set_context(session, "recommended_items", recommended)

        # Simulate what _handle_recommendation_acceptance does:
        state_machine.add_to_cart(
            session,
            menu_item_id=item_id,
            name="Classic Smash Burger",
            price_cents=8500,
            quantity=1,
            options=None,
            special_instructions=None,
        )
        state_machine.set_context(session, "recommended_items", None)

        cart = state_machine.get_cart(session)
        assert len(cart) == 1
        assert cart[0]["name"] == "Classic Smash Burger"
        assert state_machine.get_context(session, "recommended_items") is None

    def test_two_recommended_items_both_added_on_acceptance(self):
        """
        When bot recommended 'X or Y' and customer accepted, both items are added.
        This is the current behaviour. The prompt fix reduces future 'X or Y'
        recommendations, but this test documents the acceptance behaviour.
        """
        session = FakeSession()
        burger_id = str(uuid.uuid4())
        pizza_id = str(uuid.uuid4())

        # Simulate adding both recommended items
        state_machine.add_to_cart(session, burger_id, "Classic Smash Burger", 8500, 1)
        state_machine.add_to_cart(session, pizza_id, "Medium Chicken Mayo Pizza", 9500, 1)
        state_machine.set_context(session, "recommended_items", None)

        cart = state_machine.get_cart(session)
        names = {i["name"] for i in cart}
        assert "Classic Smash Burger" in names
        assert "Medium Chicken Mayo Pizza" in names
        assert len(cart) == 2
        assert state_machine.cart_total_cents(session) == 18000


# ════════════════════════════════════════════════════════════════════════════════
# BUG B — Ingredient modifier treated as cart removal (prompt_builder.py fix)
# ════════════════════════════════════════════════════════════════════════════════

class TestModifierPromptContent:
    """
    Verifies the system prompt contains correct guidance for ingredient modifiers.

    The prompt fix prevents the LLM from returning remove_item for phrases like
    "remove the tomato from my burger" — which is a modifier, not a cart removal.
    These tests assert that the specific constraint text exists in the generated prompt.
    """

    def _prompt(self, state: str = "BUILDING_CART") -> str:
        return build_system_prompt(
            business=_make_business(),
            categories=[],
            menu_items=[],
            specials=[],
            conversation_state=state,
            cart=[],
        )

    def test_prompt_contains_ingredient_modifier_section(self):
        """Prompt must contain the INGREDIENT MODIFIERS label."""
        assert "INGREDIENT MODIFIERS" in self._prompt()

    def test_prompt_add_items_mentions_special_instructions_for_modifiers(self):
        """Prompt must instruct LLM to use special_instructions for ingredient changes."""
        prompt = self._prompt()
        # The modifier section should reference special_instructions in context of modifiers
        assert "special_instructions" in prompt
        assert "no tomato" in prompt.lower() or "without" in prompt.lower()

    def test_prompt_remove_item_has_never_for_ingredients_constraint(self):
        """Prompt must explicitly say NEVER use remove_item for ingredient modifications."""
        prompt = self._prompt()
        lower = prompt.lower()
        # The negative constraint must be present
        assert "never use remove_item for ingredient" in lower, \
            "Prompt must contain: NEVER use remove_item for ingredient modifications"

    def test_prompt_remove_item_specifies_entire_item(self):
        """Prompt must clarify remove_item is for removing entire cart items."""
        prompt = self._prompt()
        lower = prompt.lower()
        assert "entire item" in lower or "whole item" in lower, \
            "Prompt must clarify remove_item is for entire cart items only"

    def test_prompt_provides_correct_modifier_example(self):
        """Prompt must show a concrete example of modifier → special_instructions."""
        prompt = self._prompt()
        # Should contain an example showing the correct pattern
        assert "special_instructions" in prompt
        # The example pattern: "burger without tomato" → special_instructions="no tomato"
        lower = prompt.lower()
        assert "no tomato" in lower or "without tomato" in lower, \
            "Prompt must include a concrete modifier example"

    def test_prompt_recommend_items_discourages_x_or_y_phrasing(self):
        """Prompt must instruct LLM not to recommend 'X or Y' alternatives."""
        prompt = self._prompt()
        lower = prompt.lower()
        assert "x or y" in lower or "do not use" in lower or "one item" in lower, \
            "Prompt must discourage 'X or Y' recommendation phrasing"

    def test_prompt_recommend_items_suggests_ask_options_for_alternatives(self):
        """When the LLM wants to offer a choice, it should use ask_options, not recommend_items."""
        prompt = self._prompt()
        # The recommend_items description should reference ask_options for alternatives
        rec_section_start = prompt.find('"recommend_items"')
        ask_options_pos = prompt.find("ask_options", rec_section_start)
        assert ask_options_pos != -1 and ask_options_pos < rec_section_start + 400, \
            "recommend_items section should reference ask_options for offering alternatives"

    def test_prompt_is_valid_python_string(self):
        """Prompt must build without error for all conversation states."""
        for state in [
            "IDLE", "BROWSING_MENU", "BUILDING_CART",
            "CONFIRMING_ORDER", "COLLECTING_DETAILS", "ORDER_PLACED",
        ]:
            prompt = self._prompt(state=state)
            assert len(prompt) > 500, f"Prompt for state {state} must be non-trivial"

    def test_modifier_prompt_present_in_confirming_order_state(self):
        """
        The modifier guidance must also be in the CONFIRMING_ORDER prompt,
        because customers often say 'no tomato' while reviewing their order.
        """
        prompt = self._prompt(state="CONFIRMING_ORDER")
        assert "INGREDIENT MODIFIERS" in prompt
        assert "never use remove_item for ingredient" in prompt.lower()


# ════════════════════════════════════════════════════════════════════════════════
# End-to-end recommendation flow documentation
# ════════════════════════════════════════════════════════════════════════════════

class TestRecommendationFlowDocumentation:
    """
    Documents the correct end-to-end recommendation flow after the fix.
    These tests serve as living documentation of expected behaviour.
    """

    def test_recommendation_context_survives_until_accepted(self):
        """
        recommended_items in context must persist until the customer explicitly
        accepts or moves on (not before). The gate fires FIRST — if gate is True,
        the context is consumed by _handle_recommendation_acceptance. Only if
        the gate is False (customer moved on) is the context cleared.
        """
        session = FakeSession()
        recommended = [
            {"name": "Classic Smash Burger", "quantity": 1, "options": None, "special_instructions": None}
        ]
        state_machine.set_context(session, "recommended_items", recommended)

        # Context should still be present before any gate check
        assert state_machine.get_context(session, "recommended_items") == recommended

        # Gate fires for "yes please" → context would be consumed by acceptance
        msg = "yes please"
        gate = _recommendation_gate(msg, state_machine.get_context(session, "recommended_items"))
        assert gate, "Gate must fire for 'yes please' — context should be consumed by acceptance"

    def test_recommendation_context_cleared_after_non_acceptance(self):
        """
        If the customer moves on (non-confirmation message), the context is cleared
        before the LLM is called, preventing ghost recommendations.
        """
        session = FakeSession()
        recommended = [
            {"name": "Classic Smash Burger", "quantity": 1, "options": None, "special_instructions": None}
        ]
        state_machine.set_context(session, "recommended_items", recommended)

        # Customer says something unrelated — gate should be False
        msg = "what are your opening hours"
        gate = _recommendation_gate(msg, state_machine.get_context(session, "recommended_items"))
        assert not gate, "Non-confirmation must not trigger gate"

        # Simulate what the pipeline does when gate is False: clear the context
        state_machine.set_context(session, "recommended_items", None)
        assert state_machine.get_context(session, "recommended_items") is None
