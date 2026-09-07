"""
Deterministic stock arithmetic — units, recipe resolution, and variance.

Every test here is fixed-input/fixed-output with no mocking, because this is
the arithmetic the client's 98% accuracy target actually rests on. If any of
these are wrong, a variance report will accuse someone of theft over a
rounding bug.

Scenarios 1-5 in TestClientVarianceScenarios are the worked examples agreed
with Ciao Pizza.
"""

import uuid
from decimal import Decimal

import pytest

from backend.app.inventory.consumption import (
    AddOnSelectionSpec,
    LineItemSpec,
    RecipeLineSpec,
    RecipeSpec,
    resolve_line_item,
    resolve_order,
)
from backend.app.inventory.units import (
    UnitError,
    cost_per_canonical_micro_cents,
    format_quantity,
    from_milli,
    pack_to_milli,
    to_canonical,
    to_milli,
    value_cents,
)
from backend.app.inventory.variance import (
    IngredientVariance,
    basis_points,
    format_basis_points,
    value_weighted_accuracy_basis_points,
)
from shared.enums import CanonicalUnit


# ── Fixtures: the Ciao Pizza ingredient set ──────────────────────────────────

DOUGH = uuid.uuid4()
CHEESE = uuid.uuid4()
SAUCE = uuid.uuid4()
CHICKEN = uuid.uuid4()
MUSHROOM = uuid.uuid4()
BOX = uuid.uuid4()

SIZE_SMALL = "opt-small"
SIZE_LARGE = "opt-large"
SIZE_GROUP = "grp-size"
REMOVABLE_MUSHROOM = "rem-mushroom"

EXTRA_CHEESE_ADDON = uuid.uuid4()
LARGE_CHICKEN_PIZZA = uuid.uuid4()


def large_chicken_pizza_recipe() -> RecipeSpec:
    """
    The client's stated recipe, with cheese scoped by size:
        Small -> 100 g,  Large -> 150 g
    """
    return RecipeSpec(
        lines=(
            RecipeLineSpec(DOUGH, to_milli(1, "unit")),
            RecipeLineSpec(SAUCE, to_milli(80, "g")),
            RecipeLineSpec(CHEESE, to_milli(100, "g"), option_id=SIZE_SMALL),
            RecipeLineSpec(CHEESE, to_milli(150, "g"), option_id=SIZE_LARGE),
            RecipeLineSpec(CHICKEN, to_milli(70, "g")),
            RecipeLineSpec(MUSHROOM, to_milli(30, "g"), removable_ref_id=REMOVABLE_MUSHROOM),
            RecipeLineSpec(BOX, to_milli(1, "unit")),
        ),
        yield_qty=1,
    )


def extra_cheese_recipe() -> RecipeSpec:
    return RecipeSpec(lines=(RecipeLineSpec(CHEESE, to_milli(50, "g")),))


def large_pizza(quantity: int = 1, **kwargs) -> LineItemSpec:
    return LineItemSpec(
        menu_item_id=LARGE_CHICKEN_PIZZA,
        quantity=quantity,
        selected_option_ids=kwargs.pop("options", {SIZE_GROUP: SIZE_LARGE}),
        **kwargs,
    )


# ── Units ────────────────────────────────────────────────────────────────────

