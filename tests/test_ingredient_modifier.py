"""
Regression tests for the three live beta failures — ingredient-level modifier routing.

Reported failures:
  1. "Classic Smash Burger take out tomato"
     → Expected: add burger with modifier 'no tomato'
     → Actual:   "Sorry, I didn't catch which item to remove."

  2. "take out tomato"
     → Expected: update existing burger's instructions with 'no tomato'
     → Actual:   "Sorry, I didn't catch which item to remove."

  3. "take out tomato from the Classic Smash Burger"
     → Expected: update burger with 'no tomato'
     → Actual:   "Sorry, I didn't catch which item to remove."

Root cause: all three matched ORDER_REMOVE intent (via the "take out" pattern).
needs_llm(ORDER_REMOVE)=True → LLM received them → tried remove_from_cart("tomato")
→ "tomato" not a cart item → error message.

Fixes applied:
  - _extract_modifier_from_suffix: "take out/off/away X" and "leave out X" → "no X"
  - _detect_ingredient_modifier_from_remove: intercepts ingredient-level ORDER_REMOVE
  - DET_REMOVE_INGREDIENT block in _handle_with_llm: fires before LLM for ORDER_REMOVE
"""
import uuid
from unittest.mock import patch

import pytest

from backend.app.bot import state_machine
from backend.app.bot.intent_router import match_intent
from backend.app.bot.normalizer import normalize
from backend.app.bot.pipeline import (
    _INGREDIENT_WORDS,
    _detect_ingredient_modifier_from_remove,
    _extract_items_from_message,
    _extract_modifier_from_suffix,
)
from shared.enums import MessageIntent


# ── Helpers ────────────────────────────────────────────────────────────────────

class FakeItem:
    def __init__(self, name: str, price: int = 7500):
        self.id = uuid.uuid4()
        self.name = name
        self.price_cents = price
        self.is_active = True
        self.is_deleted = False


_MENU = [
    FakeItem("Classic Smash Burger", 7500),
    FakeItem("Double Smash Burger", 9500),
    FakeItem("Spicy Chicken Burger", 8000),
    FakeItem("Medium Chicken Mayo Pizza", 8500),
    FakeItem("Coca-Cola (330ml)", 2000),
]

_MENU_ITEM_NAMES = {i.name.lower() for i in _MENU}

_BURGER_CART = [
    {
        "menu_item_id": "burger-1",
        "name": "Classic Smash Burger",
        "price_cents": 7500,
        "quantity": 1,
        "line_total_cents": 7500,
        "options": None,
        "special_instructions": None,
    }
]

_TWO_ITEM_CART = [
    {
        "menu_item_id": "burger-1",
        "name": "Classic Smash Burger",
        "price_cents": 7500,
        "quantity": 1,
        "line_total_cents": 7500,
        "options": None,
        "special_instructions": None,
    },
    {
        "menu_item_id": "pizza-1",
        "name": "Medium Chicken Mayo Pizza",
        "price_cents": 8500,
        "quantity": 1,
        "line_total_cents": 8500,
        "options": None,
        "special_instructions": None,
    },
]


# ════════════════════════════════════════════════════════════════════════════════
# Fix 1 — _extract_modifier_from_suffix: "take out X" → "no X"
# ════════════════════════════════════════════════════════════════════════════════

class TestExtractModifierSuffixTakeOut:

    @pytest.mark.parametrize("suffix, expected", [
        ("take out tomato",         "no tomato"),
        ("take off tomato",         "no tomato"),
        ("take away tomato",        "no tomato"),
        ("leave out onion",         "no onion"),
        ("leave off lettuce",       "no lettuce"),
        ("dont put tomato",         "no tomato"),
        ("don't put onion",         "no onion"),
        # Combined: removal + addition
        ("take out tomato extra cheese", "no tomato, extra cheese"),
        # Existing patterns must not regress
        ("no tomato",               "no tomato"),
        ("without tomato",          "no tomato"),
        ("extra cheese",            "extra cheese"),
        ("with extra cheese",       "extra cheese"),
    ])
    def test_suffix_patterns(self, suffix, expected):
        result = _extract_modifier_from_suffix(suffix)
        assert result == expected, (
            f"suffix={suffix!r}: expected {expected!r}, got {result!r}"
        )

    def test_empty_suffix_returns_none(self):
        assert _extract_modifier_from_suffix("") is None

    def test_unrelated_text_returns_none(self):
        assert _extract_modifier_from_suffix("please") is None

    def test_item_name_in_suffix_does_not_produce_spurious_modifier(self):
        # "from the Classic Smash Burger" should not produce a modifier
        result = _extract_modifier_from_suffix("from the Classic Smash Burger")
        assert result is None


# ════════════════════════════════════════════════════════════════════════════════
# Fix 1 (continued) — extraction of "Classic Smash Burger take out tomato"
# ════════════════════════════════════════════════════════════════════════════════

class TestItemExtractionWithTakeOut:

    def test_failure1_burger_plus_take_out_tomato(self):
        """
        BETA FAILURE 1: 'Classic Smash Burger take out tomato'
        Extraction must return burger WITH modifier 'no tomato'.
        """
        matches = _extract_items_from_message(
            normalize("Classic Smash Burger take out tomato"), _MENU
        )
        assert len(matches) == 1, f"Expected 1 match, got {len(matches)}: {matches}"
        item, qty, modifier = matches[0]
        assert "burger" in item.name.lower(), f"Unexpected item: {item.name}"
        assert modifier == "no tomato", (
            f"Expected modifier='no tomato', got {modifier!r}"
        )

    @pytest.mark.parametrize("msg", [
        "Classic Smash Burger take out tomato",
        "Classic Smash Burger leave out onion",
        "Classic Smash Burger take off tomato",
        "Classic Smash Burger no tomato",
        "Classic Smash Burger without lettuce",
    ])
    def test_burger_with_various_removal_phrasings(self, msg):
        matches = _extract_items_from_message(normalize(msg), _MENU)
        assert matches, f"No item extracted from: {msg!r}"
        _, _, modifier = matches[0]
        assert modifier is not None, f"No modifier extracted from: {msg!r}"


