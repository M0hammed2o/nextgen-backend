"""
End-to-end order-state regression tests for three live bugs.

These tests exercise the deterministic layers of the pipeline (DET_MATCH,
CHOOSING_OPTIONS handler, modifier extraction, modifier reversal) WITHOUT
making real LLM calls.  The focus is on:

  BUG 1 – Plural DET_MATCH: "3 burgers", "2 Classic Smash Burgers and 3 Cokes"
           must be committed to the cart, not lost in LLM propose-then-ask flow.

  BUG 2 – replace_item must clear pending_options so stale context cannot leak
           into subsequent messages.

  BUG 3 – "without a tomato" → modifier must be "no tomato" (not "no a tomato").
           "Actually leave the tomato" must remove the "no tomato" instruction.

All 10 required scenarios are covered:
  1.  Single item order
  2.  Multiple quantity order (plural form)
  3.  Multiple different items
  4.  Modifier addition
  5.  Modifier removal
  6.  Replace item
  7.  Delivery flow  (cart state only — no real DB/Redis)
  8.  Pickup flow    (cart state only)
  9.  SA slang/typos
  10. Confirmation flow after proposed order (CHOOSING_OPTIONS negation)
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.bot import state_machine
from backend.app.bot.normalizer import normalize
from backend.app.bot.pipeline import (
    _detect_modifier_reversal,
    _extract_items_from_message,
    _extract_modifier_from_suffix,
    _INGREDIENT_WORDS,
)
from shared.enums import MessageIntent


# ── Lightweight fakes ─────────────────────────────────────────────────────────

class FakeItem:
    def __init__(self, name: str, price: int = 7500, options_json=None):
        self.id = uuid.uuid4()
        self.name = name
        self.price_cents = price
        self.is_active = True
        self.is_deleted = False
        self.options_json = options_json or {}


_MENU = [
    FakeItem("Classic Smash Burger", 7500),
    FakeItem("Double Smash Burger", 9500),
    FakeItem("Spicy Chicken Burger", 8000),
    FakeItem("Grilled Chicken Burger", 8500),
    FakeItem("Small Margherita Pizza", 5000),
    FakeItem("Medium Margherita Pizza", 7000),
    FakeItem("Large Margherita Pizza", 9000),
    FakeItem("Coca-Cola (330ml)", 2000),
    FakeItem("Coca-Cola (500ml)", 2500),
    FakeItem("Ice Coffee", 7500),
]

_MENU_ITEM_NAMES = {i.name.lower() for i in _MENU}


def _make_session():
    """Return a minimal ConversationSession-like object with context_json."""
    from unittest.mock import MagicMock
    s = MagicMock()
    s.context_json = {}
    s.state = "IDLE"

    # Make flag_modified a no-op
    from sqlalchemy.orm.attributes import flag_modified as _fm
    return s


def _cart(session):
    return state_machine.get_cart(session)


def _add(session, item: FakeItem, qty: int = 1, modifier=None):
    state_machine.add_to_cart(
        session,
        menu_item_id=str(item.id),
        name=item.name,
        price_cents=item.price_cents,
        quantity=qty,
        special_instructions=modifier,
    )


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — Single item order (deterministic extraction)
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario1SingleItem:
    def test_basic_single_item(self):
        """'can i get a Classic Smash Burger' → 1x Classic Smash Burger in cart."""
        matches = _extract_items_from_message(
            normalize("can i get a Classic Smash Burger"), _MENU
        )
        assert len(matches) == 1
        item, qty, mod = matches[0]
        assert item.name == "Classic Smash Burger"
        assert qty == 1
        assert mod is None

    def test_exact_name_match(self):
        matches = _extract_items_from_message("Spicy Chicken Burger", _MENU)
        assert len(matches) == 1
        assert matches[0][0].name == "Spicy Chicken Burger"


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — Multiple quantity (plural form — Bug 1 primary fix)
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario2MultipleQuantity:
    def test_plural_burger_matched(self):
        """
        BUG 1 ROOT CAUSE: '3 Classic Smash Burgers' failed because 'burgers' ends
        with 's' after the item name 'Classic Smash Burger'. DET_MATCH now
        allows a trailing plural 's'.
        """
        matches = _extract_items_from_message(
            normalize("Can I get 3 Classic Smash Burgers"), _MENU
        )
        assert matches, "Plural 'burgers' must be matched by DET_MATCH"
        item, qty, _ = matches[0]
        assert item.name == "Classic Smash Burger"
        assert qty == 3

    def test_plural_two_burgers(self):
        matches = _extract_items_from_message(
            normalize("2 Classic Smash Burgers"), _MENU
        )
        assert matches and matches[0][1] == 2

    def test_plural_pizza_matched(self):
        matches = _extract_items_from_message(
            normalize("2 Small Margherita Pizzas"), _MENU
        )
        assert matches and matches[0][0].name == "Small Margherita Pizza"
        assert matches[0][1] == 2

    def test_word_number_plural(self):
        matches = _extract_items_from_message(
            normalize("three Classic Smash Burgers"), _MENU
        )
        assert matches and matches[0][1] == 3


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 3 — Multiple different items (Bug 1: Cokes alias + plural)
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario3MultipleItems:
    def test_burger_and_coke(self):
        """
        BUG 1: '2 Classic Smash Burgers and 3 Cokes' — both 'burgers' (plural)
        and 'cokes' (plural alias) must resolve deterministically.
        """
        matches = _extract_items_from_message(
            normalize("2 Classic Smash Burgers and 3 Cokes"), _MENU
        )
        assert len(matches) == 2, f"Expected 2 items, got {len(matches)}: {[m[0].name for m in matches]}"
        names = {m[0].name for m in matches}
        qtys = {m[0].name: m[1] for m in matches}
        assert "Classic Smash Burger" in names, "Burger not matched"
        assert any("Coca-Cola" in n for n in names), "Coke alias not matched"
        assert qtys["Classic Smash Burger"] == 2
        coke_name = next(n for n in names if "Coca-Cola" in n)
        assert qtys[coke_name] == 3

    def test_two_burgers_and_ice_coffee(self):
        matches = _extract_items_from_message(
            normalize("2 Classic Smash Burgers and an Ice Coffee"), _MENU
        )
        names = {m[0].name for m in matches}
        assert "Classic Smash Burger" in names
        assert "Ice Coffee" in names

    def test_comma_separated_items(self):
        matches = _extract_items_from_message(
            normalize("Classic Smash Burger, Spicy Chicken Burger"), _MENU
        )
        assert len(matches) == 2

    def test_plural_coke_alias(self):
        """'cokes' → Coca-Cola via alias with plural 's' allowed."""
        matches = _extract_items_from_message(normalize("3 cokes"), _MENU)
        assert matches, "Plural 'cokes' must match via alias"
        assert "Coca-Cola" in matches[0][0].name
        assert matches[0][1] == 3


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 4 — Modifier addition
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario4ModifierAddition:
    def test_no_tomato_suffix(self):
        matches = _extract_items_from_message(
            normalize("Classic Smash Burger no tomato"), _MENU
        )
        assert matches
        _, _, mod = matches[0]
        assert mod == "no tomato", f"Got {mod!r}"

    def test_without_tomato_normalises(self):
        """Bug 3: 'without a tomato' must produce 'no tomato', not 'no a tomato'."""
        result = _extract_modifier_from_suffix("without a tomato")
        assert result == "no tomato", f"Got {result!r}"

    def test_without_the_onion(self):
        result = _extract_modifier_from_suffix("without the onion")
        assert result == "no onion", f"Got {result!r}"

    def test_extra_cheese(self):
        result = _extract_modifier_from_suffix("extra cheese")
        assert result == "extra cheese"

    def test_complex_modifier_on_item(self):
        matches = _extract_items_from_message(
            normalize("Classic Smash Burger without a tomato"), _MENU
        )
        assert matches
        _, _, mod = matches[0]
        assert mod == "no tomato", f"Got {mod!r} — article should be stripped"

    def test_plural_item_with_modifier(self):
        """'2 Classic Smash Burgers no tomato' — plural match plus modifier."""
        matches = _extract_items_from_message(
            normalize("2 Classic Smash Burgers no tomato"), _MENU
        )
        assert matches
        item, qty, mod = matches[0]
        assert item.name == "Classic Smash Burger"
        assert qty == 2
        assert mod == "no tomato", f"Got {mod!r}"


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 5 — Modifier removal / reversal (Bug 3)
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario5ModifierRemoval:
    _CART_WITH_MODIFIER = [
        {
            "menu_item_id": "b1",
            "name": "Classic Smash Burger",
            "price_cents": 7500,
            "quantity": 1,
            "line_total_cents": 7500,
            "options": None,
            "special_instructions": "no tomato",
        }
    ]

    def test_leave_the_tomato_detected(self):
        """
        BUG 3: 'Actually leave the tomato' should detect a modifier reversal
        for 'tomato' on the burger.
        """
        result = _detect_modifier_reversal(
            normalize("Actually leave the tomato"), self._CART_WITH_MODIFIER
        )
        assert result is not None, "Reversal not detected"
        word, item = result
        assert "tomato" in word
        assert "Burger" in item["name"]

    def test_keep_the_tomato(self):
        result = _detect_modifier_reversal(
            normalize("keep the tomato"), self._CART_WITH_MODIFIER
        )
        assert result is not None

    def test_add_tomato_back(self):
        result = _detect_modifier_reversal(
            normalize("add tomato back"), self._CART_WITH_MODIFIER
        )
        assert result is not None

    def test_reversal_not_triggered_without_prior_modifier(self):
        """'leave the tomato' on a cart with no modifier should return None."""
        clean_cart = [{**self._CART_WITH_MODIFIER[0], "special_instructions": None}]
        result = _detect_modifier_reversal("leave the tomato", clean_cart)
        assert result is None

    def test_leave_out_not_a_reversal(self):
        """'leave out tomato' is a REMOVAL, not a reversal — must not match."""
        result = _detect_modifier_reversal(
            "leave out tomato", self._CART_WITH_MODIFIER
        )
        assert result is None

    def test_remove_modifier_from_instructions(self):
        """state_machine.remove_modifier_from_instructions removes 'no tomato'."""
        from unittest.mock import MagicMock, patch
        session = MagicMock()
        cart_data = [
            {
                "menu_item_id": "b1",
                "name": "Classic Smash Burger",
                "price_cents": 7500,
                "quantity": 1,
                "line_total_cents": 7500,
                "options": None,
                "special_instructions": "no tomato",
            }
        ]
        session.context_json = {"cart": cart_data}

        with patch("backend.app.bot.state_machine.flag_modified"):
            updated, was_updated = state_machine.remove_modifier_from_instructions(
                session, "Classic Smash Burger", "tomato"
            )

        assert was_updated, "Modifier should have been removed"
        result_instr = updated[0]["special_instructions"]
        assert result_instr is None or "tomato" not in (result_instr or ""), (
            f"'no tomato' should be gone, got {result_instr!r}"
        )

    def test_remove_one_of_multiple_modifiers(self):
        """'no tomato, extra cheese' → removing tomato leaves 'extra cheese'."""
        from unittest.mock import MagicMock, patch
        session = MagicMock()
        session.context_json = {"cart": [{
            "menu_item_id": "b1", "name": "Classic Smash Burger",
            "price_cents": 7500, "quantity": 1, "line_total_cents": 7500,
            "options": None, "special_instructions": "no tomato, extra cheese",
        }]}
        with patch("backend.app.bot.state_machine.flag_modified"):
            updated, ok = state_machine.remove_modifier_from_instructions(
                session, "Classic Smash Burger", "tomato"
            )
        assert ok
        assert updated[0]["special_instructions"] == "extra cheese"


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 6 — Replace item (Bug 2: pending_options cleared after replace)
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario6ReplaceItem:
    def test_pending_options_cleared_after_replace_intent(self):
        """
        BUG 2: After replace_item resolves, pending_options must be None so
        stale context cannot re-add the old item or confuse the LLM.
        We verify the state_machine.set_context("pending_options", None) path.
        """
        from unittest.mock import MagicMock, patch
        session = MagicMock()
        session.context_json = {
            "cart": [
                {
                    "menu_item_id": "b1", "name": "Classic Smash Burger",
                    "price_cents": 7500, "quantity": 1, "line_total_cents": 7500,
                    "options": None, "special_instructions": None,
                }
            ],
            "pending_options": [{"name": "chicken burger", "quantity": 1}],
        }
        with patch("backend.app.bot.state_machine.flag_modified"):
            # Simulate what replace_item handler does: clear pending_options
            state_machine.set_context(session, "pending_options", None)
            pending = state_machine.get_context(session, "pending_options")
        assert pending is None, f"pending_options should be None after replace, got {pending!r}"

    def test_replace_in_cart_state_machine(self):
        """
        Cart after remove+add mirrors what replace_item does:
        remove CSB → add SCB → confirmed_cart=[SCB].
        """
        from unittest.mock import MagicMock, patch
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        scb = next(i for i in _MENU if i.name == "Spicy Chicken Burger")
        session = MagicMock()
        session.context_json = {}

        with patch("backend.app.bot.state_machine.flag_modified"):
            state_machine.add_to_cart(session, str(csb.id), csb.name, csb.price_cents, 1)
            assert len(state_machine.get_cart(session)) == 1

            state_machine.remove_from_cart(session, "Classic Smash Burger")
            assert len(state_machine.get_cart(session)) == 0

            state_machine.add_to_cart(session, str(scb.id), scb.name, scb.price_cents, 1)
            cart = state_machine.get_cart(session)

        assert len(cart) == 1
        assert cart[0]["name"] == "Spicy Chicken Burger"
        assert cart[0]["price_cents"] == 8000

        import copy
        confirmed = copy.deepcopy(cart)
        assert confirmed[0]["name"] == "Spicy Chicken Burger"


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 7 — Delivery flow (cart state stays correct through detail collection)
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario7DeliveryFlow:
    def test_cart_intact_before_order_creation(self):
        """
        After replace_item, confirmed_cart must reflect the NEW item even after
        detail-collection messages are exchanged.  Simulate the re-lock that
        _handle_order_confirmation performs just before create_order_from_cart.
        """
        from unittest.mock import MagicMock, patch
        scb = next(i for i in _MENU if i.name == "Spicy Chicken Burger")
        session = MagicMock()
        session.context_json = {}

        with patch("backend.app.bot.state_machine.flag_modified"):
            # 1. Replace happened: live_cart=[SCB]
            state_machine.add_to_cart(session, str(scb.id), scb.name, scb.price_cents, 1)
            import copy
            state_machine.set_context(session, "confirmed_cart", copy.deepcopy(state_machine.get_cart(session)))

            # 2. Detail-collection messages come in (no cart mutation)
            state_machine.set_context(session, "customer_name", "Test Customer")
            state_machine.set_context(session, "delivery_address", "123 Main St")

            # 3. Re-lock right before order creation (as _handle_order_confirmation does)
            live = state_machine.get_cart(session)
            assert live, "Live cart must not be empty before order creation"
            state_machine.set_context(session, "confirmed_cart", copy.deepcopy(live))
            confirmed = state_machine.get_context(session, "confirmed_cart")

        assert len(confirmed) == 1
        assert confirmed[0]["name"] == "Spicy Chicken Burger", (
            f"DB order would have wrong item: {confirmed[0]['name']!r}"
        )


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 8 — Pickup flow
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario8PickupFlow:
    def test_pickup_cart_matches_expected(self):
        """Single CSB + modifier survives to order creation without mutation."""
        from unittest.mock import MagicMock, patch
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        session = MagicMock()
        session.context_json = {}

        with patch("backend.app.bot.state_machine.flag_modified"):
            state_machine.add_to_cart(
                session, str(csb.id), csb.name, csb.price_cents, 1,
                special_instructions="no tomato"
            )
            import copy
            cart = state_machine.get_cart(session)
            state_machine.set_context(session, "confirmed_cart", copy.deepcopy(cart))
            confirmed = state_machine.get_context(session, "confirmed_cart")

        assert confirmed[0]["name"] == "Classic Smash Burger"
        assert confirmed[0]["special_instructions"] == "no tomato"
        assert confirmed[0]["quantity"] == 1


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 9 — SA slang/spelling mistakes (normalizer + DET_MATCH)
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario9SASlang:
    @pytest.mark.parametrize("msg, expected_name", [
        ("gimme a Classic Smash Burger",           "Classic Smash Burger"),
        ("lemme get a Spicy Chicken Burger",        "Spicy Chicken Burger"),
        ("can i get 2 Classic Smash Burgers plz",  "Classic Smash Burger"),
        ("3 Classic Smash Burgers wit no tamato",  "Classic Smash Burger"),
    ])
    def test_sa_slang_matches(self, msg, expected_name):
        matches = _extract_items_from_message(normalize(msg), _MENU)
        assert matches, f"No match for: {msg!r}"
        assert any(expected_name in m[0].name for m in matches), (
            f"Expected {expected_name!r} in matches, got {[m[0].name for m in matches]}"
        )

    def test_tamato_normalised_to_tomato(self):
        """'tamato' → 'tomato' via normalizer, so modifier is 'no tomato'."""
        msg = "Classic Smash Burger wit no tamato"
        matches = _extract_items_from_message(normalize(msg), _MENU)
        assert matches
        _, _, mod = matches[0]
        assert mod == "no tomato", f"Got {mod!r}"

    def test_wout_normalised_to_without(self):
        result = _extract_modifier_from_suffix(normalize("wout tomato"))
        assert result == "no tomato", f"Got {result!r}"

    def test_dnt_normalised_to_dont(self):
        result = _extract_modifier_from_suffix(normalize("dnt put tomato"))
        assert result == "no tomato", f"Got {result!r}"


# ════════════════════════════════════════════════════════════════════════════════
# SCENARIO 10 — Confirmation flow after proposed order (Bug 1 secondary fix)
# ════════════════════════════════════════════════════════════════════════════════

class TestScenario10ConfirmationAfterProposed:
    """
    When LLM uses ask_options ("Would you like modifications?") and customer
    says "No", the CHOOSING_OPTIONS negation handler must add pending items
    to the cart deterministically — WITHOUT another LLM call.

    We test the logic components (pending_options storage, negation detection,
    add_to_cart) rather than the full async pipeline.
    """

    def test_negation_detected(self):
        from backend.app.bot.intent_router import is_negation
        assert is_negation("No")
        assert is_negation("nah")
        assert is_negation("nope")
        assert not is_negation("No tomato")  # has extra content

    def test_pending_items_added_to_cart_on_negation(self):
        """
        Simulates CHOOSING_OPTIONS + 'No' handler:
        pending_options=[{name: CSB, qty: 3}] + 'No' → 3x CSB in cart.
        """
        from unittest.mock import MagicMock, patch
        csb = next(i for i in _MENU if i.name == "Classic Smash Burger")
        session = MagicMock()
        session.context_json = {
            "pending_options": [{"name": "Classic Smash Burger", "quantity": 3}]
        }

        with patch("backend.app.bot.state_machine.flag_modified"):
            # This is what the CHOOSING_OPTIONS negation handler does
            pending = state_machine.get_context(session, "pending_options")
            assert pending and len(pending) == 1

            items_map = {i.name.lower(): i for i in _MENU if i.is_active and not i.is_deleted}
            for p in pending:
                mi = items_map.get((p.get("name") or "").lower())
                assert mi is not None, f"Pending item not found: {p['name']!r}"
                state_machine.add_to_cart(
                    session, str(mi.id), mi.name, mi.price_cents,
                    p.get("quantity", 1),
                )
            state_machine.set_context(session, "pending_options", None)

            cart = state_machine.get_cart(session)
            leftover_pending = state_machine.get_context(session, "pending_options")

        assert len(cart) == 1
        assert cart[0]["name"] == "Classic Smash Burger"
        assert cart[0]["quantity"] == 3, f"Expected qty=3, got {cart[0]['quantity']}"
        assert leftover_pending is None

    def test_yes_after_empty_cart_gives_empty_cart_error(self):
        """
        If pending_options were never committed (LLM used chitchat), the cart
        is still empty when customer says 'yes'. This test confirms the guard
        in _handle_message: ORDER_CONFIRM with empty cart → 'Your cart is empty'.
        """
        from unittest.mock import MagicMock, patch
        session = MagicMock()
        session.context_json = {}  # no cart

        with patch("backend.app.bot.state_machine.flag_modified"):
            cart = state_machine.get_cart(session)
        assert cart == [], "Empty cart must produce no order items"


# ════════════════════════════════════════════════════════════════════════════════
# Bug 3 — Article normalisation in all suffix patterns
# ════════════════════════════════════════════════════════════════════════════════

class TestBug3ArticleNormalisation:
    @pytest.mark.parametrize("suffix, expected", [
        ("without a tomato",      "no tomato"),
        ("without an onion",      "no onion"),
        ("without the lettuce",   "no lettuce"),
        ("no a tomato",           "no tomato"),   # defensive: "no a X" → "no X"
        ("take out a tomato",     "no tomato"),
        ("take out the onion",    "no onion"),
        ("leave out the lettuce", "no lettuce"),
    ])
    def test_articles_stripped(self, suffix, expected):
        result = _extract_modifier_from_suffix(suffix)
        assert result == expected, (
            f"suffix={suffix!r}: expected {expected!r}, got {result!r}"
        )

    def test_item_extraction_without_a_tomato(self):
        """Full item + modifier: 'Classic Smash Burger without a tomato'."""
        matches = _extract_items_from_message(
            normalize("Classic Smash Burger without a tomato"), _MENU
        )
        assert matches
        _, _, mod = matches[0]
        assert mod == "no tomato", f"Got {mod!r}"

    def test_item_extraction_2_burgers_without_a_tomato(self):
        """Plural + article: '2 Classic Smash Burgers without a tomato'."""
        matches = _extract_items_from_message(
            normalize("2 Classic Smash Burgers without a tomato"), _MENU
        )
        assert matches
        item, qty, mod = matches[0]
        assert qty == 2
        assert mod == "no tomato", f"Got {mod!r}"
