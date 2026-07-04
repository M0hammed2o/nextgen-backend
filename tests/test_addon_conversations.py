"""
Add-on conversation regression tests.

Covers the three production blockers exposed during live WhatsApp testing:

  BUG 1 — Paid add-on price not applied
  BUG 2 — Remove modifier removes wrong thing
  BUG 3 — Modifier replacement deletes the cart

Seven replay scenarios from the spec, plus pricing assertions on every step.

All tests are pure (no DB, no LLM, no mocks required).
"""
import uuid
import pytest

from backend.app.bot import state_machine
from backend.app.bot.pipeline import (
    _detect_addon_removal,
    _is_reference_not_target,
)
from shared.pricing.engine import calculate_line_item


# ── Helpers ────────────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self):
        self.id = uuid.uuid4()
        self.state = "BUILDING_CART"
        self.context_json: dict = {}


def _uid() -> str:
    return str(uuid.uuid4())


BURGER_ID = _uid()
LATTE_ID  = _uid()
COFFEE_ID = _uid()

AO_EXTRA_CHEESE_ID = _uid()
AO_EXTRA_PATTY_ID  = _uid()
AO_SOY_MILK_ID     = _uid()
AO_FULL_CREAM_ID   = _uid()

# Prices
BURGER_BASE  = 7500   # R75
LATTE_BASE   = 4500   # R45
COFFEE_BASE  = 3500   # R35
AO_CHEESE    = 1000   # R10
AO_PATTY     = 2500   # R25
AO_SOY_MILK  = 1000   # R10
AO_FULL_CREAM = 0     # R0 (included)


def _cheese_ao(qty: int = 1) -> dict:
    return {"add_on_id": AO_EXTRA_CHEESE_ID, "name": "Extra Cheese", "price_cents": AO_CHEESE, "quantity": qty}


def _patty_ao(qty: int = 1) -> dict:
    return {"add_on_id": AO_EXTRA_PATTY_ID, "name": "Extra Patty", "price_cents": AO_PATTY, "quantity": qty}


def _soy_milk_ao(qty: int = 1) -> dict:
    return {"add_on_id": AO_SOY_MILK_ID, "name": "Soy Milk", "price_cents": AO_SOY_MILK, "quantity": qty}


def _full_cream_ao(qty: int = 1) -> dict:
    return {"add_on_id": AO_FULL_CREAM_ID, "name": "Full Cream", "price_cents": AO_FULL_CREAM, "quantity": qty}


# ── add_ons_map used in _detect_addon_removal ─────────────────────────────────

BURGER_ADDONS_MAP = {
    BURGER_ID: [
        {"add_on_id": AO_EXTRA_CHEESE_ID, "name": "Extra Cheese", "price_cents": AO_CHEESE, "min_qty": 0, "max_qty": 5, "default_qty": 1},
        {"add_on_id": AO_EXTRA_PATTY_ID,  "name": "Extra Patty",  "price_cents": AO_PATTY,  "min_qty": 0, "max_qty": 3, "default_qty": 1},
    ],
    LATTE_ID: [
        {"add_on_id": AO_SOY_MILK_ID,   "name": "Soy Milk",   "price_cents": AO_SOY_MILK,  "min_qty": 0, "max_qty": 1, "default_qty": 1},
        {"add_on_id": AO_FULL_CREAM_ID, "name": "Full Cream", "price_cents": AO_FULL_CREAM, "min_qty": 0, "max_qty": 1, "default_qty": 1},
    ],
}


# ════════════════════════════════════════════════════════════════════════════════
# Pricing engine — core calculations
# ════════════════════════════════════════════════════════════════════════════════

