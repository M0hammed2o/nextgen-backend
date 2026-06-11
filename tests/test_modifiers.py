"""
Modifier regression test suite.

═══ WHATSAPP CONVERSATION UNDER TEST ═══

  Turn 1:
    Customer: "1 Classic Smash Burger with no tomato and 1 Medium Chicken Mayo Pizza"
    Expected cart: [Burger(qty=1, instructions="no tomato"), Pizza(qty=1, instructions=None)]

  Turn 2:
    Customer: "Add extra cheese to the burger"
    Expected cart: [Burger(qty=1, instructions="no tomato, extra cheese"), Pizza(qty=1)]

  Turn 3:
    Customer: "Actually make it only one burger"
    Expected: is_cart_correction → True (cart correction flow → LLM rebuilds)

Tests cover:
  - is_cart_correction with new real-world phrases
  - _extract_modifier_from_suffix (no tomato, extra cheese, without)
  - _detect_modifier_update (Add extra cheese to the burger)
  - Modifier-aware add_to_cart (different modifiers = separate line items)
  - update_cart_item_instructions (modifier merging)
  - remove_from_cart with partial quantity
  - Full cart state after turns 1 and 2
  - Multi-item orders
"""
import uuid

import pytest

from backend.app.bot import state_machine
from backend.app.bot.intent_router import is_cart_correction
from backend.app.bot.pipeline import (
    _detect_modifier_update,
    _extract_modifier_from_suffix,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, state: str = "BUILDING_CART"):
        self.id = uuid.uuid4()
        self.state = state
        self.context_json: dict = {}


def _uid() -> str:
    return str(uuid.uuid4())


# ════════════════════════════════════════════════════════════════════════════════
# is_cart_correction — new real-world phrases
# ════════════════════════════════════════════════════════════════════════════════

class TestIsCartCorrectionNewPhrases:
    """
    The three phrases from the spec, plus "Actually make it only one burger"
    which is the exact demo message used in Turn 3.
    """

    @pytest.mark.parametrize("text", [
        # Existing patterns (must not regress)
        "I only want one of each",
        "No just one of each",
        "Actually I only want one of each",
        "make it just one of each",
        # New patterns
        "No it must be one burger",
        "No, it must be one burger",
        "Make it only one burger",
        "Make it only 1",
        "Actually make it only one burger",    # exact Turn 3 demo message
        "Actually make it one burger",
    ])
    def test_matches_cart_correction(self, text):
        assert is_cart_correction(text), f"Expected cart correction for: {text!r}"

    @pytest.mark.parametrize("text", [
        # Modifier updates — NOT cart corrections
        "Add extra cheese to the burger",
        "No tomato please",
        # Simple add-ons
        "Add an ice coffee",
        "I want to add wings",
        # Removals of specific cart items
        "Remove the Coke",
        "Delete the pizza",
    ])
    def test_does_not_match_non_corrections(self, text):
        assert not is_cart_correction(text), f"Should NOT be cart correction: {text!r}"


# ════════════════════════════════════════════════════════════════════════════════
# _extract_modifier_from_suffix
# ════════════════════════════════════════════════════════════════════════════════

class TestExtractModifierFromSuffix:

    @pytest.mark.parametrize("suffix, expected", [
        ("with no tomato", "no tomato"),
        ("no tomato", "no tomato"),
        ("without tomato", "no tomato"),
        ("extra cheese", "extra cheese"),
        ("with extra cheese", "extra cheese"),
        ("no onion", "no onion"),
        ("without onion", "no onion"),
    ])
    def test_single_modifier_extraction(self, suffix, expected):
        result = _extract_modifier_from_suffix(suffix)
        assert result == expected, f"suffix={suffix!r}: expected {expected!r}, got {result!r}"

    def test_returns_none_for_empty_string(self):
        assert _extract_modifier_from_suffix("") is None

    def test_returns_none_for_unrelated_text(self):
        assert _extract_modifier_from_suffix("please") is None

    def test_returns_none_for_quantity_prefix_only(self):
        assert _extract_modifier_from_suffix("1") is None

    def test_no_tomato_extracted_from_full_chunk_suffix(self):
        """Simulate: chunk='1 classic smash burger with no tomato', item ends at pos 21."""
        chunk_lower = "1 classic smash burger with no tomato"
        item_name = "classic smash burger"
        end = chunk_lower.find(item_name) + len(item_name)
        suffix = chunk_lower[end:]
        result = _extract_modifier_from_suffix(suffix)
        assert result == "no tomato"


