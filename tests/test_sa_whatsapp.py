"""
South African WhatsApp ordering regression tests.

Every test case in this file is derived from real or realistic SA WhatsApp
ordering messages collected during beta testing.  The goal is to ensure
that typos, abbreviations, slang, and casual phrasing do not break the
core ordering flows.

Coverage:
  1. Normalizer — word-level corrections (typos, abbreviations, slang)
  2. Confirmation detection — SA slang + sentence-form confirmations
  3. Modifier detection — natural SA phrasing after normalization
  4. Mixed intent — modifier + new item in the same message
  5. Cart disambiguation — "remove the plain burger" vs "the no tomato one"
  6. Partial name ordering — "Chicken Pizza" → closest menu match
  7. ORDER_REMOVE intent — "take out", "leave out", "take off"
  8. Full beta conversation replay (the exact sequence from the beta report)
"""
import uuid
from unittest.mock import patch

import pytest

from backend.app.bot import state_machine
from backend.app.bot.intent_router import (
    is_confirmation,
    is_negation,
    is_cart_correction,
    match_intent,
)
from backend.app.bot.normalizer import normalize
from backend.app.bot.pipeline import _detect_modifier_update, _extract_items_from_message
from shared.enums import MessageIntent


# ── Helpers ────────────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, state: str = "CONFIRMING_ORDER"):
        self.id = uuid.uuid4()
        self.state = state
        self.context_json: dict = {}


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
    FakeItem("Large Chicken Mayo Pizza", 11000),
    FakeItem("Medium BBQ Steak Pizza", 9500),
    FakeItem("Coca-Cola (330ml)", 2000),
    FakeItem("Ice Coffee", 7500),
]


# ════════════════════════════════════════════════════════════════════════════════
# 1. Normalizer
# ════════════════════════════════════════════════════════════════════════════════

class TestNormalizer:

    # ── Ingredient typos ─────────────────────────────────────────────────────

    @pytest.mark.parametrize("raw, expected_substring", [
        ("add xtra cheese",              "extra cheese"),
        ("add cheez please",             "cheese"),
        ("no tamato",                    "no tomato"),
        ("no tamarto please",            "no tomato"),
        ("without onyon",                "without onion"),
        ("add saus",                     "add sauce"),
        ("add saace",                    "add sauce"),
    ])
    def test_ingredient_typos_corrected(self, raw, expected_substring):
        result = normalize(raw)
        assert expected_substring in result.lower(), (
            f"normalize({raw!r}) = {result!r} — expected {expected_substring!r}"
        )

    # ── SA connectors ─────────────────────────────────────────────────────────

    @pytest.mark.parametrize("raw, expected_substring", [
        ("burger wit no tomato",         "with no tomato"),
        ("burger wid no onion",          "with no onion"),
        ("wout tomato",                  "without tomato"),
        ("remove da burger",             "remove the burger"),
        ("cn i get a burger",            "can i get"),
        ("dnt put onion",                "don't put onion"),
        ("dont put onion",               "don't put onion"),
        ("pls add chips",                "please add chips"),
        ("gimme a burger",               "give me a burger"),
    ])
    def test_sa_connectors_corrected(self, raw, expected_substring):
        result = normalize(raw)
        assert expected_substring in result.lower(), (
            f"normalize({raw!r}) = {result!r}"
        )

    # ── Punctuation shortcuts ──────────────────────────────────────────────────

    def test_w_slash_o_becomes_without(self):
        assert "without" in normalize("w/o tomato").lower()

    def test_w_slash_becomes_with(self):
        assert "with" in normalize("w/ extra").lower()

    # ── SA affirmative slang ──────────────────────────────────────────────────

    def test_sho_becomes_sure(self):
        assert normalize("sho") == "sure"

    def test_aight_becomes_ok(self):
        assert normalize("aight") == "ok"

    def test_kk_becomes_ok(self):
        assert normalize("kk") == "ok"

    # ── "also add" simplification ─────────────────────────────────────────────

    def test_also_add_simplified(self):
        result = normalize("also add a coke")
        assert result.lower().startswith("add")

    # ── Idempotency ───────────────────────────────────────────────────────────

    @pytest.mark.parametrize("msg", [
        "Can I get 1 Classic Smash Burger with no tomato",
        "Add extra cheese to the burger",
        "yes please",
        "Remove the pizza",
    ])
    def test_normalize_is_idempotent(self, msg):
        once = normalize(msg)
        twice = normalize(once)
        assert once == twice, f"Not idempotent: {msg!r} → {once!r} → {twice!r}"

    # ── Normal words not damaged ──────────────────────────────────────────────

    def test_burger_word_not_mutated(self):
        """'burger' must not be affected by 'da'→'the' or other rules."""
        assert "burger" in normalize("Classic Smash Burger").lower()

    def test_order_word_preserved(self):
        assert "order" in normalize("place the order").lower()


