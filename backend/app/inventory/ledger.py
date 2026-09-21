"""
The stock ledger: posting movements, and reading stock positions back.

APPEND-ONLY
-----------
Nothing in this module ever UPDATEs or DELETEs a StockMovement. A mistake is
corrected by posting a compensating movement, so the history that explains a
variance can always be reconstructed. This mirrors the one-way close already
enforced on TillSession.

IDEMPOTENCY
-----------
A sale posts one movement per ingredient, so the idempotency key is scoped
per ingredient:

    SALE:<order_id>:<ingredient_id>
    REVERSAL:<order_id>:<ingredient_id>

paired with the partial unique index on (business_id, idempotency_key). That
gives the exact guarantee the POS needs: a double-tapped checkout can create
at most one deduction per ingredient, forever, even if the route-level
idempotency check were somehow bypassed.

TENANT ISOLATION
----------------
Every query in this module filters on a business_id that its caller sourced
from the JWT. No function here accepts a client-supplied business_id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inventory.consumption import (
    AddOnSelectionSpec,
    ConsumptionResult,
    LineItemSpec,
    RecipeLineSpec,
    RecipeSpec,
    resolve_order,
)
from backend.app.inventory.units import value_cents
from shared.enums import StockMovementType, StockSourceType
from shared.models.inventory import Ingredient, StockMovement
from shared.models.recipe import Recipe, RecipeItem

# Movement types that represent stock physically leaving for a reason other
# than a sale. Reported apart from sales consumption in variance.
LOSS_MOVEMENT_TYPES = (
    StockMovementType.WASTAGE.value,
    StockMovementType.STAFF_MEAL.value,
    StockMovementType.COMPLIMENTARY.value,
)


def sale_idempotency_key(order_id: uuid.UUID, ingredient_id: uuid.UUID) -> str:
    return f"SALE:{order_id}:{ingredient_id}"


def reversal_idempotency_key(order_id: uuid.UUID, ingredient_id: uuid.UUID) -> str:
    return f"REVERSAL:{order_id}:{ingredient_id}"


def stocktake_adjustment_idempotency_key(
    session_id: uuid.UUID, ingredient_id: uuid.UUID
) -> str:
    """
    Scoped per (stocktake session, ingredient), same scheme as the sale keys
    above — paired with the same partial unique index on
    (business_id, idempotency_key), a double-submitted approval can post at
    most one adjustment per ingredient for a given stocktake, forever.
    """
    return f"STOCKTAKE_ADJUSTMENT:{session_id}:{ingredient_id}"


# ── Loading recipes ──────────────────────────────────────────────────────────

async def load_recipe_specs(
    db: AsyncSession,
    business_id: uuid.UUID,
    menu_item_ids: list[uuid.UUID],
    add_on_ids: list[uuid.UUID],
) -> tuple[dict[uuid.UUID, RecipeSpec], dict[uuid.UUID, RecipeSpec]]:
    """
    Load active recipes for the given menu items and add-ons, flattened into
    the plain dataclasses that consumption.py resolves against.

    Both queries are scoped to business_id — a recipe from another tenant can
    never be resolved into this tenant's stock movements.
    """
    item_specs: dict[uuid.UUID, RecipeSpec] = {}
    add_on_specs: dict[uuid.UUID, RecipeSpec] = {}

    if not menu_item_ids and not add_on_ids:
        return item_specs, add_on_specs

    stmt = select(Recipe).where(
        Recipe.business_id == business_id,
        Recipe.is_active.is_(True),
    )
    conditions = []
    if menu_item_ids:
        conditions.append(Recipe.menu_item_id.in_(list(set(menu_item_ids))))
    if add_on_ids:
        conditions.append(Recipe.menu_add_on_id.in_(list(set(add_on_ids))))
    if conditions:
        from sqlalchemy import or_

        stmt = stmt.where(or_(*conditions))

    recipes = (await db.execute(stmt)).scalars().unique().all()

    for recipe in recipes:
        spec = RecipeSpec(
            lines=tuple(
                RecipeLineSpec(
                    ingredient_id=line.ingredient_id,
                    quantity_milli=int(line.quantity_milli),
                    option_id=line.option_id,
                    removable_ref_id=line.removable_ref_id,
                )
                for line in sorted(recipe.items or [], key=lambda x: (x.sort_order or 0))
            ),
            yield_qty=int(recipe.yield_qty or 1),
        )
        if recipe.menu_item_id:
            item_specs[recipe.menu_item_id] = spec
        elif recipe.menu_add_on_id:
            add_on_specs[recipe.menu_add_on_id] = spec

    return item_specs, add_on_specs


def _selected_option_ids(selected_options_snapshot) -> dict:
    """{group_id: option_id} from a selected-options snapshot, string-keyed."""
    selected: dict = {}
    for option in selected_options_snapshot or []:
        group_id = option.get("group_id") if isinstance(option, dict) else None
        option_id = option.get("option_id") if isinstance(option, dict) else None
        if group_id and option_id:
            selected[str(group_id)] = str(option_id)
    return selected


def _add_on_selections(add_ons_snapshot) -> tuple:
    """AddOnSelectionSpec tuple from an add-ons snapshot (add_on_id/id + quantity)."""
    add_ons = []
    for add_on in add_ons_snapshot or []:
        if not isinstance(add_on, dict):
            continue
        raw_id = add_on.get("add_on_id") or add_on.get("id")
        if not raw_id:
            continue
        try:
            add_on_uuid = uuid.UUID(str(raw_id))
        except (ValueError, AttributeError, TypeError):
            continue
        add_ons.append(
            AddOnSelectionSpec(add_on_uuid, int(add_on.get("quantity", 1) or 1))
        )
    return tuple(add_ons)


def _removed_ref_ids(removed_ingredients_snapshot) -> tuple:
    """removable_ref_id tuple from a removed-ingredients snapshot."""
    return tuple(
        str(r.get("id"))
        for r in (removed_ingredients_snapshot or [])
        if isinstance(r, dict) and r.get("id")
    )


def build_line_specs(resolved_items) -> list[LineItemSpec]:
    """
    Adapt the POS pricing layer's ResolvedPOSItem list into the stock layer's
    LineItemSpec list.

    Deliberately reads from the SNAPSHOTTED option/add-on/removal data that
    pricing already resolved, rather than re-reading the client request — so
    stock consumption is derived from exactly the same validated facts the
    customer was charged for.
    """
    specs: list[LineItemSpec] = []
    for item in resolved_items:
        specs.append(
            LineItemSpec(
                menu_item_id=item.menu_item_id,
                quantity=int(item.breakdown.quantity),
                selected_option_ids=_selected_option_ids(item.selected_options_snapshot),
                add_on_selections=_add_on_selections(item.add_ons_snapshot),
                removed_ref_ids=_removed_ref_ids(item.removed_ingredients_snapshot),
            )
        )
    return specs


def build_line_specs_from_bot_cart(cart: list[dict]) -> list[LineItemSpec]:
    """
    Adapt the WhatsApp bot's plain-dict cart/OrderItem snapshots into the
    stock layer's LineItemSpec list — the bot equivalent of build_line_specs()
    above, sharing the same option/add-on extraction rules so both call sites
    resolve recipes identically.

    The bot cart has no structured removed-ingredient concept today (a
    WhatsApp customer's "no onion" is captured as free-text
    special_instructions, never as a removable_ref_id — order_creator.py
    never sets removed_ingredients_snapshot on the OrderItem it writes
    either), so removed_ref_ids is always empty here. That is a known
    reporting gap, not a fabrication: nothing is invented, and it will
    resolve identically to a POS sale of the same item with nothing removed.
    """
    specs: list[LineItemSpec] = []
    for item in cart:
        raw_menu_item_id = item.get("menu_item_id")
        if not raw_menu_item_id:
            continue
        try:
            menu_item_id = uuid.UUID(str(raw_menu_item_id))
        except (ValueError, AttributeError, TypeError):
            continue
        specs.append(
            LineItemSpec(
                menu_item_id=menu_item_id,
                quantity=int(item.get("quantity", 1) or 1),
                selected_option_ids=_selected_option_ids(item.get("selected_options")),
                add_on_selections=_add_on_selections(item.get("add_ons")),
                removed_ref_ids=(),
            )
        )
    return specs


# ── Posting movements ────────────────────────────────────────────────────────

async def _ingredient_costs(
    db: AsyncSession, business_id: uuid.UUID, ingredient_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Current unit cost per ingredient, scoped to the tenant."""
    if not ingredient_ids:
        return {}
    rows = (
        await db.execute(
            select(Ingredient.id, Ingredient.cost_per_canonical_micro_cents).where(
                Ingredient.business_id == business_id,
                Ingredient.id.in_(list(set(ingredient_ids))),
            )
        )
    ).all()
    return {row[0]: int(row[1] or 0) for row in rows}


