"""
Recipe resolution: turning one sold line item into ingredient consumption.

This is the arithmetic heart of the whole inventory subsystem. It is
deliberately written as PURE FUNCTIONS over light dataclasses, with no
database access and no SQLAlchemy import, so that every rule below can be
tested exactly rather than through a mocked session.

The resolution rules, in order:

1. A recipe line scoped to an option (``option_id``) applies only when that
   option was actually selected. This is how one MenuItem consumes 100 g of
   cheese as a Small and 150 g as a Large - sizes are option groups on a
   single item in this platform, not separate items.

2. A recipe line whose ``removable_ref_id`` appears in the order's removed
   ingredient list is skipped entirely. "No mushroom" deducts no mushroom.

3. Each selected add-on contributes its own recipe, multiplied by how many
   of that add-on were chosen. "Extra cheese x2" adds 2 x 50 g.

4. Everything is multiplied by the line item's quantity, and divided by the
   recipe's yield (portions produced per execution; 1 for a pizza).

5. Results are aggregated per ingredient, so an item whose base recipe and
   add-on both use cheese produces one combined cheese figure.

Quantities are integer canonical milli-units throughout (see units.py).
The single division - by yield - is done in Decimal and rounded half-up
once, never in float.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True)
class RecipeLineSpec:
    """One ingredient line of a recipe, flattened for resolution."""

    ingredient_id: uuid.UUID
    quantity_milli: int
    option_id: str | None = None
    removable_ref_id: str | None = None


@dataclass(frozen=True)
class RecipeSpec:
    """A recipe flattened to exactly what resolution needs."""

    lines: tuple[RecipeLineSpec, ...] = ()
    yield_qty: int = 1


@dataclass(frozen=True)
class AddOnSelectionSpec:
    """An add-on chosen on a line item, and how many of it."""

    add_on_id: uuid.UUID
    quantity: int = 1


@dataclass(frozen=True)
class LineItemSpec:
    """One cart/order line, as far as stock consumption is concerned."""

    menu_item_id: uuid.UUID
    quantity: int
    selected_option_ids: dict = field(default_factory=dict)  # {group_id: option_id}
    add_on_selections: tuple[AddOnSelectionSpec, ...] = ()
    removed_ref_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngredientConsumption:
    """How much of one ingredient a resolution consumed (positive)."""

    ingredient_id: uuid.UUID
    quantity_milli: int


@dataclass
class ConsumptionResult:
    """
    Resolved consumption for a whole order.

    ``menu_items_without_recipe`` is intentionally surfaced rather than
    swallowed: an inventory-enabled tenant selling an item that has no
    recipe is a real data gap that quietly destroys variance accuracy, and
    the caller decides whether to warn or block.
    """

    lines: list[IngredientConsumption] = field(default_factory=list)
    menu_items_without_recipe: list[uuid.UUID] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.lines

    def as_map(self) -> dict[uuid.UUID, int]:
        return {line.ingredient_id: line.quantity_milli for line in self.lines}


def _scale(quantity_milli: int, multiplier: int, yield_qty: int) -> int:
    """
    quantity_milli * multiplier / yield_qty, exactly, rounded half-up once.

    Decimal rather than float because this result is posted straight to the
    stock ledger, and a ledger that drifts is worse than no ledger at all.
    """
    if yield_qty <= 0:
        raise ValueError("Recipe yield must be greater than zero")
    exact = (Decimal(int(quantity_milli)) * Decimal(int(multiplier))) / Decimal(int(yield_qty))
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def resolve_line_item(
    item: LineItemSpec,
    item_recipe: RecipeSpec | None,
    add_on_recipes: dict[uuid.UUID, RecipeSpec] | None = None,
) -> list[IngredientConsumption]:
    """
    Resolve one line item into ingredient consumption.

    Returns an empty list when the item has no recipe - the caller decides
    whether that is acceptable (it is recorded on ConsumptionResult).
    """
    add_on_recipes = add_on_recipes or {}
    totals: dict[uuid.UUID, int] = {}
    order: list[uuid.UUID] = []

    def add(ingredient_id: uuid.UUID, amount: int) -> None:
        if amount == 0:
            return
        if ingredient_id not in totals:
            totals[ingredient_id] = 0
            order.append(ingredient_id)
        totals[ingredient_id] += amount

    selected_option_ids = set(str(v) for v in (item.selected_option_ids or {}).values() if v)
    removed = set(str(r) for r in (item.removed_ref_ids or ()))

    # -- Base recipe ------------------------------------------------------
    if item_recipe is not None:
        for line in item_recipe.lines:
            # Rule 2: an explicitly removed ingredient is not consumed.
            if line.removable_ref_id and str(line.removable_ref_id) in removed:
                continue
            # Rule 1: an option-scoped line needs its option selected.
            if line.option_id is not None and str(line.option_id) not in selected_option_ids:
                continue
            add(
                line.ingredient_id,
                _scale(line.quantity_milli, item.quantity, item_recipe.yield_qty),
            )

    # -- Add-on recipes ---------------------------------------------------
    for selection in item.add_on_selections or ():
        add_on_recipe = add_on_recipes.get(selection.add_on_id)
        if add_on_recipe is None:
            continue
        for line in add_on_recipe.lines:
            if line.removable_ref_id and str(line.removable_ref_id) in removed:
                continue
            if line.option_id is not None and str(line.option_id) not in selected_option_ids:
                continue
            # An add-on's quantity multiplies with the line item's quantity:
            # 2 pizzas each with 2x extra cheese consumes 4 portions of it.
            add(
                line.ingredient_id,
                _scale(
                    line.quantity_milli,
                    item.quantity * max(0, selection.quantity),
                    add_on_recipe.yield_qty,
                ),
            )

    return [IngredientConsumption(iid, totals[iid]) for iid in order]


def resolve_order(
    items: list[LineItemSpec],
    item_recipes: dict[uuid.UUID, RecipeSpec],
    add_on_recipes: dict[uuid.UUID, RecipeSpec] | None = None,
) -> ConsumptionResult:
    """
    Resolve every line of an order, aggregating across lines so the ledger
    receives one movement per ingredient rather than one per line item.
    """
    result = ConsumptionResult()
    totals: dict[uuid.UUID, int] = {}
    order: list[uuid.UUID] = []
    missing: list[uuid.UUID] = []

    for item in items:
        recipe = item_recipes.get(item.menu_item_id)
        if recipe is None and item.menu_item_id not in missing:
            missing.append(item.menu_item_id)

        for consumption in resolve_line_item(item, recipe, add_on_recipes):
            if consumption.ingredient_id not in totals:
                totals[consumption.ingredient_id] = 0
                order.append(consumption.ingredient_id)
            totals[consumption.ingredient_id] += consumption.quantity_milli

    result.lines = [IngredientConsumption(i, totals[i]) for i in order]
    result.menu_items_without_recipe = missing
    return result
