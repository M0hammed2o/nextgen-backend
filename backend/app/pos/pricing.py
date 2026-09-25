"""
POS order pricing — resolves client-submitted menu item / option / add-on IDs
against this business's live catalog and computes totals via the shared
pricing engine. The POS client never submits a price; every cents value in
the resulting order is computed here from data already trusted (loaded from
the DB, scoped to the caller's business_id).
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, NotFoundError
from shared.models.menu import MenuAddOn, MenuItem
from shared.pricing.engine import (
    LineItemBreakdown,
    SelectedAddOn,
    SelectedOption,
    calculate_line_item,
)


@dataclass
class POSAddOnSelection:
    add_on_id: uuid.UUID
    quantity: int = 1


@dataclass
class POSItemInput:
    menu_item_id: uuid.UUID
    quantity: int
    selected_option_ids: dict = field(default_factory=dict)  # {group_id: option_id}
    add_on_selections: list = field(default_factory=list)    # list[POSAddOnSelection]
    removed_ingredient_ids: list = field(default_factory=list)  # list[str]
    special_instructions: str | None = None


@dataclass
class ResolvedPOSItem:
    menu_item_id: uuid.UUID
    name: str
    breakdown: LineItemBreakdown
    selected_options_snapshot: list
    add_ons_snapshot: list
    removed_ingredients_snapshot: list
    special_instructions: str | None
    requires_preparation: bool


def _resolve_selected_options(menu_item: MenuItem, selected_option_ids: dict) -> list[SelectedOption]:
    """
    Look up each {group_id: option_id} pair against menu_item.options_json and
    return the matching SelectedOption list. Raises AppError if a referenced
    group/option doesn't exist or is disabled, or if a required group has no
    selection — this is the check the existing manual-order endpoint skips.
    """
    options_json = menu_item.options_json or {}
    groups = options_json.get("option_groups", [])
    resolved: list[SelectedOption] = []

    for group in groups:
        if not group.get("is_enabled", True):
            continue
        group_id = group["id"]
        chosen_option_id = selected_option_ids.get(group_id)

        if group.get("required") and not chosen_option_id:
            raise AppError(
                "MISSING_REQUIRED_OPTION",
                f"'{group['name']}' is required for {menu_item.name}",
                422,
                details={"group_id": group_id, "menu_item_id": str(menu_item.id)},
            )

        if not chosen_option_id:
            continue

        options = [o for o in group.get("options", []) if o.get("is_enabled", True)]
        match = next((o for o in options if o["id"] == chosen_option_id), None)
        if not match:
            raise AppError(
                "INVALID_OPTION",
                f"Option {chosen_option_id} is not valid for group '{group['name']}'",
                422,
                details={"group_id": group_id, "option_id": chosen_option_id},
            )

        resolved.append(SelectedOption(
            group_id=group_id,
            group_name=group["name"],
            option_id=match["id"],
            option_name=match["name"],
            price_delta_cents=int(match.get("price_delta_cents", 0)),
        ))

    return resolved


def _resolve_removed_ingredients(menu_item: MenuItem, removed_ingredient_ids: list) -> list[dict]:
    """
    Removable ingredients are a plain, zero-priced, business-configured list
    living alongside option_groups in MenuItem.options_json — not a new
    priced-modifier concept, since "no tomato" never changes the price.
    Resolves and validates requested ids against the item's own configured
    list (never a hardcoded ingredient name anywhere in this codebase).
    """
    if not removed_ingredient_ids:
        return []
    available = {
        i["id"]: i["name"]
        for i in (menu_item.options_json or {}).get("removable_ingredients", [])
    }
    resolved = []
    for ing_id in removed_ingredient_ids:
        name = available.get(ing_id)
        if not name:
            raise AppError(
                "INVALID_REMOVED_INGREDIENT",
                f"'{ing_id}' is not a removable ingredient on {menu_item.name}",
                422,
                details={"ingredient_id": ing_id, "menu_item_id": str(menu_item.id)},
            )
        resolved.append({"id": ing_id, "name": name})
    return resolved


async def _resolve_add_ons(
    db: AsyncSession,
    business_id: uuid.UUID,
    menu_item: MenuItem,
    add_on_selections: list[POSAddOnSelection],
) -> list[SelectedAddOn]:
    if not add_on_selections:
        return []

    allowed_ids = {a.id for a in menu_item.add_ons}
    requested_ids = {sel.add_on_id for sel in add_on_selections}
    unknown = requested_ids - allowed_ids
    if unknown:
        raise AppError(
            "INVALID_ADD_ON",
            f"One or more add-ons are not available on {menu_item.name}",
            422,
            details={"add_on_ids": [str(i) for i in unknown]},
        )

    result = await db.execute(
        select(MenuAddOn).where(
            MenuAddOn.id.in_(requested_ids),
            MenuAddOn.business_id == business_id,
            MenuAddOn.is_active.is_(True),
            MenuAddOn.is_deleted.is_(False),
        )
    )
    add_ons_by_id = {a.id: a for a in result.scalars().all()}

    resolved: list[SelectedAddOn] = []
    for sel in add_on_selections:
        add_on = add_ons_by_id.get(sel.add_on_id)
        if not add_on:
            raise AppError(
                "INVALID_ADD_ON",
                "Add-on not found or no longer available",
                422,
                details={"add_on_id": str(sel.add_on_id)},
            )
        qty = max(sel.quantity, add_on.min_qty or 0)
        if qty > add_on.max_qty:
            raise AppError(
                "INVALID_ADD_ON_QUANTITY",
                f"{add_on.name} allows at most {add_on.max_qty} per line",
                422,
            )
        resolved.append(SelectedAddOn(
            add_on_id=str(add_on.id),
            name=add_on.name,
            price_cents=add_on.price_cents,
            quantity=qty,
        ))
    return resolved


async def resolve_pos_items(
    db: AsyncSession,
    business_id: uuid.UUID,
    items: list[POSItemInput],
) -> list[ResolvedPOSItem]:
    """
    Resolve every submitted line item against this business's live menu
    catalog. Raises NotFoundError (404) for any menu_item_id that doesn't
    belong to this business, and AppError (422) for invalid/missing
    option or add-on selections. Never trusts a client-submitted price.
    """
    if not items:
        return []

    menu_item_ids = {item.menu_item_id for item in items}
    result = await db.execute(
        select(MenuItem).where(
            MenuItem.id.in_(menu_item_ids),
            MenuItem.business_id == business_id,
            MenuItem.is_deleted.is_(False),
        )
    )
    menu_items_by_id = {m.id: m for m in result.scalars().all()}

    resolved_items: list[ResolvedPOSItem] = []
    for item in items:
        menu_item = menu_items_by_id.get(item.menu_item_id)
        if not menu_item:
            raise NotFoundError("Menu item", str(item.menu_item_id))
        if not menu_item.is_active:
            raise AppError(
                "MENU_ITEM_INACTIVE",
                f"{menu_item.name} is not currently available",
                422,
                details={"menu_item_id": str(menu_item.id)},
            )

        selected_options = _resolve_selected_options(menu_item, item.selected_option_ids)
        selected_add_ons = await _resolve_add_ons(db, business_id, menu_item, item.add_on_selections)
        removed_ingredients = _resolve_removed_ingredients(menu_item, item.removed_ingredient_ids)

        breakdown = calculate_line_item(
            base_price_cents=menu_item.price_cents,
            selected_options=selected_options,
            add_ons=selected_add_ons,
            quantity=item.quantity,
        )

        resolved_items.append(ResolvedPOSItem(
            menu_item_id=menu_item.id,
            name=menu_item.name,
            breakdown=breakdown,
            selected_options_snapshot=[
                {
                    "group_id": o.group_id,
                    "group_name": o.group_name,
                    "option_id": o.option_id,
                    "option_name": o.option_name,
                    "price_delta_cents": o.price_delta_cents,
                }
                for o in selected_options
            ],
            add_ons_snapshot=[
                {
                    "add_on_id": a.add_on_id,
                    "name": a.name,
                    "price_cents": a.price_cents,
                    "quantity": a.quantity,
                }
                for a in selected_add_ons
            ],
            removed_ingredients_snapshot=removed_ingredients,
            special_instructions=item.special_instructions,
            requires_preparation=menu_item.requires_preparation,
        ))

    return resolved_items