class TestAddonPricingCalculations:
    """Verify the engine produces correct numbers for the real-world add-on prices."""

    def test_burger_plus_extra_cheese(self):
        b = calculate_line_item(BURGER_BASE, [], [_cheese_ao()], 1)
        assert b.add_on_total_cents == AO_CHEESE
        assert b.unit_price_cents == BURGER_BASE + AO_CHEESE   # 8500
        assert b.line_total_cents == BURGER_BASE + AO_CHEESE

    def test_burger_plus_extra_patty(self):
        b = calculate_line_item(BURGER_BASE, [], [_patty_ao()], 1)
        assert b.unit_price_cents == BURGER_BASE + AO_PATTY    # 10000

    def test_burger_plus_cheese_plus_patty(self):
        b = calculate_line_item(BURGER_BASE, [], [_cheese_ao(), _patty_ao()], 1)
        assert b.unit_price_cents == BURGER_BASE + AO_CHEESE + AO_PATTY  # 11000

    def test_latte_plus_soy_milk(self):
        b = calculate_line_item(LATTE_BASE, [], [_soy_milk_ao()], 1)
        assert b.unit_price_cents == LATTE_BASE + AO_SOY_MILK  # 5500

    def test_latte_full_cream_is_base_price(self):
        b = calculate_line_item(LATTE_BASE, [], [_full_cream_ao()], 1)
        assert b.unit_price_cents == LATTE_BASE   # 4500 — no charge

    def test_two_burgers_with_extra_cheese(self):
        b = calculate_line_item(BURGER_BASE, [], [_cheese_ao()], 2)
        assert b.unit_price_cents == BURGER_BASE + AO_CHEESE
        assert b.line_total_cents == (BURGER_BASE + AO_CHEESE) * 2


# ════════════════════════════════════════════════════════════════════════════════
# add_to_cart with add-ons
# ════════════════════════════════════════════════════════════════════════════════

class TestAddToCartWithAddOns:
    """Verify that add_to_cart stores add-ons and prices them correctly."""

    def test_burger_with_extra_cheese_priced_correctly(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao()],
        )
        cart = state_machine.get_cart(sess)
        assert len(cart) == 1
        item = cart[0]
        assert item["add_ons"] == [_cheese_ao()]
        assert item["base_price_cents"] == BURGER_BASE
        assert item["add_on_total_cents"] == AO_CHEESE
        assert item["unit_price_cents"] == BURGER_BASE + AO_CHEESE
        assert item["line_total_cents"] == BURGER_BASE + AO_CHEESE
        assert item["price_cents"] == BURGER_BASE + AO_CHEESE   # legacy compat field

    def test_burger_with_extra_patty_priced_correctly(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_patty_ao()],
        )
        cart = state_machine.get_cart(sess)
        assert cart[0]["unit_price_cents"] == BURGER_BASE + AO_PATTY

    def test_burger_with_cheese_and_patty(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao(), _patty_ao()],
        )
        cart = state_machine.get_cart(sess)
        item = cart[0]
        assert item["unit_price_cents"] == BURGER_BASE + AO_CHEESE + AO_PATTY
        assert item["line_total_cents"] == BURGER_BASE + AO_CHEESE + AO_PATTY

    def test_total_cents_helper_includes_addons(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao()],
        )
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_CHEESE


# ════════════════════════════════════════════════════════════════════════════════
# remove_addon_from_cart_item
# ════════════════════════════════════════════════════════════════════════════════