# ════════════════════════════════════════════════════════════════════════════════
# _detect_modifier_update
# ════════════════════════════════════════════════════════════════════════════════

class TestDetectModifierUpdate:

    _burger_cart = [
        {
            "menu_item_id": "burger-id",
            "name": "Classic Smash Burger",
            "price_cents": 8500,
            "quantity": 1,
            "line_total_cents": 8500,
            "options": None,
            "special_instructions": "no tomato",
        }
    ]
    _two_item_cart = [
        {
            "menu_item_id": "burger-id",
            "name": "Classic Smash Burger",
            "price_cents": 8500,
            "quantity": 1,
            "line_total_cents": 8500,
            "options": None,
            "special_instructions": "no tomato",
        },
        {
            "menu_item_id": "pizza-id",
            "name": "Medium Chicken Mayo Pizza",
            "price_cents": 9500,
            "quantity": 1,
            "line_total_cents": 9500,
            "options": None,
            "special_instructions": None,
        },
    ]

    def test_add_extra_cheese_to_burger(self):
        """Turn 2: 'Add extra cheese to the burger' must match."""
        result = _detect_modifier_update(
            "Add extra cheese to the burger", self._two_item_cart
        )
        assert result is not None
        modifier, item = result
        assert modifier == "extra cheese"
        assert "burger" in item["name"].lower()

    def test_no_tomato_to_pizza(self):
        result = _detect_modifier_update(
            "Add no tomato to the pizza", self._two_item_cart
        )
        assert result is not None
        modifier, item = result
        assert modifier == "no tomato"
        assert "pizza" in item["name"].lower()

    def test_returns_none_when_cart_empty(self):
        assert _detect_modifier_update("Add extra cheese to the burger", []) is None

    def test_returns_none_when_no_cart_item_matches(self):
        result = _detect_modifier_update(
            "Add extra sauce to the wings", self._burger_cart
        )
        assert result is None

    def test_returns_none_for_regular_add_message(self):
        """A normal item-add message must not match the modifier pattern."""
        result = _detect_modifier_update(
            "Add a Medium Chicken Mayo Pizza", self._burger_cart
        )
        assert result is None


# ════════════════════════════════════════════════════════════════════════════════
# Modifier-aware add_to_cart
# ════════════════════════════════════════════════════════════════════════════════

class TestModifierAwareAddToCart:

    def test_same_item_different_modifiers_are_separate_entries(self):
        """Burger(no tomato) + Burger(extra cheese) → two separate line items."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=1, special_instructions="no tomato")
        cart = state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=1, special_instructions="extra cheese")
        assert len(cart) == 2

    def test_same_item_same_modifier_accumulates_quantity(self):
        """Burger(no tomato) × 2 → single entry qty=2."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=1, special_instructions="no tomato")
        cart = state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=1, special_instructions="no tomato")
        assert len(cart) == 1
        assert cart[0]["quantity"] == 2
        assert cart[0]["special_instructions"] == "no tomato"

    def test_no_modifier_still_accumulates_quantity(self):
        """Existing behaviour preserved when special_instructions is None."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Burger", 8500)
        cart = state_machine.add_to_cart(s, iid, "Burger", 8500)
        assert len(cart) == 1
        assert cart[0]["quantity"] == 2

    def test_new_item_with_modifier_stored_correctly(self):
        """First-time add with modifier sets special_instructions."""
        s = FakeSession()
        cart = state_machine.add_to_cart(s, _uid(), "Classic Smash Burger", 8500,
                                          quantity=1, special_instructions="no tomato")
        assert len(cart) == 1
        assert cart[0]["special_instructions"] == "no tomato"
        assert cart[0]["quantity"] == 1
        assert cart[0]["line_total_cents"] == 8500


# ════════════════════════════════════════════════════════════════════════════════
# update_cart_item_instructions
# ════════════════════════════════════════════════════════════════════════════════

class TestUpdateCartItemInstructions:

    def test_appends_to_existing_instructions(self):
        """Turn 2 state: burger already has 'no tomato', adding 'extra cheese'."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 8500,
                                   special_instructions="no tomato")
        cart, found = state_machine.update_cart_item_instructions(
            s, "Classic Smash Burger", "extra cheese"
        )
        assert found is True
        assert cart[0]["special_instructions"] == "no tomato, extra cheese"

    def test_sets_instruction_when_none_previously(self):
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 8500)
        cart, found = state_machine.update_cart_item_instructions(
            s, "Classic Smash Burger", "extra cheese"
        )
        assert found is True
        assert cart[0]["special_instructions"] == "extra cheese"

    def test_fuzzy_match_short_name(self):
        """'burger' matches 'Classic Smash Burger'."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 8500)
        cart, found = state_machine.update_cart_item_instructions(s, "burger", "no tomato")
        assert found is True
        assert cart[0]["special_instructions"] == "no tomato"

    def test_returns_false_when_item_not_in_cart(self):
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 8500)
        cart, found = state_machine.update_cart_item_instructions(s, "pizza", "no tomato")
        assert found is False

    def test_does_not_modify_quantity(self):
        """Modifier update must never change the item quantity."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 8500, quantity=2)
        cart, _ = state_machine.update_cart_item_instructions(
            s, "Classic Smash Burger", "extra cheese"
        )
        assert cart[0]["quantity"] == 2
        assert cart[0]["line_total_cents"] == 17000