class TestUnitConversion:
    def test_canonical_storage_is_thousandths(self):
        assert to_milli(150, "g") == 150_000
        assert to_milli(1, "unit") == 1_000
        assert to_milli(1, "ml") == 1_000

    def test_kilograms_convert_to_grams(self):
        assert to_milli(2, "kg") == 2_000_000
        assert to_canonical(to_milli(2, "kg")) == Decimal(2000)

    def test_litres_convert_to_millilitres(self):
        assert to_milli(1, "L") == 1_000_000
        assert to_canonical(to_milli(1, "L")) == Decimal(1000)

    def test_client_scenario_five_two_kilo_bags_is_ten_thousand_grams(self):
        """5 x 2 kg cheese bags must post exactly 10 000 g — not 10, not 10 000 000 g."""
        received = pack_to_milli(5, to_milli(2, "kg"))
        assert received == 10_000_000
        assert to_canonical(received) == Decimal(10_000)
        assert from_milli(received, "kg") == Decimal(10)

    def test_fractional_pack_receipt(self):
        assert pack_to_milli(Decimal("0.5"), to_milli(2, "kg")) == 1_000_000

    def test_decimal_input_is_exact(self):
        assert to_milli(Decimal("2.5"), "kg") == 2_500_000
        assert to_milli("0.125", "kg") == 125_000

    def test_case_and_whitespace_insensitive(self):
        assert to_milli(1, " Kg ") == to_milli(1, "KG") == to_milli(1, "kg")

    def test_unknown_unit_rejected(self):
        with pytest.raises(UnitError):
            to_milli(1, "handful")

    def test_cross_family_conversion_rejected(self):
        """Receiving litres against a gram-measured ingredient must fail loudly."""
        with pytest.raises(UnitError):
            to_milli(2, "L", canonical=CanonicalUnit.GRAM)
        with pytest.raises(UnitError):
            to_milli(2, "kg", canonical=CanonicalUnit.UNIT)

    def test_same_family_conversion_allowed(self):
        assert to_milli(2, "kg", canonical=CanonicalUnit.GRAM) == 2_000_000

    def test_display_formatting(self):
        assert format_quantity(10_000_000, CanonicalUnit.GRAM) == "10 kg"
        assert format_quantity(150_000, CanonicalUnit.GRAM) == "150 g"
        assert format_quantity(72_000, CanonicalUnit.UNIT) == "72"
        assert format_quantity(-150_000, CanonicalUnit.GRAM) == "-150 g"
        assert format_quantity(500_000, CanonicalUnit.MILLILITRE) == "500 ml"


class TestCostArithmetic:
    def test_pack_cost_becomes_per_gram_micro_cents(self):
        """A 2 kg bag at R190 is 9.5 cents per gram."""
        micro = cost_per_canonical_micro_cents(19000, to_milli(2, "kg"))
        assert micro == 9_500_000

    def test_value_of_a_portion(self):
        micro = cost_per_canonical_micro_cents(19000, to_milli(2, "kg"))
        assert value_cents(to_milli(150, "g"), micro) == 1425  # R14.25

    def test_cheap_ingredient_keeps_precision(self):
        """R38/kg tomato base is 3.8c/g; an 80 g portion is R3.04."""
        micro = cost_per_canonical_micro_cents(3800, to_milli(1, "kg"))
        assert micro == 3_800_000
        assert value_cents(to_milli(80, "g"), micro) == 304

    def test_sub_cent_per_gram_ingredient_does_not_truncate_to_zero(self):
        """
        Flour at R12/kg is 1.2c/g. A 5 g dusting is 6c — the case that would
        round to zero if unit cost were held in whole cents per gram.
        """
        micro = cost_per_canonical_micro_cents(1200, to_milli(1, "kg"))
        assert micro == 1_200_000
        assert value_cents(to_milli(5, "g"), micro) == 6

    def test_zero_pack_size_rejected(self):
        with pytest.raises(UnitError):
            cost_per_canonical_micro_cents(1000, 0)


# ── Recipe resolution ────────────────────────────────────────────────────────