# ════════════════════════════════════════════════════════════════════════════════
# 2. Confirmation detection — SA slang + sentence-form
# ════════════════════════════════════════════════════════════════════════════════

class TestSAConfirmations:

    # ── Exact beta-test failures ──────────────────────────────────────────────

    @pytest.mark.parametrize("text", [
        # From beta report: these all failed is_confirmation
        "No this is the correct order",
        "This is the correct order",
        "That is correct",
        "correct",
        "place the order",
        "place order",
        # Other missed sentence-form affirmatives
        "that is right",
        "this is right",
        "that's fine",
        "that is fine",
        "this is fine",
        "that's what I want",
        "confirm the order",
        "go ahead with it",
        "carry on",
    ])
    def test_sentence_form_confirmations(self, text):
        assert is_confirmation(text), f"Expected confirmation: {text!r}"

    # ── SA slang (normalizer converts before is_confirmation is called) ───────

    @pytest.mark.parametrize("raw, normalized", [
        ("sho",   "sure"),
        ("aight", "ok"),
        ("kk",    "ok"),
    ])
    def test_sa_slang_confirmation_after_normalize(self, raw, normalized):
        """Slang is normalised before is_confirmation, so test normalized form."""
        assert is_confirmation(normalized), (
            f"normalize({raw!r})={normalized!r} should be a confirmation"
        )

    # ── "ya" (not "yah", commonly missed) ────────────────────────────────────

    def test_ya_is_confirmation(self):
        assert is_confirmation("ya"), "'ya' must be accepted as confirmation"

    def test_ya_with_trailing_words(self):
        assert is_confirmation("ya please"), "'ya please' must be confirmation"

    # ── Existing passing cases must not regress ───────────────────────────────

    @pytest.mark.parametrize("text", [
        "yes", "yep", "yeah", "yebo", "sharp", "lekker",
        "ok", "sure", "done", "looks good", "perfect",
    ])
    def test_existing_confirmations_still_pass(self, text):
        assert is_confirmation(text)

    # ── These must NOT be confirmations ──────────────────────────────────────

    @pytest.mark.parametrize("text", [
        "yes but change the chips",
        "yes add another burger",
        "no thanks",
        "nah",
        "cancel",
        "Add extra cheese to the burger",
    ])
    def test_non_confirmations_unchanged(self, text):
        assert not is_confirmation(text), f"Should NOT be confirmation: {text!r}"


# ════════════════════════════════════════════════════════════════════════════════
# 3. Modifier detection — SA natural phrasing after normalization
# ════════════════════════════════════════════════════════════════════════════════

