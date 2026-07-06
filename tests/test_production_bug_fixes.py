"""
Production bug fix regression tests — 2026-07-06

Covers the four production issues fixed in this session:

  BUG 1 — Paid add-ons treated as free modifiers
           Root cause: real LLM puts add-on names in special_instructions.
           Fix: _rescue_addons_from_instructions migrates them to add_ons.

  BUG 2 — Compound add-on edit destroys the cart
           Root cause: "remove X and add Y" fell through to LLM which could
           return remove_item and delete the parent item.
           Fix: _detect_compound_addon_edit handles it deterministically.

  BUG 3 — Payment workflow missing method/reference/timestamp
           Fix: Order model + migration + endpoint extended.

  BUG 4 — pe.patch is not a function in manager dashboard
           Fix: patch() method added to ApiClient in api-client.ts.

All tests are pure (no DB, no LLM, no HTTP).
"""
import uuid
import pytest

from backend.app.bot import state_machine
from backend.app.bot.pipeline import (
    _rescue_addons_from_instructions,
    _detect_compound_addon_edit,
)
from shared.pricing.engine import calculate_line_item


# ── Shared fixtures ────────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self):
        self.id = uuid.uuid4()
        self.state = "CONFIRMING_ORDER"
        self.context_json: dict = {}


class FakeItem:
    """Minimal stand-in for a MenuItem with add_ons relationship."""
    def __init__(self, item_id, name, price_cents, add_on_dicts=None):
        self.id = uuid.UUID(item_id)
        self.name = name
        self.price_cents = price_cents
        self.is_active = True
        self.is_deleted = False
        # add_ons as plain dicts (same shape _build_add_ons_map_from_items handles)
        self.add_ons = add_on_dicts or []


def _uid() -> str:
    return str(uuid.uuid4())


BURGER_ID = _uid()
LATTE_ID  = _uid()

AO_CHEESE_ID = _uid()
AO_PATTY_ID  = _uid()
AO_SOY_ID    = _uid()
AO_OAT_ID    = _uid()
AO_BACON_ID  = _uid()

BURGER_BASE = 7500   # R75
LATTE_BASE  = 4500   # R45
AO_CHEESE   = 1000   # R10
AO_PATTY    = 2500   # R25
AO_SOY      = 1000   # R10
AO_OAT      = 1000   # R10
AO_BACON    = 1500   # R15


def _ao(ao_id, name, price) -> dict:
    return {
        "add_on_id": ao_id, "name": name, "price_cents": price,
        "min_qty": 0, "max_qty": 5, "default_qty": 1, "is_active": True, "is_deleted": False,
    }


BURGER_ITEM = FakeItem(BURGER_ID, "Classic Smash Burger", BURGER_BASE, [
    _ao(AO_CHEESE_ID, "Extra Cheese", AO_CHEESE),
    _ao(AO_PATTY_ID,  "Extra Patty",  AO_PATTY),
    _ao(AO_BACON_ID,  "Extra Bacon",  AO_BACON),
])

LATTE_ITEM = FakeItem(LATTE_ID, "Latte", LATTE_BASE, [
    _ao(AO_SOY_ID, "Soy Milk", AO_SOY),
    _ao(AO_OAT_ID, "Oat Milk", AO_OAT),
])

ADD_ONS_MAP = {
    BURGER_ID: [
        {"add_on_id": AO_CHEESE_ID, "name": "Extra Cheese", "price_cents": AO_CHEESE, "min_qty": 0, "max_qty": 5, "default_qty": 1},
        {"add_on_id": AO_PATTY_ID,  "name": "Extra Patty",  "price_cents": AO_PATTY,  "min_qty": 0, "max_qty": 3, "default_qty": 1},
        {"add_on_id": AO_BACON_ID,  "name": "Extra Bacon",  "price_cents": AO_BACON,  "min_qty": 0, "max_qty": 3, "default_qty": 1},
    ],
    LATTE_ID: [
        {"add_on_id": AO_SOY_ID, "name": "Soy Milk", "price_cents": AO_SOY, "min_qty": 0, "max_qty": 1, "default_qty": 1},
        {"add_on_id": AO_OAT_ID, "name": "Oat Milk", "price_cents": AO_OAT, "min_qty": 0, "max_qty": 1, "default_qty": 1},
    ],
}