class TestRecipeResolution:
    def test_large_pizza_consumes_the_client_stated_recipe(self):
        result = {c.ingredient_id: c.quantity_milli
                  for c in resolve_line_item(large_pizza(), large_chicken_pizza_recipe())}
        assert result[DOUGH] == to_milli(1, "unit")
        assert result[SAUCE] == to_milli(80, "g")
        assert result[CHEESE] == to_milli(150, "g")
        assert result[CHICKEN] == to_milli(70, "g")
        assert result[MUSHROOM] == to_milli(30, "g")
        assert result[BOX] == to_milli(1, "unit")

    def test_size_option_changes_cheese(self):
        """The same MenuItem must consume differently by selected size."""
        small = large_pizza(options={SIZE_GROUP: SIZE_SMALL})
        large = large_pizza(options={SIZE_GROUP: SIZE_LARGE})
        recipe = large_chicken_pizza_recipe()

        small_map = {c.ingredient_id: c.quantity_milli for c in resolve_line_item(small, recipe)}
        large_map = {c.ingredient_id: c.quantity_milli for c in resolve_line_item(large, recipe)}

        assert small_map[CHEESE] == to_milli(100, "g")
        assert large_map[CHEESE] == to_milli(150, "g")
        # Unscoped lines are identical regardless of size.
        assert small_map[DOUGH] == large_map[DOUGH] == to_milli(1, "unit")

    def test_no_size_selected_consumes_no_scoped_line(self):
        """An option-scoped line never fires when its option wasn't chosen."""
        result = {c.ingredient_id: c.quantity_milli
                  for c in resolve_line_item(large_pizza(options={}), large_chicken_pizza_recipe())}
        assert CHEESE not in result
        assert result[DOUGH] == to_milli(1, "unit")

    def test_removal_deducts_nothing_for_that_ingredient(self):
        """'No mushroom' must deduct zero mushroom — not a reduced amount."""
        item = large_pizza(removed_ref_ids=(REMOVABLE_MUSHROOM,))
        result = {c.ingredient_id: c.quantity_milli
                  for c in resolve_line_item(item, large_chicken_pizza_recipe())}
        assert MUSHROOM not in result
        assert result[CHEESE] == to_milli(150, "g")  # everything else unaffected

    def test_add_on_adds_its_own_recipe(self):
        item = large_pizza(add_on_selections=(AddOnSelectionSpec(EXTRA_CHEESE_ADDON, 1),))
        result = {c.ingredient_id: c.quantity_milli for c in resolve_line_item(
            item, large_chicken_pizza_recipe(), {EXTRA_CHEESE_ADDON: extra_cheese_recipe()}
        )}
        # 150 g base + 50 g extra, aggregated into one cheese figure.
        assert result[CHEESE] == to_milli(200, "g")

    def test_add_on_quantity_multiplies(self):
        item = large_pizza(add_on_selections=(AddOnSelectionSpec(EXTRA_CHEESE_ADDON, 3),))
        result = {c.ingredient_id: c.quantity_milli for c in resolve_line_item(
            item, large_chicken_pizza_recipe(), {EXTRA_CHEESE_ADDON: extra_cheese_recipe()}
        )}
        assert result[CHEESE] == to_milli(150 + 150, "g")

    def test_add_on_multiplies_with_line_quantity(self):
        """2 pizzas each with 2x extra cheese = 4 extra portions."""
        item = large_pizza(quantity=2, add_on_selections=(AddOnSelectionSpec(EXTRA_CHEESE_ADDON, 2),))
        result = {c.ingredient_id: c.quantity_milli for c in resolve_line_item(
            item, large_chicken_pizza_recipe(), {EXTRA_CHEESE_ADDON: extra_cheese_recipe()}
        )}
        assert result[CHEESE] == to_milli(2 * 150 + 4 * 50, "g")

    def test_line_quantity_scales_everything(self):
        result = {c.ingredient_id: c.quantity_milli
                  for c in resolve_line_item(large_pizza(quantity=10), large_chicken_pizza_recipe())}
        assert result[DOUGH] == to_milli(10, "unit")
        assert result[CHEESE] == to_milli(1500, "g")

    def test_unknown_add_on_is_ignored_not_crashed(self):
        item = large_pizza(add_on_selections=(AddOnSelectionSpec(uuid.uuid4(), 1),))
        result = {c.ingredient_id: c.quantity_milli
                  for c in resolve_line_item(item, large_chicken_pizza_recipe(), {})}
        assert result[CHEESE] == to_milli(150, "g")

    def test_item_without_recipe_consumes_nothing(self):
        assert resolve_line_item(large_pizza(), None) == []

    def test_recipe_yield_divides(self):
        """A batch recipe making 4 portions consumes a quarter each."""
        batch = RecipeSpec(lines=(RecipeLineSpec(SAUCE, to_milli(1, "kg")),), yield_qty=4)
        result = resolve_line_item(large_pizza(), batch)
        assert result[0].quantity_milli == to_milli(250, "g")