class TestSAModifierDetection:

    _cart = [
        {
            "menu_item_id": "burger-1",
            "name": "Classic Smash Burger",
            "price_cents": 7500,
            "quantity": 1,
            "line_total_cents": 7500,
            "options": None,
            "special_instructions": "no tomato",
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

    # ── Beta-reported failure: "also add" ─────────────────────────────────────

    def test_also_add_after_normalize(self):
        """'Can you also add extra cheese for me on the burger' — the exact beta failure."""
        raw = "Can you also add extra cheese for me on the burger"
        norm = normalize(raw)
        result = _detect_modifier_update(norm, self._cart)
        assert result is not None, (
            f"Modifier not detected in normalized: {norm!r}"
        )
        modifier, item = result
        assert "cheese" in modifier, modifier
        assert "burger" in item["name"].lower()

    # ── All beta-reported phrasing variations ─────────────────────────────────

    @pytest.mark.parametrize("raw", [
        "Add extra cheese to the burger",
        "Add extra cheese on the burger",
        "Add extra cheese for me on the burger",
        "can you add extra cheese to the burger",
        "can you add extra cheese for me on the burger",
        "please add extra cheese to the burger",
        "could you add extra cheese to the burger",
        "put extra cheese on the burger",
        "give me extra cheese on the burger",
    ])
    def test_modifier_detected_for_various_phrasings(self, raw):
        norm = normalize(raw)
        result = _detect_modifier_update(norm, self._cart)
        assert result is not None, f"Expected modifier match for: {raw!r} (normalized: {norm!r})"
        modifier, item = result
        assert "cheese" in modifier
        assert "burger" in item["name"].lower()

    # ── SA typo variations (normalizer does the heavy lifting) ───────────────

    @pytest.mark.parametrize("raw", [
        "add xtra cheese to the burger",
        "add xtra cheez to the burger",    # both typos together
        "Add xtra cheez on the burger",
    ])
    def test_typo_modifier_detected_after_normalization(self, raw):
        norm = normalize(raw)
        result = _detect_modifier_update(norm, self._cart)
        assert result is not None, f"Expected match after normalization of: {raw!r}"

    # ── "no tomato" on pizza ──────────────────────────────────────────────────

    def test_no_tomato_on_pizza(self):
        result = _detect_modifier_update("Add no tomato to the pizza", self._cart)
        assert result is not None
        modifier, item = result
        assert "tomato" in modifier
        assert "pizza" in item["name"].lower()

    # ── These should NOT match modifier pattern ───────────────────────────────

    @pytest.mark.parametrize("msg", [
        "Add a Medium Chicken Mayo Pizza",
        "Can I get a burger",
        "Remove the pizza",
    ])
    def test_non_modifier_messages_do_not_match(self, msg):
        assert _detect_modifier_update(msg, self._cart) is None, (
            f"Should NOT match as modifier: {msg!r}"
        )


# ════════════════════════════════════════════════════════════════════════════════
# 4. Mixed intent — modifier + new item in same message (Fix C)
# ════════════════════════════════════════════════════════════════════════════════

class TestMixedIntent:
    """
    Verifies that a message containing both a modifier instruction and a new item
    correctly applies both, rather than silently dropping the modifier.

    Uses the pipeline helpers directly, not the full async pipeline.
    """

    _cart_with_burger = [
        {
            "menu_item_id": "burger-1",
            "name": "Classic Smash Burger",
            "price_cents": 7500,
            "quantity": 1,
            "line_total_cents": 7500,
            "options": None,
            "special_instructions": "no tomato",
        }
    ]

    def test_modifier_chunk_detected_in_combined_message(self):
        """
        'Add extra cheese to the burger and add a medium chicken mayo pizza'
        — the modifier chunk 'Add extra cheese to the burger' must be detected
        even though the pizza chunk is also present.
        """
        import re as _re
        msg = "Add extra cheese to the burger and add a medium chicken mayo pizza"
        norm = normalize(msg)

        chunks = _re.split(r"\band\b|\balso\b|,", norm, flags=_re.I)
        matched_items = _extract_items_from_message(norm, _MENU)
        matched_names = {item.name.lower() for item, _, _ in matched_items}

        modifier_applied = False
        for chunk in chunks:
            chunk = chunk.strip()
            if any(name in chunk.lower() for name in matched_names):
                continue
            result = _detect_modifier_update(chunk, self._cart_with_burger)
            if result:
                modifier_applied = True
                modifier, target = result
                assert "cheese" in modifier
                assert "burger" in target["name"].lower()

        assert modifier_applied, (
            "No modifier chunk detected in combined message. "
            "Fix C (mixed intent) is not working."
        )

    def test_new_item_detected_in_combined_message(self):
        """The pizza must still be detected as a new menu item."""
        msg = "Add extra cheese to the burger and add a medium chicken mayo pizza"
        norm = normalize(msg)
        matches = _extract_items_from_message(norm, _MENU)
        names = [item.name for item, _, _ in matches]
        assert any("pizza" in n.lower() for n in names), (
            f"Pizza not found in matches: {names}"
        )


# ════════════════════════════════════════════════════════════════════════════════
# 5. Cart disambiguation — qualifier-based remove (Fix D)
# ════════════════════════════════════════════════════════════════════════════════

class TestCartDisambiguation:

    def _session_with_two_burgers(self):
        s = FakeSession()
        iid = str(uuid.uuid4())
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 7500,
                                   quantity=1, special_instructions=None)
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 7500,
                                   quantity=1, special_instructions="no tomato")
        return s

    def test_plain_qualifier_removes_unmodified_item(self):
        """
        'remove the plain burger' → removes the one with no special_instructions.
        """
        s = self._session_with_two_burgers()
        cart_before = state_machine.get_cart(s)
        assert len(cart_before) == 2

        cart, removed = state_machine.remove_from_cart(
            s, "Classic Smash Burger",
            qualifier_hint="remove the plain burger"
        )
        assert removed is True
        assert len(cart) == 1
        # The remaining item should be the one with the modifier
        assert cart[0]["special_instructions"] == "no tomato", (
            "Plain burger removed but modified burger should remain"
        )

    def test_normal_qualifier_removes_unmodified_item(self):
        """'remove the normal burger' → removes plain entry."""
        s = self._session_with_two_burgers()
        cart, removed = state_machine.remove_from_cart(
            s, "Classic Smash Burger",
            qualifier_hint="take out the normal classic smash burger"
        )
        assert removed is True
        assert len(cart) == 1
        assert cart[0]["special_instructions"] == "no tomato"

    def test_ingredient_qualifier_removes_modified_item(self):
        """
        'remove the no tomato burger' → removes the one with 'no tomato'.
        """
        s = self._session_with_two_burgers()
        cart, removed = state_machine.remove_from_cart(
            s, "Classic Smash Burger",
            qualifier_hint="remove the no tomato classic smash burger"
        )
        assert removed is True
        assert len(cart) == 1
        assert cart[0]["special_instructions"] is None, (
            "Modified burger removed but plain burger should remain"
        )

    def test_no_qualifier_removes_first_match(self):
        """Without a qualifier hint, existing behaviour: first match removed."""
        s = self._session_with_two_burgers()
        cart, removed = state_machine.remove_from_cart(s, "Classic Smash Burger")
        assert removed is True
        assert len(cart) == 1

    def test_single_item_removes_correctly_regardless_of_qualifier(self):
        """When only one matching item exists, qualifier is irrelevant."""
        s = FakeSession()
        state_machine.add_to_cart(s, str(uuid.uuid4()), "Classic Smash Burger", 7500)
        cart, removed = state_machine.remove_from_cart(
            s, "Classic Smash Burger",
            qualifier_hint="remove the normal burger"
        )
        assert removed is True
        assert len(cart) == 0