def _burger_cart_item(add_ons=None) -> dict:
    add_ons = add_ons or []
    breakdown = calculate_line_item(BURGER_BASE, [], add_ons, 1)
    return {
        "menu_item_id": BURGER_ID,
        "name": "Classic Smash Burger",
        "price_cents": breakdown.unit_price_cents,
        "base_price_cents": BURGER_BASE,
        "add_ons": add_ons,
        "add_on_total_cents": breakdown.add_on_total_cents,
        "unit_price_cents": breakdown.unit_price_cents,
        "quantity": 1,
        "line_total_cents": breakdown.line_total_cents,
        "selected_options": [],
        "options": None,
        "special_instructions": None,
    }


def _latte_cart_item(add_ons=None) -> dict:
    add_ons = add_ons or []
    breakdown = calculate_line_item(LATTE_BASE, [], add_ons, 1)
    return {
        "menu_item_id": LATTE_ID,
        "name": "Latte",
        "price_cents": breakdown.unit_price_cents,
        "base_price_cents": LATTE_BASE,
        "add_ons": add_ons,
        "add_on_total_cents": breakdown.add_on_total_cents,
        "unit_price_cents": breakdown.unit_price_cents,
        "quantity": 1,
        "line_total_cents": breakdown.line_total_cents,
        "selected_options": [],
        "options": None,
        "special_instructions": None,
    }


def _cheese_ao(qty=1): return {"add_on_id": AO_CHEESE_ID, "name": "Extra Cheese", "price_cents": AO_CHEESE, "quantity": qty}
def _patty_ao(qty=1):  return {"add_on_id": AO_PATTY_ID,  "name": "Extra Patty",  "price_cents": AO_PATTY,  "quantity": qty}
def _soy_ao(qty=1):    return {"add_on_id": AO_SOY_ID,    "name": "Soy Milk",     "price_cents": AO_SOY,    "quantity": qty}
def _oat_ao(qty=1):    return {"add_on_id": AO_OAT_ID,    "name": "Oat Milk",     "price_cents": AO_OAT,    "quantity": qty}
def _bacon_ao(qty=1):  return {"add_on_id": AO_BACON_ID,  "name": "Extra Bacon",  "price_cents": AO_BACON,  "quantity": qty}


# ══════════════════════════════════════════════════════════════════════════════
# BUG 1 — _rescue_addons_from_instructions
# ══════════════════════════════════════════════════════════════════════════════