# ════════════════════════════════════════════════════════════════════════════════
# Partial quantity reduction in remove_from_cart
# ════════════════════════════════════════════════════════════════════════════════

class TestPartialRemoveFromCart:

    def test_partial_removal_reduces_quantity(self):
        """Remove 1 from qty=3 → qty=2, item stays in cart."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=3)
        cart, removed = state_machine.remove_from_cart(s, "Burger", quantity=1)
        assert removed is True
        assert len(cart) == 1
        assert cart[0]["quantity"] == 2
        assert cart[0]["line_total_cents"] == 17000

    def test_partial_removal_exceeding_quantity_removes_item(self):
        """Remove 5 from qty=2 → item removed entirely."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=2)
        cart, removed = state_machine.remove_from_cart(s, "Burger", quantity=5)
        assert removed is True
        assert len(cart) == 0

    def test_partial_removal_exact_quantity_removes_item(self):
        """Remove exactly qty → item removed from cart."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=2)
        cart, removed = state_machine.remove_from_cart(s, "Burger", quantity=2)
        assert removed is True
        assert len(cart) == 0

    def test_full_removal_without_quantity_param_unchanged(self):
        """Original behaviour: no quantity param → entire item removed."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=3)
        cart, removed = state_machine.remove_from_cart(s, "Burger")
        assert removed is True
        assert len(cart) == 0

    def test_partial_removal_updates_line_total(self):
        """line_total_cents must be recalculated after partial reduction."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Burger", 8500, quantity=4)
        cart, _ = state_machine.remove_from_cart(s, "Burger", quantity=2)
        assert cart[0]["quantity"] == 2
        assert cart[0]["line_total_cents"] == 17000  # 8500 * 2


# ════════════════════════════════════════════════════════════════════════════════
# Full WhatsApp conversation regression — cart state after each turn
# ════════════════════════════════════════════════════════════════════════════════

class TestWhatsAppConversationCartStates:
    """
    Regression test for the exact conversation described in the spec.

    This does NOT invoke the LLM or database — it tests state_machine + the
    deterministic pipeline helpers directly.

    Expected cart state after each turn:
      Turn 1: [Burger(qty=1, "no tomato"), Pizza(qty=1, None)]
      Turn 2: [Burger(qty=1, "no tomato, extra cheese"), Pizza(qty=1, None)]
      Turn 3: is_cart_correction("Actually make it only one burger") → True
               (full LLM-driven cart correction; we only assert the gate fires)
    """

    BURGER_ID = "aaaaaaaa-0000-0000-0000-000000000001"
    PIZZA_ID  = "bbbbbbbb-0000-0000-0000-000000000002"

    def test_turn1_multi_item_with_no_tomato_modifier(self):
        """
        Turn 1: '1 Classic Smash Burger with no tomato and 1 Medium Chicken Mayo Pizza'
        Cart must contain both items; burger must have special_instructions='no tomato'.
        """
        s = FakeSession()
        state_machine.add_to_cart(
            s, self.BURGER_ID, "Classic Smash Burger", 8500,
            quantity=1, special_instructions="no tomato",
        )
        state_machine.add_to_cart(
            s, self.PIZZA_ID, "Medium Chicken Mayo Pizza", 9500,
            quantity=1, special_instructions=None,
        )
        cart = state_machine.get_cart(s)

        assert len(cart) == 2
        burger = next(i for i in cart if "burger" in i["name"].lower())
        pizza  = next(i for i in cart if "pizza"  in i["name"].lower())

        assert burger["quantity"] == 1
        assert burger["special_instructions"] == "no tomato"
        assert pizza["quantity"] == 1
        assert pizza["special_instructions"] is None

        assert state_machine.cart_total_cents(s) == 8500 + 9500

    def test_turn2_extra_cheese_merges_with_no_tomato(self):
        """
        Turn 2: 'Add extra cheese to the burger'
        Cart must still have both items; burger instructions must be 'no tomato, extra cheese'.
        """
        s = FakeSession()
        state_machine.add_to_cart(
            s, self.BURGER_ID, "Classic Smash Burger", 8500,
            quantity=1, special_instructions="no tomato",
        )
        state_machine.add_to_cart(
            s, self.PIZZA_ID, "Medium Chicken Mayo Pizza", 9500,
            quantity=1, special_instructions=None,
        )

        # Simulate the modifier-update path in the pipeline
        cart, found = state_machine.update_cart_item_instructions(
            s, "Classic Smash Burger", "extra cheese"
        )

        assert found is True
        assert len(cart) == 2

        burger = next(i for i in cart if "burger" in i["name"].lower())
        pizza  = next(i for i in cart if "pizza"  in i["name"].lower())

        assert burger["special_instructions"] == "no tomato, extra cheese"
        assert pizza["special_instructions"] is None
        assert state_machine.cart_total_cents(s) == 8500 + 9500  # totals unchanged

    def test_turn3_cart_correction_gate_fires(self):
        """
        Turn 3: 'Actually make it only one burger'
        is_cart_correction must return True so the pipeline clears cart + calls LLM.
        """
        assert is_cart_correction("Actually make it only one burger"), \
            "'Actually make it only one burger' must trigger cart correction"

    def test_turn1_summary_shows_modifier(self):
        """cart_summary_text must display special_instructions for the burger."""
        s = FakeSession()
        state_machine.add_to_cart(
            s, self.BURGER_ID, "Classic Smash Burger", 8500,
            quantity=1, special_instructions="no tomato",
        )
        state_machine.add_to_cart(
            s, self.PIZZA_ID, "Medium Chicken Mayo Pizza", 9500,
            quantity=1, special_instructions=None,
        )
        summary = state_machine.cart_summary_text(s)
        assert "Classic Smash Burger" in summary
        assert "Medium Chicken Mayo Pizza" in summary
        assert "no tomato" in summary


# ════════════════════════════════════════════════════════════════════════════════
# Multi-item orders (no modifier)
# ════════════════════════════════════════════════════════════════════════════════

class TestMultiItemOrders:
    """Basic multi-item cart behaviour that must not regress."""

    def test_three_distinct_items(self):
        s = FakeSession()
        state_machine.add_to_cart(s, _uid(), "Classic Smash Burger", 8500)
        state_machine.add_to_cart(s, _uid(), "Medium Chicken Mayo Pizza", 9500)
        state_machine.add_to_cart(s, _uid(), "Coke 330ml", 2000)
        cart = state_machine.get_cart(s)
        assert len(cart) == 3
        assert state_machine.cart_total_cents(s) == 20000

    def test_quantity_label_multi_item(self):
        """Two burgers + one pizza: summary shows correct quantities."""
        s = FakeSession()
        iid = _uid()
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 8500, quantity=2)
        state_machine.add_to_cart(s, _uid(), "Medium Chicken Mayo Pizza", 9500)
        summary = state_machine.cart_summary_text(s)
        assert "2x Classic Smash Burger" in summary
        assert "1x Medium Chicken Mayo Pizza" in summary