def build_movement(
    *,
    business_id: uuid.UUID,
    ingredient_id: uuid.UUID,
    quantity_milli: int,
    movement_type: str,
    unit_cost_micro_cents: int = 0,
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    created_by_user_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    notes: str | None = None,
    occurred_at: datetime | None = None,
) -> StockMovement:
    """
    Construct (but do not add) one ledger movement, with its rand value
    computed from the unit cost snapshotted at this moment.
    """
    when = occurred_at or datetime.now(timezone.utc)
    return StockMovement(
        business_id=business_id,
        ingredient_id=ingredient_id,
        quantity_milli=int(quantity_milli),
        movement_type=movement_type,
        source_type=source_type,
        source_id=source_id,
        unit_cost_micro_cents=int(unit_cost_micro_cents),
        value_cents=value_cents(int(quantity_milli), int(unit_cost_micro_cents)),
        created_by_user_id=created_by_user_id,
        idempotency_key=idempotency_key,
        notes=notes,
        occurred_at=when,
        created_at=datetime.now(timezone.utc),
    )


async def post_sale_consumption(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    order_id: uuid.UUID,
    resolved_items=None,
    line_specs: list[LineItemSpec] | None = None,
    created_by_user_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
    movement_type: str = StockMovementType.SALE_CONSUMPTION.value,
) -> ConsumptionResult:
    """
    Resolve an order's recipes and stage one negative movement per ingredient.

    Does NOT commit — the caller adds these inside its own transaction so a
    sale and its stock deduction commit together or not at all.

    Callers pass either `resolved_items` (POS's ResolvedPOSItem list, adapted
    via build_line_specs) or a pre-built `line_specs` (e.g. the WhatsApp bot's
    build_line_specs_from_bot_cart output) — exactly one of the two.

    Returns the ConsumptionResult so the caller can surface any menu item
    that had no recipe (a real data gap, not something to swallow silently).
    """
    if line_specs is None:
        line_specs = build_line_specs(resolved_items)

    menu_item_ids = [spec.menu_item_id for spec in line_specs]
    add_on_ids = [
        selection.add_on_id for spec in line_specs for selection in spec.add_on_selections
    ]

    item_recipes, add_on_recipes = await load_recipe_specs(
        db, business_id, menu_item_ids, add_on_ids
    )
    result = resolve_order(line_specs, item_recipes, add_on_recipes)
    if result.is_empty:
        return result

    costs = await _ingredient_costs(
        db, business_id, [line.ingredient_id for line in result.lines]
    )

    for line in result.lines:
        db.add(
            build_movement(
                business_id=business_id,
                ingredient_id=line.ingredient_id,
                quantity_milli=-abs(line.quantity_milli),  # a sale removes stock
                movement_type=movement_type,
                unit_cost_micro_cents=costs.get(line.ingredient_id, 0),
                source_type=StockSourceType.ORDER.value,
                source_id=order_id,
                created_by_user_id=created_by_user_id,
                idempotency_key=sale_idempotency_key(order_id, line.ingredient_id),
                occurred_at=occurred_at,
            )
        )

    return result