# ════════════════════════════════════════════════════════════════════════════════
# Fix 2 — _detect_ingredient_modifier_from_remove
# ════════════════════════════════════════════════════════════════════════════════

class TestDetectIngredientModifierFromRemove:

    def test_failure2_take_out_tomato_standalone(self):
        """
        BETA FAILURE 2: 'take out tomato' with burger in cart.
        Must return ("no tomato", burger_cart_item).
        """
        result = _detect_ingredient_modifier_from_remove(
            "take out tomato", _BURGER_CART, _MENU_ITEM_NAMES
        )
        assert result is not None, (
            "'take out tomato' with a burger in cart must be detected as ingredient modifier"
        )
        modifier, item = result
        assert "tomato" in modifier, modifier
        assert "burger" in item["name"].lower(), item["name"]

    def test_failure3_take_out_tomato_from_explicit_item(self):
        """
        BETA FAILURE 3: 'take out tomato from the Classic Smash Burger'
        Must return ("no tomato", burger_cart_item).
        """
        result = _detect_ingredient_modifier_from_remove(
            "take out tomato from the Classic Smash Burger",
            _TWO_ITEM_CART,
            _MENU_ITEM_NAMES,
        )
        assert result is not None, (
            "'take out tomato from the Classic Smash Burger' must detect ingredient modifier"
        )
        modifier, item = result
        assert "tomato" in modifier
        assert "burger" in item["name"].lower()

    # ── All SA ingredient removal phrasings ──────────────────────────────────

    @pytest.mark.parametrize("msg", [
        "remove tomato",
        "take out tomato",
        "take off tomato",
        "take away tomato",
        "leave out onion",
        "no tomato",
        "without onion",
        "dont put tomato",
        "don't add cheese",
        "remove tomato from the burger",
        "take out onion from my burger",
    ])
    def test_ingredient_phrases_detected(self, msg):
        norm = normalize(msg)
        result = _detect_ingredient_modifier_from_remove(
            norm, _BURGER_CART, _MENU_ITEM_NAMES
        )
        assert result is not None, (
            f"Expected ingredient modifier for: {msg!r} (norm: {norm!r})"
        )

    # ── Real cart removals must NOT match ────────────────────────────────────

    @pytest.mark.parametrize("msg", [
        "remove the burger",
        "take out the pizza",
        "take out the Classic Smash Burger",
        "remove the Classic Smash Burger",
        "take out the Medium Chicken Mayo Pizza",
    ])
    def test_cart_item_removals_not_matched(self, msg):
        norm = normalize(msg)
        result = _detect_ingredient_modifier_from_remove(
            norm, _TWO_ITEM_CART, _MENU_ITEM_NAMES
        )
        assert result is None, (
            f"Should NOT match ingredient modifier for cart removal: {msg!r}\n"
            f"Got: {result}"
        )

    def test_returns_none_when_cart_empty(self):
        result = _detect_ingredient_modifier_from_remove(
            "take out tomato", [], _MENU_ITEM_NAMES
        )
        assert result is None

    def test_explicit_item_ref_selects_correct_cart_item(self):
        """With pizza + burger in cart, 'from the burger' targets burger."""
        result = _detect_ingredient_modifier_from_remove(
            "take out tomato from the burger",
            _TWO_ITEM_CART,
            _MENU_ITEM_NAMES,
        )
        assert result is not None
        _, item = result
        assert "burger" in item["name"].lower(), (
            f"Expected burger, got {item['name']!r}"
        )


# ════════════════════════════════════════════════════════════════════════════════
# _INGREDIENT_WORDS completeness
# ════════════════════════════════════════════════════════════════════════════════

class TestIngredientWordsCoverage:

    @pytest.mark.parametrize("word", [
        "tomato", "onion", "lettuce", "cheese", "sauce",
        "mayo", "pickle", "jalapeno", "pepper", "bacon",
        "avocado", "avo", "egg", "garlic", "mushroom",
        "chili", "chilli", "spicy",
    ])
    def test_common_sa_ingredients_in_set(self, word):
        assert word in _INGREDIENT_WORDS, f"'{word}' should be in _INGREDIENT_WORDS"

    @pytest.mark.parametrize("word", [
        "burger", "pizza", "smash", "chicken", "classic", "medium",
    ])
    def test_menu_item_words_excluded(self, word):
        assert word not in _INGREDIENT_WORDS, (
            f"Menu item keyword '{word}' must NOT be in _INGREDIENT_WORDS"
        )


# ════════════════════════════════════════════════════════════════════════════════
# ORDER_REMOVE intent must still fire for disambiguation purposes
# (the new DET blocks intercept before LLM, but intent detection is unchanged)
# ════════════════════════════════════════════════════════════════════════════════

class TestOrderRemoveIntentStillFires:

    @pytest.mark.parametrize("msg", [
        "take out tomato",
        "take out the burger",
        "remove the pizza",
        "leave out onion",
    ])
    def test_order_remove_intent_matches(self, msg):
        norm = normalize(msg)
        intent = match_intent(norm)
        assert intent == MessageIntent.ORDER_REMOVE, (
            f"Expected ORDER_REMOVE for {msg!r}, got {intent}"
        )