class TestOrderResolution:
    def test_aggregates_across_lines(self):
        """Two separate lines of the same pizza produce one cheese movement."""
        result = resolve_order(
            [large_pizza(quantity=2), large_pizza(quantity=3)],
            {LARGE_CHICKEN_PIZZA: large_chicken_pizza_recipe()},
        )
        assert result.as_map()[CHEESE] == to_milli(5 * 150, "g")
        assert result.as_map()[DOUGH] == to_milli(5, "unit")

    def test_missing_recipe_is_reported_not_swallowed(self):
        result = resolve_order([large_pizza()], {})
        assert result.is_empty
        assert result.menu_items_without_recipe == [LARGE_CHICKEN_PIZZA]

    def test_client_ten_pizza_example(self):
        """The client's stated example: 10 pizzas of the standard recipe."""
        result = resolve_order(
            [large_pizza(quantity=10)], {LARGE_CHICKEN_PIZZA: large_chicken_pizza_recipe()}
        ).as_map()
        assert result[DOUGH] == to_milli(10, "unit")
        assert result[CHEESE] == to_milli(1500, "g")
        assert result[SAUCE] == to_milli(800, "g")
        assert result[CHICKEN] == to_milli(700, "g")
        assert result[BOX] == to_milli(10, "unit")


# ── Variance ─────────────────────────────────────────────────────────────────

class TestBasisPoints:
    def test_two_percent(self):
        assert basis_points(2_000, 100_000) == 200

    def test_negative(self):
        assert basis_points(-1_000, 27_000) == -370  # -3.70%

    def test_zero_denominator_is_none_not_zero(self):
        assert basis_points(500, 0) is None

    def test_formatting(self):
        assert format_basis_points(-370) == "-3.70%"
        assert format_basis_points(None) == "n/a"


class TestClientVarianceScenarios:
    """The five worked examples agreed with Ciao Pizza."""

    def test_scenario_1_dough_balls(self):
        """
        100 opening, 25 sold (1 each), 2 wasted -> expect 73, counted 72.
        Variance -1 unit, which is -3.70% of the 27 units of usage.
        """
        v = IngredientVariance(
            opening_milli=to_milli(100, "unit"),
            theoretical_consumption_milli=to_milli(25, "unit"),
            wastage_milli=to_milli(2, "unit"),
            counted_milli=to_milli(72, "unit"),
        )
        assert v.expected_milli == to_milli(73, "unit")
        assert v.variance_milli == to_milli(-1, "unit")
        # Denominator is consumption INCLUDING wastage: 25 + 2 = 27 units.
        assert basis_points(v.variance_milli, to_milli(27, "unit")) == -370

    def test_scenario_2_cheese(self):
        """10 000 g opening + 5 000 received - 6 000 used - 250 wasted = 8 750; counted 8 600."""
        v = IngredientVariance(
            opening_milli=to_milli(10_000, "g"),
            received_milli=to_milli(5_000, "g"),
            theoretical_consumption_milli=to_milli(40 * 150, "g"),
            wastage_milli=to_milli(250, "g"),
            counted_milli=to_milli(8_600, "g"),
            unit_cost_micro_cents=cost_per_canonical_micro_cents(19000, to_milli(2, "kg")),
        )
        assert v.theoretical_consumption_milli == to_milli(6_000, "g")
        assert v.expected_milli == to_milli(8_750, "g")
        assert v.variance_milli == to_milli(-150, "g")
        assert basis_points(v.variance_milli, to_milli(6_250, "g")) == -240  # -2.40%
        assert v.variance_value_cents == -1425  # R14.25 of cheese missing

    def test_scenario_3_sauce(self):
        v = IngredientVariance(
            opening_milli=to_milli(4_000, "g"),
            theoretical_consumption_milli=to_milli(40 * 80, "g"),
            counted_milli=to_milli(780, "g"),
        )
        assert v.expected_milli == to_milli(800, "g")
        assert v.variance_milli == to_milli(-20, "g")
        assert v.variance_basis_points == -63  # -0.63%
        assert not v.is_flagged  # inside the 2% tolerance

    def test_scenario_4_pizza_boxes_perfect(self):
        v = IngredientVariance(
            opening_milli=to_milli(200, "unit"),
            received_milli=to_milli(100, "unit"),
            theoretical_consumption_milli=to_milli(40, "unit"),
            wastage_milli=to_milli(3, "unit"),
            counted_milli=to_milli(257, "unit"),
        )
        assert v.expected_milli == to_milli(257, "unit")
        assert v.variance_milli == 0
        assert v.variance_basis_points == 0
        assert not v.is_flagged

    def test_scenario_5_chicken_surplus(self):
        """A positive variance — more on the shelf than the books expected."""
        v = IngredientVariance(
            opening_milli=to_milli(3_000, "g"),
            received_milli=to_milli(2_000, "g"),
            theoretical_consumption_milli=to_milli(40 * 70, "g"),
            wastage_milli=to_milli(120, "g"),
            counted_milli=to_milli(2_145, "g"),
        )
        assert v.expected_milli == to_milli(2_080, "g")
        assert v.variance_milli == to_milli(65, "g")
        assert basis_points(v.variance_milli, to_milli(2_920, "g")) == 223  # +2.23%

    def test_dough_ball_variance_fails_a_two_percent_target(self):
        """
        Documented deliberately: one dough ball short is 3.7% of usage, so a
        flat per-SKU 2% target is unrealistic on low-volume discrete items.
        This is exactly why the headline figure is value-weighted.
        """
        v = IngredientVariance(
            opening_milli=to_milli(100, "unit"),
            theoretical_consumption_milli=to_milli(27, "unit"),
            counted_milli=to_milli(72, "unit"),
        )
        assert v.is_flagged


