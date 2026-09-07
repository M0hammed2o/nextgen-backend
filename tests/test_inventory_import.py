"""
CSV/XLSX import parsing for ingredients and recipes.

Tests the pure parser functions directly — the file-handling and DB layers
above them are thin, and this is where a franchise's spreadsheet either
converts correctly or silently produces a 1000x stock error.
"""

import types
import uuid

from backend.app.api.v1.routes_import import _parse_ingredients, _parse_recipes
from backend.app.inventory.units import to_milli


def rows(*dicts):
    return list(dicts)


# ── Ingredients ──────────────────────────────────────────────────────────────

class TestIngredientImport:
    def test_kilogram_pack_converts_to_grams(self):
        """A 2 kg cheese bag must store 2 000 000 milli-grams, not 2."""
        result = _parse_ingredients(rows(
            {"name": "Cheese", "unit": "kg", "pack_size": "2", "pack_unit": "KG",
             "pack_cost": "190.00"}
        ))
        assert result.errors == []
        row = result.rows[0]
        assert row["canonical_unit"] == "GRAM"
        assert row["purchase_pack_milli"] == to_milli(2, "kg")
        assert row["cost_per_pack_cents"] == 19000
        assert row["cost_per_canonical_micro_cents"] == 9_500_000  # 9.5c/g

    def test_count_method_defaults_sensibly_per_unit(self):
        result = _parse_ingredients(rows(
            {"name": "Cheese", "unit": "kg", "pack_size": "1", "pack_unit": "KG"},
            {"name": "Dough Ball", "unit": "unit", "pack_size": "1", "pack_unit": "UNIT"},
            {"name": "Olive Oil", "unit": "L", "pack_size": "1", "pack_unit": "L"},
        ))
        assert result.errors == []
        methods = {r["name"]: r["count_method"] for r in result.rows}
        assert methods["Cheese"] == "WEIGHT"
        assert methods["Dough Ball"] == "UNIT_COUNT"
        assert methods["Olive Oil"] == "VOLUME"

    def test_explicit_count_method_wins(self):
        result = _parse_ingredients(rows(
            {"name": "Cheese", "unit": "kg", "pack_size": "1", "pack_unit": "KG",
             "count_method": "UNIT_COUNT"}
        ))
        assert result.rows[0]["count_method"] == "UNIT_COUNT"

    def test_missing_unit_is_an_error_never_a_guess(self):
        """A wrong unit is a 1000x error, so it is never assumed."""
        result = _parse_ingredients(rows({"name": "Cheese", "pack_size": "2"}))
        assert result.rows == []
        assert len(result.errors) == 1
        assert "unit" in result.errors[0].message

    def test_unknown_unit_is_rejected(self):
        result = _parse_ingredients(rows({"name": "Flour", "unit": "handfuls"}))
        assert result.rows == []
        assert "unit" in result.errors[0].message

    def test_missing_name_is_an_error(self):
        result = _parse_ingredients(rows({"unit": "kg", "pack_size": "1"}))
        assert result.errors[0].message == "Missing 'name'"

    def test_duplicate_names_within_one_file_are_rejected(self):
        result = _parse_ingredients(rows(
            {"name": "Cheese", "unit": "kg", "pack_size": "1", "pack_unit": "KG"},
            {"name": "cheese", "unit": "kg", "pack_size": "2", "pack_unit": "KG"},
        ))
        assert len(result.rows) == 1
        assert "Duplicate" in result.errors[0].message

    def test_cross_family_pack_unit_is_rejected(self):
        """Litres of a gram-measured ingredient must fail loudly."""
        result = _parse_ingredients(rows(
            {"name": "Cheese", "unit": "kg", "pack_size": "2", "pack_unit": "L"}
        ))
        assert result.rows == []
        assert "pack_size/pack_unit" in result.errors[0].message

    def test_bad_cost_is_rejected(self):
        result = _parse_ingredients(rows(
            {"name": "Cheese", "unit": "kg", "pack_size": "1", "pack_unit": "KG",
             "pack_cost": "about two hundred"}
        ))
        assert result.rows == []
        assert "pack_cost" in result.errors[0].message

    def test_rand_symbol_and_thousands_separator_are_tolerated(self):
        result = _parse_ingredients(rows(
            {"name": "Cheese", "unit": "kg", "pack_size": "1", "pack_unit": "KG",
             "pack_cost": "R1,250.50"}
        ))
        assert result.errors == []
        assert result.rows[0]["cost_per_pack_cents"] == 125050

    def test_error_rows_report_the_spreadsheet_row_number(self):
        """Row 1 is the header, so the first data row must report as row 2."""
        result = _parse_ingredients(rows({"name": "", "unit": "kg"}))
        assert result.errors[0].row == 2

    def test_par_level_converts(self):
        result = _parse_ingredients(rows(
            {"name": "Cheese", "unit": "kg", "pack_size": "2", "pack_unit": "KG",
             "par_level": "5"}
        ))
        assert result.rows[0]["par_level_milli"] == to_milli(5, "kg")