class TestRemoveAddonFromCartItem:

    def _burger_sess_with_cheese(self) -> FakeSession:
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao()],
        )
        return sess

    def _burger_sess_with_cheese_and_patty(self) -> FakeSession:
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao(), _patty_ao()],
        )
        return sess

    # ── Scenario 1: Burger + Extra Cheese → Remove Extra Cheese ──────────────

    def test_remove_extra_cheese_from_burger(self):
        sess = self._burger_sess_with_cheese()
        cart, found = state_machine.remove_addon_from_cart_item(
            sess, "Classic Smash Burger", "Extra Cheese"
        )
        assert found
        assert len(cart) == 1
        item = cart[0]
        assert item["add_ons"] == []
        assert item["unit_price_cents"] == BURGER_BASE
        assert item["line_total_cents"] == BURGER_BASE
        assert item["add_on_total_cents"] == 0

    def test_remove_cheese_restores_base_price(self):
        sess = self._burger_sess_with_cheese()
        state_machine.remove_addon_from_cart_item(sess, "Classic Smash Burger", "Extra Cheese")
        assert state_machine.cart_total_cents(sess) == BURGER_BASE

    # ── Scenario 5: Burger + Cheese + Patty → Remove Cheese ──────────────────

    def test_remove_cheese_leaves_patty(self):
        sess = self._burger_sess_with_cheese_and_patty()
        cart, found = state_machine.remove_addon_from_cart_item(
            sess, "Classic Smash Burger", "Extra Cheese"
        )
        assert found
        item = cart[0]
        assert len(item["add_ons"]) == 1
        assert item["add_ons"][0]["name"] == "Extra Patty"
        assert item["unit_price_cents"] == BURGER_BASE + AO_PATTY
        assert item["line_total_cents"] == BURGER_BASE + AO_PATTY

    # ── Scenario 6: Burger + Patty + Cheese → Remove Patty ───────────────────

    def test_remove_patty_leaves_cheese(self):
        sess = self._burger_sess_with_cheese_and_patty()
        cart, found = state_machine.remove_addon_from_cart_item(
            sess, "Classic Smash Burger", "Extra Patty"
        )
        assert found
        item = cart[0]
        assert len(item["add_ons"]) == 1
        assert item["add_ons"][0]["name"] == "Extra Cheese"
        assert item["unit_price_cents"] == BURGER_BASE + AO_CHEESE

    def test_returns_not_found_when_addon_missing(self):
        sess = self._burger_sess_with_cheese()
        _, found = state_machine.remove_addon_from_cart_item(
            sess, "Classic Smash Burger", "Extra Patty"
        )
        assert not found

    def test_returns_not_found_when_no_item_matches(self):
        sess = self._burger_sess_with_cheese()
        _, found = state_machine.remove_addon_from_cart_item(
            sess, "Margherita Pizza", "Extra Cheese"
        )
        assert not found

    def test_fuzzy_item_name_match(self):
        """'burger' (partial) must match 'Classic Smash Burger'."""
        sess = self._burger_sess_with_cheese()
        _, found = state_machine.remove_addon_from_cart_item(sess, "burger", "Extra Cheese")
        assert found


# ════════════════════════════════════════════════════════════════════════════════
# add_addon_to_cart_item
# ════════════════════════════════════════════════════════════════════════════════

class TestAddAddonToCartItem:

    def _plain_burger_sess(self) -> FakeSession:
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
        )
        return sess

    # ── Scenario 4: Coffee → Add Soy Milk → Remove Soy Milk ──────────────────

    def test_add_addon_prices_correctly(self):
        sess = self._plain_burger_sess()
        cart, found = state_machine.add_addon_to_cart_item(
            sess, "Classic Smash Burger", _cheese_ao()
        )
        assert found
        item = cart[0]
        assert len(item["add_ons"]) == 1
        assert item["unit_price_cents"] == BURGER_BASE + AO_CHEESE
        assert item["line_total_cents"] == BURGER_BASE + AO_CHEESE

    def test_add_addon_no_duplicate(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao()],
        )
        # Adding same add-on again is a no-op
        cart, found = state_machine.add_addon_to_cart_item(
            sess, "Classic Smash Burger", _cheese_ao()
        )
        assert found
        assert len(cart[0]["add_ons"]) == 1   # still just one

    def test_returns_not_found_for_unknown_item(self):
        sess = self._plain_burger_sess()
        _, found = state_machine.add_addon_to_cart_item(
            sess, "Nonexistent Pizza", _cheese_ao()
        )
        assert not found


