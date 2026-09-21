"""
Recipe routes — the bill of materials that turns a sold item into ingredient
consumption, plus live food costing.

Food cost is exposed on every read because it doubles as a correctness check
during setup: a recipe entered with the wrong unit (150 kg of cheese instead
of 150 g) shows up instantly as an absurd cost per portion, long before it
corrupts a variance report.
"""

import uuid
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.inventory.units import UnitError, format_quantity, to_milli, value_cents
from shared.enums import CanonicalUnit
from shared.models.business import Business
from shared.models.inventory import Ingredient
from shared.models.menu import MenuAddOn, MenuItem
from shared.models.recipe import Recipe, RecipeItem

router = APIRouter(prefix="/business/recipes", tags=["recipes"])


async def _require_inventory_enabled(db: AsyncSession, business_id: uuid.UUID) -> Business:
    business = (
        await db.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    if not business.inventory_enabled:
        raise AppError(
            "INVENTORY_NOT_ENABLED",
            "The inventory module is not enabled for this business",
            409,
        )
    return business


# ── Schemas ──────────────────────────────────────────────────────────────────

class RecipeItemRequest(BaseModel):
    ingredient_id: uuid.UUID
    quantity: str = Field(description="Quantity per portion, in `unit`")
    unit: str = Field(max_length=32)
    option_group_id: str | None = Field(default=None, max_length=64)
    option_id: str | None = Field(
        default=None,
        max_length=64,
        description="Scope this line to one option (e.g. the Large size). "
        "Omit for a line that always applies.",
    )
    removable_ref_id: str | None = Field(
        default=None,
        max_length=64,
        description="Matches options_json.removable_ingredients[].id so "
        "'no mushroom' skips this line",
    )
    sort_order: int = 0


class RecipeUpsertRequest(BaseModel):
    menu_item_id: uuid.UUID | None = None
    menu_add_on_id: uuid.UUID | None = None
    yield_qty: int = Field(default=1, ge=1)
    notes: str | None = None
    items: list[RecipeItemRequest] = Field(default_factory=list)


class RecipeItemResponse(BaseModel):
    id: uuid.UUID
    ingredient_id: uuid.UUID
    ingredient_name: str
    canonical_unit: str
    unit_label: str | None = None
    quantity_milli: int
    quantity_display: str
    option_group_id: str | None
    option_id: str | None
    removable_ref_id: str | None
    cost_cents: int
    sort_order: int


class RecipeResponse(BaseModel):
    id: uuid.UUID
    menu_item_id: uuid.UUID | None
    menu_add_on_id: uuid.UUID | None
    version: int
    is_active: bool
    yield_qty: int
    notes: str | None
    items: list[RecipeItemResponse]

    # Costing. base_cost covers unscoped lines only; option-scoped lines are
    # listed separately because their cost depends on what the customer picks.
    base_food_cost_cents: int
    max_food_cost_cents: int


def _per_portion_quantity_milli(quantity_milli: int, yield_qty: int) -> int:
    """
    Recipe lines are stored per BATCH (e.g. one dough batch yields 4 pizzas),
    so cost must scale down by yield exactly the way a sale's consumption
    does (backend/app/inventory/consumption.py's _scale at multiplier=1) —
    otherwise a batch recipe's displayed cost is N times what one portion
    actually costs. Decimal, rounded half-up once, same rule as _scale.
    """
    if yield_qty <= 0:
        yield_qty = 1
    exact = Decimal(int(quantity_milli)) / Decimal(int(yield_qty))
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _build_response(recipe: Recipe, ingredients: dict[uuid.UUID, Ingredient]) -> RecipeResponse:
    items: list[RecipeItemResponse] = []
    base_cost = 0
    option_costs: dict[str, int] = {}
    yield_qty = int(recipe.yield_qty or 1)

    for line in sorted(recipe.items or [], key=lambda x: (x.sort_order or 0)):
        ingredient = ingredients.get(line.ingredient_id)
        unit = ingredient.canonical_unit if ingredient else CanonicalUnit.UNIT.value
        label = ingredient.unit_label if ingredient else None
        per_portion_milli = _per_portion_quantity_milli(int(line.quantity_milli), yield_qty)
        cost = value_cents(
            per_portion_milli,
            int(ingredient.cost_per_canonical_micro_cents or 0) if ingredient else 0,
        )
        if line.option_id:
            option_costs[line.option_id] = option_costs.get(line.option_id, 0) + cost
        else:
            base_cost += cost

        items.append(
            RecipeItemResponse(
                id=line.id,
                ingredient_id=line.ingredient_id,
                ingredient_name=ingredient.name if ingredient else "(deleted ingredient)",
                canonical_unit=unit,
                unit_label=label,
                quantity_milli=int(line.quantity_milli),
                quantity_display=format_quantity(
                    int(line.quantity_milli), CanonicalUnit(unit), label
                ),
                option_group_id=line.option_group_id,
                option_id=line.option_id,
                removable_ref_id=line.removable_ref_id,
                cost_cents=cost,
                sort_order=line.sort_order or 0,
            )
        )

    return RecipeResponse(
        id=recipe.id,
        menu_item_id=recipe.menu_item_id,
        menu_add_on_id=recipe.menu_add_on_id,
        version=recipe.version,
        is_active=recipe.is_active,
        yield_qty=recipe.yield_qty,
        notes=recipe.notes,
        items=items,
        base_food_cost_cents=base_cost,
        # Worst case: base plus the most expensive single option choice.
        max_food_cost_cents=base_cost + (max(option_costs.values()) if option_costs else 0),
    )


async def _load_ingredients(
    db: AsyncSession, business_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, Ingredient]:
    if not ids:
        return {}
    rows = (
        (
            await db.execute(
                select(Ingredient).where(
                    Ingredient.business_id == business_id, Ingredient.id.in_(list(set(ids)))
                )
            )
        )
        .scalars()
        .all()
    )
    return {i.id: i for i in rows}


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("", response_model=list[RecipeResponse])
async def list_recipes(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Every active recipe for this business, with costing."""
    await _require_inventory_enabled(db, user.business_id)
    recipes = (
        (
            await db.execute(
                select(Recipe)
                .options(selectinload(Recipe.items))
                .where(Recipe.business_id == user.business_id, Recipe.is_active.is_(True))
            )
        )
        .scalars()
        .unique()
        .all()
    )
    ingredient_ids = [line.ingredient_id for r in recipes for line in (r.items or [])]
    ingredients = await _load_ingredients(db, user.business_id, ingredient_ids)
    return [_build_response(r, ingredients) for r in recipes]


@router.get("/menu-item/{menu_item_id}", response_model=RecipeResponse)
async def get_recipe_for_menu_item(
    menu_item_id: uuid.UUID,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    await _require_inventory_enabled(db, user.business_id)
    recipe = (
        await db.execute(
            select(Recipe)
            .options(selectinload(Recipe.items))
            .where(
                Recipe.business_id == user.business_id,
                Recipe.menu_item_id == menu_item_id,
                Recipe.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if not recipe:
        raise NotFoundError("Recipe for menu item", str(menu_item_id))
    ingredients = await _load_ingredients(
        db, user.business_id, [line.ingredient_id for line in (recipe.items or [])]
    )
    return _build_response(recipe, ingredients)


@router.put("", response_model=RecipeResponse)
async def upsert_recipe(
    body: RecipeUpsertRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Create or replace the active recipe for a menu item or add-on.

    Replacing lines does NOT rewrite history: consumption is snapshotted onto
    the stock ledger at sale time, so yesterday's orders keep the quantities
    they actually consumed even after the recipe changes today. The version
    counter increments purely for traceability.
    """
    await _require_inventory_enabled(db, user.business_id)

    if bool(body.menu_item_id) == bool(body.menu_add_on_id):
        raise AppError(
            "INVALID_RECIPE_PARENT",
            "Provide exactly one of menu_item_id or menu_add_on_id",
            422,
        )

    # Parent must belong to this tenant — never trust the id alone.
    if body.menu_item_id:
        parent = (
            await db.execute(
                select(MenuItem).where(
                    MenuItem.id == body.menu_item_id,
                    MenuItem.business_id == user.business_id,
                )
            )
        ).scalar_one_or_none()
        if not parent:
            raise NotFoundError("Menu item", str(body.menu_item_id))
    else:
        parent = (
            await db.execute(
                select(MenuAddOn).where(
                    MenuAddOn.id == body.menu_add_on_id,
                    MenuAddOn.business_id == user.business_id,
                )
            )
        ).scalar_one_or_none()
        if not parent:
            raise NotFoundError("Menu add-on", str(body.menu_add_on_id))

    ingredients = await _load_ingredients(
        db, user.business_id, [item.ingredient_id for item in body.items]
    )
    unknown = [str(i.ingredient_id) for i in body.items if i.ingredient_id not in ingredients]
    if unknown:
        raise AppError("UNKNOWN_INGREDIENT", f"Unknown ingredient(s): {', '.join(unknown)}", 422)

    existing = (
        await db.execute(
            select(Recipe)
            .options(selectinload(Recipe.items))
            .where(
                Recipe.business_id == user.business_id,
                Recipe.is_active.is_(True),
                (
                    Recipe.menu_item_id == body.menu_item_id
                    if body.menu_item_id
                    else Recipe.menu_add_on_id == body.menu_add_on_id
                ),
            )
        )
    ).scalar_one_or_none()

    if existing:
        recipe = existing
        recipe.version = int(recipe.version or 1) + 1
        recipe.yield_qty = body.yield_qty
        recipe.notes = body.notes
        for line in list(recipe.items or []):
            await db.delete(line)
        await db.flush()
    else:
        recipe = Recipe(
            business_id=user.business_id,
            menu_item_id=body.menu_item_id,
            menu_add_on_id=body.menu_add_on_id,
            yield_qty=body.yield_qty,
            notes=body.notes,
        )
        db.add(recipe)
        await db.flush()

    for item in body.items:
        ingredient = ingredients[item.ingredient_id]
        try:
            quantity_milli = to_milli(
                item.quantity, item.unit, canonical=CanonicalUnit(ingredient.canonical_unit)
            )
        except (UnitError, ValueError) as exc:
            raise AppError(
                "INVALID_UNIT", f"{ingredient.name}: {exc}", 422
            ) from exc
        if quantity_milli <= 0:
            raise AppError(
                "INVALID_QUANTITY",
                f"{ingredient.name}: recipe quantity must be greater than zero",
                422,
            )
        db.add(
            RecipeItem(
                business_id=user.business_id,
                recipe_id=recipe.id,
                ingredient_id=ingredient.id,
                quantity_milli=quantity_milli,
                option_group_id=item.option_group_id,
                option_id=item.option_id,
                removable_ref_id=item.removable_ref_id,
                sort_order=item.sort_order,
            )
        )

    await db.commit()

    refreshed = (
        await db.execute(
            select(Recipe).options(selectinload(Recipe.items)).where(Recipe.id == recipe.id)
        )
    ).scalar_one()
    return _build_response(refreshed, ingredients)


@router.delete("/{recipe_id}", status_code=204)
async def deactivate_recipe(
    recipe_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Deactivate rather than delete, so past ledger rows keep a resolvable
    parent and the recipe that produced them stays inspectable.
    """
    await _require_inventory_enabled(db, user.business_id)
    recipe = (
        await db.execute(
            select(Recipe).where(
                Recipe.id == recipe_id, Recipe.business_id == user.business_id
            )
        )
    ).scalar_one_or_none()
    if not recipe:
        raise NotFoundError("Recipe", str(recipe_id))
    recipe.is_active = False
    await db.commit()
    return None