# ── Recipes ──────────────────────────────────────────────────────────────────

def _item(name):
    return types.SimpleNamespace(id=uuid.uuid4(), name=name)


def _ingredient(name, unit="GRAM"):
    return types.SimpleNamespace(id=uuid.uuid4(), name=name, canonical_unit=unit)


def _lookup():
    pizza = _item("Large Chicken Pizza")
    cheese = _ingredient("Cheese")
    dough = _ingredient("Dough Ball", "UNIT")
    return (
        {pizza.name.casefold(): pizza},
        {cheese.name.casefold(): cheese, dough.name.casefold(): dough},
        pizza, cheese, dough,
    )


class TestRecipeImport:
    def test_groups_lines_by_menu_item(self):
        items, ingredients, pizza, cheese, dough = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Large Chicken Pizza", "ingredient": "Cheese",
             "quantity": "150", "unit": "g"},
            {"menu_item": "Large Chicken Pizza", "ingredient": "Dough Ball",
             "quantity": "1", "unit": "unit"},
        ), items, ingredients)
        assert errors == []
        assert len(grouped) == 1
        lines = grouped[str(pizza.id)]
        assert len(lines) == 2
        assert lines[0]["quantity_milli"] == to_milli(150, "g")
        assert lines[1]["quantity_milli"] == to_milli(1, "unit")

    def test_name_matching_is_case_insensitive(self):
        items, ingredients, pizza, _, _ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "large chicken PIZZA", "ingredient": "cheese",
             "quantity": "150", "unit": "g"},
        ), items, ingredients)
        assert errors == []
        assert str(pizza.id) in grouped

    def test_unknown_menu_item_is_an_error_not_a_dropped_line(self):
        """
        A silently dropped line is invisible in the UI but permanently
        understates usage, so it must fail loudly instead.
        """
        items, ingredients, *_ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Garlic Bread", "ingredient": "Cheese",
             "quantity": "50", "unit": "g"},
        ), items, ingredients)
        assert grouped == {}
        assert "No menu item" in errors[0].message

    def test_unknown_ingredient_is_an_error(self):
        items, ingredients, *_ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Large Chicken Pizza", "ingredient": "Truffle Oil",
             "quantity": "5", "unit": "ml"},
        ), items, ingredients)
        assert grouped == {}
        assert "No ingredient" in errors[0].message

    def test_missing_unit_is_rejected(self):
        items, ingredients, *_ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Large Chicken Pizza", "ingredient": "Cheese", "quantity": "150"},
        ), items, ingredients)
        assert grouped == {}
        assert "never assumed" in errors[0].message

    def test_wrong_unit_family_is_rejected(self):
        """Millilitres of a gram-measured cheese must not silently convert."""
        items, ingredients, *_ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Large Chicken Pizza", "ingredient": "Cheese",
             "quantity": "150", "unit": "ml"},
        ), items, ingredients)
        assert grouped == {}
        assert "Cheese" in errors[0].message

    def test_zero_quantity_is_rejected(self):
        items, ingredients, *_ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Large Chicken Pizza", "ingredient": "Cheese",
             "quantity": "0", "unit": "g"},
        ), items, ingredients)
        assert grouped == {}
        assert "greater than zero" in errors[0].message

    def test_option_and_removable_scoping_carry_through(self):
        items, ingredients, pizza, *_ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Large Chicken Pizza", "ingredient": "Cheese",
             "quantity": "150", "unit": "g", "option_id": "opt-large"},
            {"menu_item": "Large Chicken Pizza", "ingredient": "Dough Ball",
             "quantity": "1", "unit": "unit", "removable_id": "rem-dough"},
        ), items, ingredients)
        assert errors == []
        lines = grouped[str(pizza.id)]
        assert lines[0]["option_id"] == "opt-large"
        assert lines[0]["removable_ref_id"] is None
        assert lines[1]["removable_ref_id"] == "rem-dough"

    def test_kilograms_in_a_recipe_convert(self):
        """A batch recipe quoted in kg must not be read as grams."""
        items, ingredients, pizza, *_ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Large Chicken Pizza", "ingredient": "Cheese",
             "quantity": "1.5", "unit": "kg"},
        ), items, ingredients)
        assert errors == []
        assert grouped[str(pizza.id)][0]["quantity_milli"] == to_milli(1500, "g")

    def test_valid_rows_are_kept_while_bad_ones_are_reported(self):
        """
        The parser reports both; the COMMIT endpoint is what refuses the whole
        file, so an operator can see every problem at once in the preview.
        """
        items, ingredients, pizza, *_ = _lookup()
        grouped, errors = _parse_recipes(rows(
            {"menu_item": "Large Chicken Pizza", "ingredient": "Cheese",
             "quantity": "150", "unit": "g"},
            {"menu_item": "Nope", "ingredient": "Cheese", "quantity": "1", "unit": "g"},
        ), items, ingredients)
        assert len(grouped[str(pizza.id)]) == 1
        assert len(errors) == 1
        assert errors[0].row == 3