class TestAdjustmentsAndAccuracy:
    def test_adjustment_moves_expected(self):
        v = IngredientVariance(
            opening_milli=to_milli(100, "unit"),
            theoretical_consumption_milli=to_milli(10, "unit"),
            adjustment_milli=to_milli(5, "unit"),
            counted_milli=to_milli(95, "unit"),
        )
        assert v.expected_milli == to_milli(95, "unit")
        assert v.variance_milli == 0

    def test_value_weighted_accuracy_prefers_the_expensive_ingredient(self):
        """
        A big % miss on a cheap item must not dominate a small % miss on an
        expensive one. Cheese: -150 g of R95/kg. Oregano: -50 g of R20/kg.
        """
        cheese_cost = cost_per_canonical_micro_cents(19000, to_milli(2, "kg"))
        oregano_cost = cost_per_canonical_micro_cents(2000, to_milli(1, "kg"))
        cheese = IngredientVariance(
            opening_milli=to_milli(10_000, "g"),
            theoretical_consumption_milli=to_milli(6_000, "g"),
            counted_milli=to_milli(3_850, "g"),
            unit_cost_micro_cents=cheese_cost,
        )
        oregano = IngredientVariance(
            opening_milli=to_milli(200, "g"),
            theoretical_consumption_milli=to_milli(100, "g"),
            counted_milli=to_milli(50, "g"),
            unit_cost_micro_cents=oregano_cost,
        )
        assert cheese.variance_milli == to_milli(-150, "g")
        assert oregano.variance_milli == to_milli(-50, "g")
        # Oregano is 50% off by quantity but trivial by value; cheese is only
        # 2.5% off but dominates the rand exposure.
        assert oregano.variance_basis_points == -5_000
        assert cheese.variance_basis_points == -250

        bp = value_weighted_accuracy_basis_points([cheese, oregano])
        assert bp is not None
        # Value weighting keeps the headline near cheese's 2.5%, instead of
        # letting the trivial oregano miss drag it toward ~75%.
        assert bp > 9_700
        assert bp == 9_733

    def test_accuracy_is_none_when_nothing_was_consumed(self):
        assert value_weighted_accuracy_basis_points([IngredientVariance()]) is None

    def test_perfect_period_is_ten_thousand_basis_points(self):
        v = IngredientVariance(
            opening_milli=to_milli(100, "unit"),
            theoretical_consumption_milli=to_milli(10, "unit"),
            counted_milli=to_milli(90, "unit"),
            unit_cost_micro_cents=cost_per_canonical_micro_cents(500, to_milli(1, "unit")),
        )
        assert v.variance_milli == 0
        assert value_weighted_accuracy_basis_points([v]) == 10_000