# ════════════════════════════════════════════════════════════════════════════════
# _detect_addon_removal
# ════════════════════════════════════════════════════════════════════════════════

class TestDetectAddonRemoval:
    """Unit-test the DET add-on removal detector (Sub-case B2)."""

    def _cart_burger_with_cheese(self) -> list[dict]:
        return [{
            "menu_item_id": BURGER_ID,
            "name": "Classic Smash Burger",
            "price_cents": BURGER_BASE + AO_CHEESE,
            "base_price_cents": BURGER_BASE,
            "unit_price_cents": BURGER_BASE + AO_CHEESE,
            "quantity": 1,
            "line_total_cents": BURGER_BASE + AO_CHEESE,
            "add_ons": [_cheese_ao()],
            "selected_options": [],
            "special_instructions": None,
        }]

    def _cart_latte_with_soy(self) -> list[dict]:
        return [{
            "menu_item_id": LATTE_ID,
            "name": "Ice Coffee",
            "price_cents": LATTE_BASE + AO_SOY_MILK,
            "base_price_cents": LATTE_BASE,
            "unit_price_cents": LATTE_BASE + AO_SOY_MILK,
            "quantity": 1,
            "line_total_cents": LATTE_BASE + AO_SOY_MILK,
            "add_ons": [_soy_milk_ao()],
            "selected_options": [],
            "special_instructions": None,
        }]

    @pytest.mark.parametrize("msg", [
        "Remove the extra cheese",
        "Remove extra cheese",
        "Take off the extra cheese",
        "Take out extra cheese",
    ])
    def test_detects_simple_addon_removal(self, msg):
        result = _detect_addon_removal(msg, self._cart_burger_with_cheese(), BURGER_ADDONS_MAP)
        assert result is not None, f"Should detect add-on removal in: {msg!r}"
        ao_name, item = result
        assert "cheese" in ao_name.lower()
        assert "burger" in item["name"].lower()

    def test_detects_soy_milk_removal(self):
        result = _detect_addon_removal(
            "Remove the soy milk", self._cart_latte_with_soy(), BURGER_ADDONS_MAP
        )
        assert result is not None
        ao_name, item = result
        assert "soy" in ao_name.lower()

    def test_returns_none_when_no_cart(self):
        result = _detect_addon_removal("Remove the extra cheese", [], BURGER_ADDONS_MAP)
        assert result is None

    def test_returns_none_when_addon_not_in_cart(self):
        """Cart has burger with NO add-ons → nothing to detect."""
        plain_cart = [{
            "menu_item_id": BURGER_ID,
            "name": "Classic Smash Burger",
            "price_cents": BURGER_BASE,
            "base_price_cents": BURGER_BASE,
            "unit_price_cents": BURGER_BASE,
            "quantity": 1,
            "line_total_cents": BURGER_BASE,
            "add_ons": [],
            "selected_options": [],
            "special_instructions": None,
        }]
        result = _detect_addon_removal("Remove the extra cheese", plain_cart, BURGER_ADDONS_MAP)
        assert result is None

    def test_returns_none_for_compound_message(self):
        """'Remove extra cheese and add extra patty' → None (LLM handles atomically)."""
        result = _detect_addon_removal(
            "Remove extra cheese and add extra patty",
            self._cart_burger_with_cheese(),
            BURGER_ADDONS_MAP,
        )
        assert result is None, "Compound remove+add must fall through to LLM"

    def test_returns_none_for_compound_message_variant(self):
        result = _detect_addon_removal(
            "Remove the extra cheese and instead add extra patty",
            self._cart_burger_with_cheese(),
            BURGER_ADDONS_MAP,
        )
        assert result is None


# ════════════════════════════════════════════════════════════════════════════════
# _is_reference_not_target — BUG 3 guard
# ════════════════════════════════════════════════════════════════════════════════