async def reverse_sale_consumption(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    order_id: uuid.UUID,
    created_by_user_id: uuid.UUID | None = None,
    notes: str | None = None,
) -> int:
    """
    Put back exactly what a sale took out, by mirroring the original
    movements rather than re-resolving the recipe.

    Mirroring matters: if the recipe changed between the sale and the void,
    re-resolving would return a different quantity and silently invent stock.
    Reading the original ledger rows guarantees the reversal nets to zero.

    Returns how many movements were written. Already-reversed orders return
    0 and write nothing, so a double-tapped void cannot inflate stock.
    """
    originals = (
        (
            await db.execute(
                select(StockMovement).where(
                    StockMovement.business_id == business_id,
                    StockMovement.source_type == StockSourceType.ORDER.value,
                    StockMovement.source_id == order_id,
                    StockMovement.movement_type.in_(
                        (
                            StockMovementType.SALE_CONSUMPTION.value,
                            StockMovementType.REMAKE_CONSUMPTION.value,
                        )
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not originals:
        return 0

    already = {
        row[0]
        for row in (
            await db.execute(
                select(StockMovement.ingredient_id).where(
                    StockMovement.business_id == business_id,
                    StockMovement.source_type == StockSourceType.ORDER.value,
                    StockMovement.source_id == order_id,
                    StockMovement.movement_type == StockMovementType.SALE_REVERSAL.value,
                )
            )
        ).all()
    }

    written = 0
    for original in originals:
        if original.ingredient_id in already:
            continue
        db.add(
            build_movement(
                business_id=business_id,
                ingredient_id=original.ingredient_id,
                quantity_milli=-int(original.quantity_milli),  # negate the original
                movement_type=StockMovementType.SALE_REVERSAL.value,
                unit_cost_micro_cents=int(original.unit_cost_micro_cents or 0),
                source_type=StockSourceType.ORDER.value,
                source_id=order_id,
                created_by_user_id=created_by_user_id,
                idempotency_key=reversal_idempotency_key(order_id, original.ingredient_id),
                notes=notes,
            )
        )
        already.add(original.ingredient_id)
        written += 1

    return written


# ── Reading stock positions ──────────────────────────────────────────────────

async def stock_on_hand(
    db: AsyncSession,
    business_id: uuid.UUID,
    *,
    as_of: datetime | None = None,
    ingredient_ids: list[uuid.UUID] | None = None,
) -> dict[uuid.UUID, int]:
    """
    Net stock per ingredient: the sum of every movement up to `as_of`.

    `as_of` is compared against occurred_at (the business event time), not
    created_at, so a stocktake cut-off excludes sales rung up while staff
    were still counting.
    """
    stmt = (
        select(StockMovement.ingredient_id, func.sum(StockMovement.quantity_milli))
        .where(StockMovement.business_id == business_id)
        .group_by(StockMovement.ingredient_id)
    )
    if as_of is not None:
        stmt = stmt.where(StockMovement.occurred_at <= as_of)
    if ingredient_ids:
        stmt = stmt.where(StockMovement.ingredient_id.in_(list(set(ingredient_ids))))

    return {row[0]: int(row[1] or 0) for row in (await db.execute(stmt)).all()}


async def movement_totals_by_type(
    db: AsyncSession,
    business_id: uuid.UUID,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    ingredient_ids: list[uuid.UUID] | None = None,
) -> dict[tuple[uuid.UUID, str], int]:
    """
    Totals keyed by (ingredient_id, movement_type) over a period — the raw
    material a variance report is built from.

    `since` is exclusive and `until` inclusive, so consecutive stocktake
    periods tile exactly without double-counting the boundary movement.
    """
    stmt = (
        select(
            StockMovement.ingredient_id,
            StockMovement.movement_type,
            func.sum(StockMovement.quantity_milli),
        )
        .where(StockMovement.business_id == business_id)
        .group_by(StockMovement.ingredient_id, StockMovement.movement_type)
    )
    if since is not None:
        stmt = stmt.where(StockMovement.occurred_at > since)
    if until is not None:
        stmt = stmt.where(StockMovement.occurred_at <= until)
    if ingredient_ids:
        stmt = stmt.where(StockMovement.ingredient_id.in_(list(set(ingredient_ids))))

    return {(row[0], row[1]): int(row[2] or 0) for row in (await db.execute(stmt)).all()}