class TestRescueAddonsFromInstructions:
    """
    BUG 1 regression: when the real LLM puts a paid add-on name in
    special_instructions instead of add_ons, the rescue step must detect
    and migrate it so the correct price is applied.
    """

    def test_extra_cheese_in_instructions_rescued(self):
        cleaned, rescued = _rescue_addons_from_instructions(
            BURGER_ITEM, "extra cheese", ADD_ONS_MAP, []
        )
        assert len(rescued) == 1
        assert rescued[0]["name"] == "Extra Cheese"
        assert rescued[0]["price_cents"] == AO_CHEESE
        assert cleaned is None  # instructions empty after rescue

    def test_extra_patty_in_instructions_rescued(self):
        cleaned, rescued = _rescue_addons_from_instructions(
            BURGER_ITEM, "extra patty", ADD_ONS_MAP, []
        )
        assert len(rescued) == 1
        assert rescued[0]["name"] == "Extra Patty"
        assert rescued[0]["price_cents"] == AO_PATTY

    def test_soy_milk_rescued_for_latte(self):
        cleaned, rescued = _rescue_addons_from_instructions(
            LATTE_ITEM, "soy milk", ADD_ONS_MAP, []
        )
        assert len(rescued) == 1
        assert rescued[0]["name"] == "Soy Milk"
        assert rescued[0]["price_cents"] == AO_SOY

    def test_free_modifier_not_rescued(self):
        """'no tomato' is not a paid add-on and must not be rescued."""
        cleaned, rescued = _rescue_addons_from_instructions(
            BURGER_ITEM, "no tomato", ADD_ONS_MAP, []
        )
        assert rescued == []
        assert cleaned == "no tomato"

    def test_already_in_addons_not_duplicated(self):
        """If Extra Cheese is already in res_add_ons, do not rescue again."""
        existing = [_cheese_ao()]
        cleaned, rescued = _rescue_addons_from_instructions(
            BURGER_ITEM, "extra cheese", ADD_ONS_MAP, existing
        )
        assert rescued == []

    def test_none_instructions_returns_no_rescued(self):
        cleaned, rescued = _rescue_addons_from_instructions(
            BURGER_ITEM, None, ADD_ONS_MAP, []
        )
        assert rescued == []
        assert cleaned is None

    def test_empty_instructions_returns_no_rescued(self):
        cleaned, rescued = _rescue_addons_from_instructions(
            BURGER_ITEM, "", ADD_ONS_MAP, []
        )
        assert rescued == []

    def test_rescue_leaves_remaining_instructions(self):
        """If instructions say 'extra cheese no tomato', only cheese is rescued."""
        cleaned, rescued = _rescue_addons_from_instructions(
            BURGER_ITEM, "extra cheese no tomato", ADD_ONS_MAP, []
        )
        assert len(rescued) == 1
        assert rescued[0]["name"] == "Extra Cheese"
        assert cleaned is not None
        assert "tomato" in cleaned.lower()

    def test_rescued_addon_applied_to_pricing(self):
        """
        End-to-end: add_to_cart with rescued add-on must yield correct line total.
        This is the core Bug 1 assertion.
        """
        sess = FakeSession()
        rescued = [{"add_on_id": AO_CHEESE_ID, "name": "Extra Cheese", "price_cents": AO_CHEESE, "quantity": 1}]
        state_machine.add_to_cart(
            sess, BURGER_ID, "Classic Smash Burger", BURGER_BASE, 1,
            add_ons=rescued,
            special_instructions=None,
        )
        cart = state_machine.get_cart(sess)
        assert cart[0]["unit_price_cents"] == BURGER_BASE + AO_CHEESE
        assert cart[0]["line_total_cents"] == BURGER_BASE + AO_CHEESE
        assert cart[0]["add_on_total_cents"] == AO_CHEESE


# ══════════════════════════════════════════════════════════════════════════════
# BUG 2 — _detect_compound_addon_edit
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectCompoundAddonEdit:
    """
    BUG 2 regression: compound "remove X and add Y" must be handled
    deterministically rather than falling through to the LLM which could
    return remove_item and delete the parent cart item.
    """

    def _cart_with_cheese(self):
        return [_burger_cart_item(add_ons=[_cheese_ao()])]

    def _cart_with_soy(self):
        return [_latte_cart_item(add_ons=[_soy_ao()])]

    def test_remove_cheese_add_patty(self):
        cart = self._cart_with_cheese()
        result = _detect_compound_addon_edit(
            "remove extra cheese and add extra patty", cart, ADD_ONS_MAP
        )
        assert result is not None
        remove_name, add_ao, target = result
        assert remove_name == "Extra Cheese"
        assert add_ao["name"] == "Extra Patty"
        assert add_ao["price_cents"] == AO_PATTY
        assert target["name"] == "Classic Smash Burger"

    def test_remove_cheese_add_bacon(self):
        cart = self._cart_with_cheese()
        result = _detect_compound_addon_edit(
            "remove extra cheese and add extra bacon", cart, ADD_ONS_MAP
        )
        assert result is not None
        remove_name, add_ao, _ = result
        assert remove_name == "Extra Cheese"
        assert add_ao["name"] == "Extra Bacon"

    def test_swap_soy_for_oat(self):
        cart = self._cart_with_soy()
        result = _detect_compound_addon_edit(
            "swap the soy milk for oat milk", cart, ADD_ONS_MAP
        )
        assert result is not None
        remove_name, add_ao, target = result
        assert remove_name == "Soy Milk"
        assert add_ao["name"] == "Oat Milk"
        assert target["name"] == "Latte"

    def test_replace_cheese_with_patty(self):
        cart = self._cart_with_cheese()
        result = _detect_compound_addon_edit(
            "replace extra cheese with extra patty", cart, ADD_ONS_MAP
        )
        assert result is not None
        remove_name, add_ao, _ = result
        assert remove_name == "Extra Cheese"
        assert add_ao["name"] == "Extra Patty"

    def test_no_match_when_remove_target_not_in_cart(self):
        """Cheese not in cart — should return None."""
        cart = [_burger_cart_item(add_ons=[])]  # no add-ons on burger
        result = _detect_compound_addon_edit(
            "remove extra cheese and add extra patty", cart, ADD_ONS_MAP
        )
        assert result is None

    def test_no_match_for_simple_remove(self):
        """Simple 'remove extra cheese' — not a compound edit."""
        cart = self._cart_with_cheese()
        result = _detect_compound_addon_edit(
            "remove extra cheese", cart, ADD_ONS_MAP
        )
        assert result is None

    def test_no_match_for_item_swap(self):
        """'Change the Sprite to a Coke' is not an add-on compound edit."""
        cart = [_burger_cart_item()]
        result = _detect_compound_addon_edit(
            "change the sprite to a coke", cart, ADD_ONS_MAP
        )
        assert result is None

    def test_compound_edit_preserves_burger_in_cart(self):
        """
        End-to-end: after compound edit, parent item MUST remain in cart.
        This is the core Bug 2 assertion.
        """
        sess = FakeSession()
        state_machine.set_cart(sess, [_burger_cart_item(add_ons=[_cheese_ao()])])

        # Simulate DET compound edit
        cart = state_machine.get_cart(sess)
        burger = cart[0]

        _, removed = state_machine.remove_addon_from_cart_item(
            sess, burger["name"], "Extra Cheese"
        )
        _, added = state_machine.add_addon_to_cart_item(
            sess, burger["name"],
            {"add_on_id": AO_PATTY_ID, "name": "Extra Patty", "price_cents": AO_PATTY, "quantity": 1},
        )

        assert removed
        assert added
        updated = state_machine.get_cart(sess)
        assert len(updated) == 1, "Parent burger must NOT be removed"
        assert updated[0]["name"] == "Classic Smash Burger"
        # Extra Patty added, Extra Cheese removed
        ao_names = [a["name"] for a in updated[0].get("add_ons", [])]
        assert "Extra Patty" in ao_names
        assert "Extra Cheese" not in ao_names
        # Pricing correct: base + patty
        assert updated[0]["unit_price_cents"] == BURGER_BASE + AO_PATTY
        assert updated[0]["line_total_cents"] == BURGER_BASE + AO_PATTY