class TestIsReferenceNotTarget:

    @pytest.mark.parametrize("msg, item_name, expected", [
        # BUG 3 pattern: item is the PARENT, not the target of removal
        (
            "can you remove the extra cheese on the classic smash burger",
            "Classic Smash Burger",
            True,
        ),
        (
            "remove extra cheese from the classic smash burger",
            "Classic Smash Burger",
            True,
        ),
        (
            "take off extra patty from the classic smash burger",
            "Classic Smash Burger",
            True,
        ),
        # Normal removal: item IS the target
        (
            "remove the classic smash burger",
            "Classic Smash Burger",
            False,
        ),
        (
            "take out the burger",
            "Classic Smash Burger",
            False,
        ),
        (
            "delete my classic smash burger",
            "Classic Smash Burger",
            False,
        ),
    ])
    def test_reference_detection(self, msg, item_name, expected):
        result = _is_reference_not_target(item_name, msg.lower())
        assert result == expected, (
            f"msg={msg!r}, item={item_name!r}: expected {expected}, got {result}"
        )


# ════════════════════════════════════════════════════════════════════════════════
# End-to-end cart conversations (no LLM)
# ════════════════════════════════════════════════════════════════════════════════

class TestCartConversationPricing:
    """
    Simulate the DET-path cart operations that replace the LLM for these scenarios.
    Verify totals after every change.
    """

    # ── Scenario 1: Burger + Extra Cheese → Remove Extra Cheese ──────────────

    def test_scenario1_remove_cheese_restores_base_price(self):
        sess = FakeSession()
        # Step 1: add burger with extra cheese
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao()],
        )
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_CHEESE

        # Step 2: remove extra cheese
        state_machine.remove_addon_from_cart_item(sess, "Classic Smash Burger", "Extra Cheese")
        assert state_machine.cart_total_cents(sess) == BURGER_BASE
        cart = state_machine.get_cart(sess)
        assert cart[0]["add_ons"] == []

    # ── Scenario 2: Burger + Extra Cheese → Replace with Extra Patty ─────────
    # (replace_item removes the burger and re-adds it; we simulate the net result)

    def test_scenario2_replace_cheese_with_patty(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao()],
        )
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_CHEESE

        # Simulate replace_item: remove burger, re-add with extra patty
        state_machine.remove_from_cart(sess, "Classic Smash Burger")
        assert state_machine.get_cart(sess) == []

        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_patty_ao()],
        )
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_PATTY
        cart = state_machine.get_cart(sess)
        assert cart[0]["add_ons"][0]["name"] == "Extra Patty"

    # ── Scenario 3: Latte + Soy Milk → Change to Full Cream ──────────────────

    def test_scenario3_change_soy_to_full_cream(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, LATTE_ID, "Ice Coffee", LATTE_BASE, 1,
            add_ons=[_soy_milk_ao()],
        )
        assert state_machine.cart_total_cents(sess) == LATTE_BASE + AO_SOY_MILK

        # Change to full cream: remove soy, add full cream (both are add-ons)
        state_machine.remove_addon_from_cart_item(sess, "Ice Coffee", "Soy Milk")
        state_machine.add_addon_to_cart_item(sess, "Ice Coffee", _full_cream_ao())

        cart = state_machine.get_cart(sess)
        item = cart[0]
        assert item["add_ons"][0]["name"] == "Full Cream"
        assert item["unit_price_cents"] == LATTE_BASE   # full cream = no charge
        assert state_machine.cart_total_cents(sess) == LATTE_BASE

    # ── Scenario 4: Coffee → Add Soy Milk → Remove Soy Milk ──────────────────

    def test_scenario4_add_then_remove_soy_milk(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, COFFEE_ID, "Ice Coffee", COFFEE_BASE, 1,
        )
        assert state_machine.cart_total_cents(sess) == COFFEE_BASE

        state_machine.add_addon_to_cart_item(sess, "Ice Coffee", _soy_milk_ao())
        assert state_machine.cart_total_cents(sess) == COFFEE_BASE + AO_SOY_MILK

        state_machine.remove_addon_from_cart_item(sess, "Ice Coffee", "Soy Milk")
        assert state_machine.cart_total_cents(sess) == COFFEE_BASE
        assert state_machine.get_cart(sess)[0]["add_ons"] == []

    # ── Scenario 5: Burger + Cheese + Patty → Remove Cheese ──────────────────

    def test_scenario5_remove_cheese_from_cheese_and_patty(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao(), _patty_ao()],
        )
        expected_initial = BURGER_BASE + AO_CHEESE + AO_PATTY
        assert state_machine.cart_total_cents(sess) == expected_initial

        state_machine.remove_addon_from_cart_item(sess, "Classic Smash Burger", "Extra Cheese")
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_PATTY
        cart = state_machine.get_cart(sess)
        assert len(cart[0]["add_ons"]) == 1
        assert cart[0]["add_ons"][0]["name"] == "Extra Patty"

    # ── Scenario 6: Burger + Patty + Cheese → Remove Patty ───────────────────

    def test_scenario6_remove_patty_from_patty_and_cheese(self):
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_patty_ao(), _cheese_ao()],
        )
        state_machine.remove_addon_from_cart_item(sess, "Classic Smash Burger", "Extra Patty")
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_CHEESE
        cart = state_machine.get_cart(sess)
        assert len(cart[0]["add_ons"]) == 1
        assert cart[0]["add_ons"][0]["name"] == "Extra Cheese"

    # ── Scenario 7: Verify totals at every step ───────────────────────────────

    def test_scenario7_total_at_every_step(self):
        sess = FakeSession()

        # Step 1: plain burger
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1
        )
        assert state_machine.cart_total_cents(sess) == BURGER_BASE

        # Step 2: add extra cheese
        state_machine.add_addon_to_cart_item(sess, "Classic Smash Burger", _cheese_ao())
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_CHEESE

        # Step 3: add extra patty
        state_machine.add_addon_to_cart_item(sess, "Classic Smash Burger", _patty_ao())
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_CHEESE + AO_PATTY

        # Step 4: remove cheese
        state_machine.remove_addon_from_cart_item(sess, "Classic Smash Burger", "Extra Cheese")
        assert state_machine.cart_total_cents(sess) == BURGER_BASE + AO_PATTY

        # Step 5: remove patty
        state_machine.remove_addon_from_cart_item(sess, "Classic Smash Burger", "Extra Patty")
        assert state_machine.cart_total_cents(sess) == BURGER_BASE

        # Burger still in cart — no cart deletion
        assert len(state_machine.get_cart(sess)) == 1

    # ── BUG 3 regression: parent item must never be deleted ───────────────────

    def test_bug3_parent_item_never_deleted(self):
        """
        Simulates: Burger + Extra Cheese → remove_addon → burger still in cart.
        The parent item must survive add-on removal.
        """
        sess = FakeSession()
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=[_cheese_ao()],
        )
        state_machine.remove_addon_from_cart_item(sess, "Classic Smash Burger", "Extra Cheese")
        cart = state_machine.get_cart(sess)
        assert len(cart) == 1, "Parent item must remain in cart after add-on removal"
        assert cart[0]["name"] == "Classic Smash Burger"
        assert cart[0]["add_ons"] == []

    def test_bug3_reference_guard_prevents_cart_wipe(self):
        """
        Regression for BUG 3: 'remove extra cheese on the classic smash burger'
        must NOT match _is_reference_not_target=False for the burger.
        """
        msg = "can you remove the extra cheese on the classic smash burger and instead add an extra patty"
        assert _is_reference_not_target("Classic Smash Burger", msg.lower()), (
            "Burger should be identified as a REFERENCE in this message, "
            "not as the DET removal target."
        )