# ════════════════════════════════════════════════════════════════════════════════
# 6. ORDER_REMOVE intent — new SA phrasing (Fix ORDER_REMOVE)
# ════════════════════════════════════════════════════════════════════════════════

class TestOrderRemoveIntent:

    @pytest.mark.parametrize("msg", [
        # Already working
        "remove the pizza",
        "delete the coke",
        # New patterns from beta
        "take out the burger",
        "take out the normal burger",
        "take off the pizza",
        "leave out the coke",
        "leave off the extra burger",
        "take away the pizza",
        # Compound — intent should still be ORDER_REMOVE
        "can you take out the plain burger",
        "please leave out the tomato burger",
    ])
    def test_order_remove_intent_matched(self, msg):
        norm = normalize(msg)
        intent = match_intent(norm)
        assert intent == MessageIntent.ORDER_REMOVE, (
            f"Expected ORDER_REMOVE for: {msg!r} (normalized: {norm!r}), got {intent}"
        )


# ════════════════════════════════════════════════════════════════════════════════
# 7. Full beta conversation replay
# ════════════════════════════════════════════════════════════════════════════════

class TestBetaConversationReplay:
    """
    Replays the exact beta test sequence using the pipeline helpers, verifying
    that each step now produces the correct cart state.  No DB or LLM involved.
    """

    BURGER_ID = "aaaaaaaa-0000-0000-0000-000000000001"
    PIZZA_ID  = "bbbbbbbb-0000-0000-0000-000000000002"

    def _build_cart_turn1(self):
        """
        Turn: 'Can I get 1 Classic Smash Burger with no tomato and a medium Chicken Mayo Pizza'
        Expected: Burger(no tomato) + Pizza
        """
        s = FakeSession()

        # Simulate what the pipeline does after deterministic extraction
        matches = _extract_items_from_message(
            normalize("Can I get 1 Classic Smash Burger with no tomato and a medium Chicken Mayo Pizza"),
            _MENU,
        )
        for item, qty, modifier in matches:
            state_machine.add_to_cart(s, str(item.id), item.name, item.price_cents,
                                       quantity=qty, special_instructions=modifier)
        return s, matches

    def test_turn1_extracts_both_items(self):
        """The extraction must find both items from the combined message."""
        _, matches = self._build_cart_turn1()
        names = [item.name for item, _, _ in matches]
        assert any("burger" in n.lower() for n in names), "Burger not extracted"
        assert any("pizza" in n.lower() for n in names), "Pizza not extracted"

    def test_turn1_burger_has_no_tomato_modifier(self):
        """Burger extracted with modifier 'no tomato'."""
        _, matches = self._build_cart_turn1()
        burger_modifier = next(
            (mod for item, _, mod in matches if "burger" in item.name.lower()),
            "NOT_FOUND",
        )
        assert burger_modifier == "no tomato", (
            f"Expected 'no tomato', got {burger_modifier!r}"
        )

    def test_turn1_cart_has_two_items_not_three(self):
        """
        The classic beta failure: plain burger from recommendation + new burger(no tomato)
        = 2 items, not 3.  (Only arises when extraction is combined with existing cart.)
        """
        s, matches = self._build_cart_turn1()
        cart = state_machine.get_cart(s)
        assert len(cart) == 2, (
            f"Expected 2 cart items, got {len(cart)}: {[(i['name'], i['special_instructions']) for i in cart]}"
        )

    def test_turn2_extra_cheese_merges_with_no_tomato(self):
        """
        Turn: 'can you also add extra cheese for me on the burger'
        After normalization: 'can you add extra cheese for me on the burger'
        Expected: burger instructions = 'no tomato, extra cheese'
        """
        s, _ = self._build_cart_turn1()
        raw = "can you also add extra cheese for me on the burger"
        norm = normalize(raw)
        cart = state_machine.get_cart(s)
        result = _detect_modifier_update(norm, cart)

        assert result is not None, (
            f"Modifier not detected in: {norm!r}"
        )
        modifier, target = result
        state_machine.update_cart_item_instructions(s, target["name"], modifier)

        cart = state_machine.get_cart(s)
        burger = next(i for i in cart if "burger" in i["name"].lower())
        assert "no tomato" in burger["special_instructions"], burger["special_instructions"]
        assert "cheese" in burger["special_instructions"], burger["special_instructions"]

    def test_turn3_confirmation_accepted(self):
        """
        Turn: 'No this is the correct order'
        This was the confirmation that the bot rejected.  Must now return True.
        """
        assert is_confirmation("No this is the correct order"), (
            "'No this is the correct order' must be accepted as order confirmation"
        )

    def test_remove_plain_burger_leaves_modified_one(self):
        """
        Sequence:
          - Cart: Burger(plain) + Burger(no tomato) + Pizza
          - Customer: 'remove the normal Classic Smash Burger'
        Expected: only Burger(no tomato) + Pizza remain.
        """
        s = FakeSession()
        iid = str(uuid.uuid4())
        pid = str(uuid.uuid4())
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 7500,
                                   quantity=1, special_instructions=None)
        state_machine.add_to_cart(s, iid, "Classic Smash Burger", 7500,
                                   quantity=1, special_instructions="no tomato")
        state_machine.add_to_cart(s, pid, "Medium Chicken Mayo Pizza", 8500,
                                   quantity=1, special_instructions=None)

        cart, removed = state_machine.remove_from_cart(
            s, "Classic Smash Burger",
            qualifier_hint="remove the normal Classic Smash Burger and leave the rest"
        )

        assert removed is True
        assert len(cart) == 2
        burger = next((i for i in cart if "burger" in i["name"].lower()), None)
        assert burger is not None
        assert burger["special_instructions"] == "no tomato"
        pizza = next((i for i in cart if "pizza" in i["name"].lower()), None)
        assert pizza is not None


# ════════════════════════════════════════════════════════════════════════════════
# 8. is_cart_correction — must NOT match modifier or remove messages
# ════════════════════════════════════════════════════════════════════════════════

class TestCartCorrectionNonRegression:
    """
    Adding new patterns to is_cart_correction risks false positives.
    Confirm that common ordering messages do not accidentally trigger it.
    """

    @pytest.mark.parametrize("text", [
        "Add extra cheese to the burger",
        "can you also add extra cheese for me on the burger",
        "remove the pizza",
        "take out the normal burger",
        "Add a Coca-Cola",
        "I want to add wings",
        "no tomato please",
        "Can I get a burger",
    ])
    def test_common_messages_are_not_cart_corrections(self, text):
        assert not is_cart_correction(normalize(text)), (
            f"Should NOT be cart correction: {text!r}"
        )