# ══════════════════════════════════════════════════════════════════════════════
# BUG 3 — Payment model fields (PaymentUpdateRequest validation)
# ══════════════════════════════════════════════════════════════════════════════

class TestPaymentUpdateRequest:
    """
    BUG 3 regression: PaymentUpdateRequest must accept payment_method and
    payment_reference in addition to payment_status.
    """

    def _make_request(self, **kwargs):
        from backend.app.api.v1.routes_orders import PaymentUpdateRequest
        return PaymentUpdateRequest(**kwargs)

    def test_paid_with_cash(self):
        req = self._make_request(payment_status="PAID", payment_method="CASH")
        assert req.payment_status == "PAID"
        assert req.payment_method == "CASH"
        assert req.payment_reference is None

    def test_paid_with_card_and_reference(self):
        req = self._make_request(
            payment_status="PAID",
            payment_method="CARD",
            payment_reference="TXN-ABC-123",
        )
        assert req.payment_method == "CARD"
        assert req.payment_reference == "TXN-ABC-123"

    def test_status_only_backwards_compatible(self):
        """Legacy callers that only send payment_status must still work."""
        req = self._make_request(payment_status="PAID")
        assert req.payment_method is None
        assert req.payment_reference is None

    def test_cash_on_collection(self):
        req = self._make_request(payment_status="CASH_ON_COLLECTION")
        assert req.payment_status == "CASH_ON_COLLECTION"

    def test_invalid_payment_status_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self._make_request(payment_status="INVALID")

    def test_invalid_payment_method_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            self._make_request(payment_status="PAID", payment_method="BITCOIN")

    def test_order_response_has_payment_fields(self):
        """OrderResponse schema must expose the three new payment fields."""
        from backend.app.api.v1.routes_orders import OrderResponse
        fields = OrderResponse.model_fields
        assert "payment_method" in fields
        assert "payment_reference" in fields
        assert "paid_at" in fields


# ══════════════════════════════════════════════════════════════════════════════
# BUG 4 — Manager api-client.ts patch() method
# (verified by code review; no Python test needed — frontend file lives in a
#  separate repo that is not checked out in CI)
# ══════════════════════════════════════════════════════════════════════════════
