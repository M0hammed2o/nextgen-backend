"""
Unit tests for the unified pricing engine (shared/pricing/engine.py).

All tests are pure functions — no database, no mocks required.
"""
import pytest

from shared.pricing.engine import (
    SelectedOption,
    SelectedAddOn,
    calculate_unit_price,
    calculate_line_item,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def opt(delta: int) -> SelectedOption:
    return SelectedOption(
        group_id="grp", group_name="Group", option_id="opt", option_name="Option",
        price_delta_cents=delta,
    )


def ao(price: int, qty: int = 1) -> SelectedAddOn:
    return SelectedAddOn(add_on_id="ao", name="Add-on", price_cents=price, quantity=qty)


def opt_dict(delta: int) -> dict:
    return {"group_id": "g", "group_name": "G", "option_id": "o", "option_name": "O",
            "price_delta_cents": delta}


def ao_dict(price: int, qty: int = 1) -> dict:
    return {"add_on_id": "a", "name": "A", "price_cents": price, "quantity": qty}


# ════════════════════════════════════════════════════════════════════════════════
# calculate_unit_price
# ════════════════════════════════════════════════════════════════════════════════

class TestCalculateUnitPrice:

    def test_no_adjustments_returns_base(self):
        assert calculate_unit_price(7500, [], []) == 7500

    def test_positive_option_delta(self):
        assert calculate_unit_price(4500, [opt(1000)], []) == 5500

    def test_negative_option_delta(self):
        assert calculate_unit_price(5000, [opt(-500)], []) == 4500

    def test_zero_option_delta_is_noop(self):
        assert calculate_unit_price(7500, [opt(0)], []) == 7500

    def test_single_add_on(self):
        assert calculate_unit_price(7500, [], [ao(1000)]) == 8500

    def test_add_on_with_quantity(self):
        assert calculate_unit_price(7500, [], [ao(1000, 2)]) == 9500

    def test_option_plus_add_on(self):
        # base=4500, option=+1000, add-on=+1500 → 7000
        assert calculate_unit_price(4500, [opt(1000)], [ao(1500)]) == 7000

    def test_multiple_options_and_add_ons(self):
        # base=7500, option=-500, option=+200, add-on=+1000, add-on=+500×2
        # = 7500 + (-300) + (1000 + 1000) = 9200
        result = calculate_unit_price(7500, [opt(-500), opt(200)], [ao(1000), ao(500, 2)])
        assert result == 9200

    def test_never_negative(self):
        # Large negative delta should clamp to 0
        assert calculate_unit_price(1000, [opt(-5000)], []) == 0

    def test_accepts_dicts_as_well_as_dataclasses(self):
        # Both dict and dataclass inputs must produce identical results
        r1 = calculate_unit_price(4500, [opt(1000)], [ao(1500)])
        r2 = calculate_unit_price(4500, [opt_dict(1000)], [ao_dict(1500)])
        assert r1 == r2 == 7000


# ════════════════════════════════════════════════════════════════════════════════
# calculate_line_item
# ════════════════════════════════════════════════════════════════════════════════

class TestCalculateLineItem:

    def test_base_only_single_qty(self):
        b = calculate_line_item(7500, [], [], 1)
        assert b.base_price_cents == 7500
        assert b.option_adjustment_cents == 0
        assert b.add_on_total_cents == 0
        assert b.unit_price_cents == 7500
        assert b.quantity == 1
        assert b.line_total_cents == 7500

    def test_base_only_multi_qty(self):
        b = calculate_line_item(7500, [], [], 2)
        assert b.unit_price_cents == 7500
        assert b.line_total_cents == 15000

    def test_option_adjustment_stored_separately(self):
        b = calculate_line_item(4500, [opt(1000)], [], 1)
        assert b.base_price_cents == 4500
        assert b.option_adjustment_cents == 1000
        assert b.add_on_total_cents == 0
        assert b.unit_price_cents == 5500
        assert b.line_total_cents == 5500

    def test_add_on_total_stored_separately(self):
        b = calculate_line_item(7500, [], [ao(1000)], 1)
        assert b.base_price_cents == 7500
        assert b.option_adjustment_cents == 0
        assert b.add_on_total_cents == 1000
        assert b.unit_price_cents == 8500
        assert b.line_total_cents == 8500

    def test_add_on_with_quantity_total(self):
        # 2× Extra Cheese at R10 = R20
        b = calculate_line_item(7500, [], [ao(1000, 2)], 1)
        assert b.add_on_total_cents == 2000
        assert b.unit_price_cents == 9500

    def test_option_and_add_on_combined(self):
        # Latte (4500) + Soy Milk (+1000) + Extra Shot (+1500) = 7000
        b = calculate_line_item(4500, [opt(1000)], [ao(1500)], 1)
        assert b.unit_price_cents == 7000
        assert b.line_total_cents == 7000

    def test_quantity_multiplies_unit_price(self):
        # 2× Burger (7500) + Extra Cheese (1000) = unit=8500, total=17000
        b = calculate_line_item(7500, [], [ao(1000)], 2)
        assert b.unit_price_cents == 8500
        assert b.line_total_cents == 17000

    def test_negative_delta_reduces_price(self):
        b = calculate_line_item(5000, [opt(-500)], [], 1)
        assert b.option_adjustment_cents == -500
        assert b.unit_price_cents == 4500

    def test_clamps_unit_price_to_zero(self):
        b = calculate_line_item(1000, [opt(-9999)], [], 1)
        assert b.unit_price_cents == 0
        assert b.line_total_cents == 0

    def test_multiple_add_ons_summed(self):
        # Extra Cheese 1000 + Extra Patty 2500 = 3500 in add-ons
        b = calculate_line_item(7500, [], [ao(1000), ao(2500)], 1)
        assert b.add_on_total_cents == 3500
        assert b.unit_price_cents == 11000

    def test_dict_inputs_equivalent(self):
        b1 = calculate_line_item(4500, [opt(1000)], [ao(1500)], 2)
        b2 = calculate_line_item(4500, [opt_dict(1000)], [ao_dict(1500)], 2)
        assert b1.unit_price_cents == b2.unit_price_cents
        assert b1.line_total_cents == b2.line_total_cents


# ════════════════════════════════════════════════════════════════════════════════
# Real-world scenario calculations
# ════════════════════════════════════════════════════════════════════════════════

class TestRealWorldScenarios:

    def test_classic_smash_burger_extra_cheese_extra_patty(self):
        """R75 burger + R10 cheese + R25 patty = R110 per unit."""
        b = calculate_line_item(
            7500,
            [],
            [ao(1000), ao(2500)],  # Extra Cheese, Extra Patty
            1,
        )
        assert b.unit_price_cents == 11000
        assert b.line_total_cents == 11000

    def test_latte_oat_milk(self):
        """Latte R45 + Oat Milk +R10 = R55."""
        b = calculate_line_item(4500, [opt(1000)], [], 1)
        assert b.unit_price_cents == 5500

    def test_latte_almond_milk_plus_extra_shot(self):
        """Latte R45 + Almond Milk +R15 + Extra Shot +R15 = R75."""
        b = calculate_line_item(4500, [opt(1500)], [ao(1500)], 1)
        assert b.unit_price_cents == 7500

    def test_two_burgers_with_extra_cheese(self):
        """2× (R75 + R10 cheese) = 2× R85 = R170."""
        b = calculate_line_item(7500, [], [ao(1000)], 2)
        assert b.unit_price_cents == 8500
        assert b.line_total_cents == 17000
