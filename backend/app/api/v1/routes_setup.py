"""
Self-service setup: how far a restaurant has got configuring itself, and the
live theoretical-stock view its manager runs the business from.

WHY THIS EXISTS
---------------
A restaurant must be able to go live without a NextGen developer touching
anything. That means the owner needs to see, in their own dashboard, exactly
what is still missing before they can trade — not discover it at the till on
a Friday night.

LANGUAGE
--------
Everything returned here is in restaurant vocabulary. No canonical
milli-units, no movement-type enums, no ledger internals leak into a
manager's screen: quantities come back as pre-formatted display strings in
the ingredient's own noun ("72 portions", "10 kg"), and a negative variance
is called an unexplained difference, never theft. The software cannot know
why stock is missing, and saying otherwise would be both wrong and damaging.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.inventory.ledger import movement_totals_by_type, stock_on_hand
from backend.app.inventory.units import format_quantity, value_cents
from backend.app.inventory.variance import (
    DEFAULT_VARIANCE_ALERT_BASIS_POINTS,
    IngredientVariance,
    format_basis_points,
    value_weighted_accuracy_basis_points,
)
from shared.enums import CanonicalUnit, StockMovementType
from shared.models.business import Business
from shared.models.inventory import Ingredient
from shared.models.menu import MenuItem
from shared.models.recipe import Recipe
from shared.models.stocktake import StocktakeSession
from shared.models.user import BusinessUser

router = APIRouter(prefix="/business/setup", tags=["setup"])

# Which movement types feed which term of the stock equation. Mirrors
# routes_stocktake.py deliberately — one definition of what "used by sales"
# means, so the live view and the stocktake can never disagree.
_RECEIPT_TYPES = {StockMovementType.GOODS_RECEIVED.value}
_SALE_TYPES = {
    StockMovementType.SALE_CONSUMPTION.value,
    StockMovementType.REMAKE_CONSUMPTION.value,
    StockMovementType.SALE_REVERSAL.value,
}
_WASTAGE_TYPES = {
    StockMovementType.WASTAGE.value,
    StockMovementType.STAFF_MEAL.value,
    StockMovementType.COMPLIMENTARY.value,
}
_OPENING_TYPES = {StockMovementType.OPENING_BALANCE.value}


# ── Setup progress ───────────────────────────────────────────────────────────

class SetupStep(BaseModel):
    key: str
    label: str
    description: str
    status: str          # done | partial | todo
    detail: str
    count: int = 0
    total: int = 0
    required_to_trade: bool = True
    href: str | None = None


class SetupProgress(BaseModel):
    business_name: str
    inventory_enabled: bool
    ready_to_trade: bool
    percent_complete: int
    steps: list[SetupStep]


@router.get("/progress", response_model=SetupProgress)
async def get_setup_progress(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    What still needs configuring before this restaurant can trade.

    Deliberately computed from real data rather than a checklist the owner
    ticks off — a "done" here means the records actually exist.
    """
    business = (
        await db.execute(select(Business).where(Business.id == user.business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    async def count(model, *where):
        stmt = select(func.count()).select_from(model).where(
            model.business_id == user.business_id, *where
        )
        return int((await db.execute(stmt)).scalar() or 0)

    menu_items = await count(MenuItem, MenuItem.is_deleted.is_(False))
    active_items = await count(
        MenuItem, MenuItem.is_deleted.is_(False), MenuItem.is_active.is_(True)
    )
    staff = await count(BusinessUser)
    steps: list[SetupStep] = []

    # -- Business details --------------------------------------------------
    has_details = bool(business.name and (business.address or business.phone))
    steps.append(SetupStep(
        key="business",
        label="Business details",
        description="Name, address and telephone — these appear on your till slip.",
        status="done" if has_details else "partial",
        detail="Complete" if has_details else "Add your address and phone number",
        href="/settings",
    ))

    # -- Menu ---------------------------------------------------------------
    steps.append(SetupStep(
        key="menu",
        label="Menu",
        description="The items you sell, their prices and sizes.",
        status="done" if active_items else "todo",
        detail=(
            f"{active_items} item{'s' if active_items != 1 else ''} on sale"
            if active_items else "No menu items yet — add them or upload a spreadsheet"
        ),
        count=active_items,
        href="/menu",
    ))

    # -- Receipt ------------------------------------------------------------
    config = business.receipt_config_json or {}
    has_receipt = bool(config.get("trading_name") or business.name)
    steps.append(SetupStep(
        key="receipt",
        label="Till slip",
        description="What prints on your customer receipt.",
        status="done" if has_receipt and config else "partial",
        detail=(
            "Configured" if config
            else "Using defaults — set your branch, address and footer"
        ),
        href="/settings/receipts",
    ))

    # -- Staff --------------------------------------------------------------
    steps.append(SetupStep(
        key="staff",
        label="Staff",
        description="Who can use the till, and what they are allowed to do.",
        status="done" if staff > 1 else "partial" if staff == 1 else "todo",
        detail=(
            f"{staff} account{'s' if staff != 1 else ''}"
            if staff else "No staff accounts yet"
        ),
        count=staff,
        href="/staff",
    ))

    # -- Printer (cannot be verified from here) -----------------------------
    steps.append(SetupStep(
        key="printer",
        label="Printer test",
        description="Print a test receipt on your own printer.",
        # Honest: no server-side signal can tell us whether a physical
        # printer works. Presented as a manual step rather than guessed.
        status="todo",
        detail="Ring up a test sale and check the slip prints",
        required_to_trade=False,
        href="/settings/receipts",
    ))

    # -- Inventory steps, only when the module is on ------------------------
    if business.inventory_enabled:
        ingredients = await count(
            Ingredient, Ingredient.is_deleted.is_(False), Ingredient.is_active.is_(True)
        )
        steps.append(SetupStep(
            key="ingredients",
            label="Ingredients",
            description="What you stock, in the units you actually count.",
            status="done" if ingredients else "todo",
            detail=(
                f"{ingredients} ingredient{'s' if ingredients != 1 else ''}"
                if ingredients else "None yet — add them or upload your stock sheet"
            ),
            count=ingredients,
            href="/ingredients",
        ))

        with_recipe = {
            r.menu_item_id
            for r in (
                await db.execute(
                    select(Recipe).where(
                        Recipe.business_id == user.business_id,
                        Recipe.is_active.is_(True),
                        Recipe.menu_item_id.isnot(None),
                    )
                )
            ).scalars().all()
        }
        covered = 0
        if active_items:
            covered = int((await db.execute(
                select(func.count()).select_from(MenuItem).where(
                    MenuItem.business_id == user.business_id,
                    MenuItem.is_deleted.is_(False),
                    MenuItem.is_active.is_(True),
                    MenuItem.id.in_(with_recipe or [uuid.uuid4()]),
                )
            )).scalar() or 0)

        recipe_status = (
            "done" if active_items and covered == active_items
            else "partial" if covered else "todo"
        )
        steps.append(SetupStep(
            key="recipes",
            label="Recipes",
            description="What each menu item uses, so sales reduce your stock.",
            status=recipe_status,
            detail=(
                f"{covered} of {active_items} items have a recipe"
                if active_items else "Add menu items first"
            ),
            count=covered,
            total=active_items,
            href="/recipes",
        ))

        opening = int((await db.execute(
            select(func.count()).select_from(StocktakeSession).where(
                StocktakeSession.business_id == user.business_id,
                StocktakeSession.status == "APPROVED",
            )
        )).scalar() or 0)
        steps.append(SetupStep(
            key="opening_stock",
            label="Opening stock",
            description="Count what you have on hand, so expected stock has a starting point.",
            status="done" if opening else "todo",
            detail=(
                f"{opening} completed count{'s' if opening != 1 else ''}"
                if opening else "No stocktake yet — do one before you start trading"
            ),
            count=opening,
            href="/stocktake",
        ))

    required = [s for s in steps if s.required_to_trade]
    done = sum(1 for s in required if s.status == "done")
    percent = int(round(100 * done / len(required))) if required else 0

    return SetupProgress(
        business_name=business.name,
        inventory_enabled=business.inventory_enabled,
        ready_to_trade=all(s.status == "done" for s in required),
        percent_complete=percent,
        steps=steps,
    )


# ── Live theoretical stock ───────────────────────────────────────────────────

class LiveStockRow(BaseModel):
    ingredient_id: uuid.UUID
    name: str
    category: str | None
    unit_label: str | None

    opening_display: str
    received_display: str
    used_by_sales_display: str
    wastage_display: str
    adjustments_display: str
    expected_display: str

    last_counted_display: str | None
    last_counted_at: datetime | None
    variance_display: str | None
    variance_value_cents: int | None
    variance_percent: str | None
    needs_attention: bool

    expected_value_cents: int
    below_par: bool


class LiveStockResponse(BaseModel):
    """
    The manager's continuously-updated answer to "what should I have?".

    `since` is the last approved stocktake's cut-off, so the figures describe
    the CURRENT period rather than all time — which is what makes them
    actionable rather than a slowly-growing total.
    """
    period_started_at: datetime | None
    period_label: str
    total_expected_value_cents: int
    ingredient_count: int
    needs_attention_count: int
    rows: list[LiveStockRow]


@router.get("/live-stock", response_model=LiveStockResponse)
async def get_live_stock(
    attention_only: bool = Query(False),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Expected stock right now, per ingredient, with the breakdown that explains
    how it was reached.

    Comes entirely from the movement ledger — there is no stored "current
    quantity" column anywhere that could drift away from the history.
    """
    business = (
        await db.execute(select(Business).where(Business.id == user.business_id))
    ).scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    if not business.inventory_enabled:
        raise AppError(
            "INVENTORY_NOT_ENABLED",
            "Stock tracking is not switched on for this business",
            409,
        )

    ingredients = (
        await db.execute(
            select(Ingredient)
            .where(
                Ingredient.business_id == user.business_id,
                Ingredient.is_deleted.is_(False),
                Ingredient.is_active.is_(True),
            )
            .order_by(Ingredient.name)
        )
    ).scalars().all()

    last_count = (
        await db.execute(
            select(StocktakeSession)
            .where(
                StocktakeSession.business_id == user.business_id,
                StocktakeSession.status == "APPROVED",
            )
            .order_by(StocktakeSession.cutoff_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    since = last_count.cutoff_at if last_count else None

    ingredient_ids = [i.id for i in ingredients]
    totals = await movement_totals_by_type(
        db, user.business_id, since=since, ingredient_ids=ingredient_ids
    )
    on_hand = await stock_on_hand(db, user.business_id, ingredient_ids=ingredient_ids)
    opening_at_start = (
        await stock_on_hand(db, user.business_id, as_of=since, ingredient_ids=ingredient_ids)
        if since else {}
    )

    rows: list[LiveStockRow] = []
    total_value = 0
    attention = 0

    for ingredient in ingredients:
        canonical = CanonicalUnit(ingredient.canonical_unit)
        label = ingredient.unit_label

        received = sales = wastage = adjustments = opening_in_period = 0
        for (row_ingredient, movement_type), quantity in totals.items():
            if row_ingredient != ingredient.id:
                continue
            if movement_type in _RECEIPT_TYPES:
                received += quantity
            elif movement_type in _SALE_TYPES:
                sales += -quantity          # stored negative; show a magnitude
            elif movement_type in _WASTAGE_TYPES:
                wastage += -quantity
            elif movement_type in _OPENING_TYPES:
                opening_in_period += quantity
            else:
                adjustments += quantity

        opening = opening_at_start.get(ingredient.id, 0) + opening_in_period
        expected = on_hand.get(ingredient.id, 0)

        def show(value: int) -> str:
            return format_quantity(value, canonical, label)

        counted_display = None
        variance_display = None
        variance_cents = None
        variance_pct = None
        needs_attention = False

        if last_count:
            line = next(
                (l for l in (last_count.lines or []) if l.ingredient_id == ingredient.id),
                None,
            )
            if line and line.counted_quantity_milli is not None:
                counted_display = show(int(line.counted_quantity_milli))
                variance = IngredientVariance(
                    theoretical_consumption_milli=int(line.theoretical_consumption_milli or 0),
                    unit_cost_micro_cents=int(line.unit_cost_micro_cents or 0),
                )
                variance_display = show(int(line.variance_quantity_milli or 0))
                variance_cents = int(line.variance_value_cents or 0)
                variance_pct = format_basis_points(line.variance_basis_points)
                needs_attention = (
                    line.variance_basis_points is not None
                    and abs(line.variance_basis_points) >= DEFAULT_VARIANCE_ALERT_BASIS_POINTS
                )

        below_par = (
            ingredient.par_level_milli is not None
            and expected < ingredient.par_level_milli
        )
        if below_par:
            needs_attention = True
        if needs_attention:
            attention += 1

        expected_value = value_cents(
            expected, int(ingredient.cost_per_canonical_micro_cents or 0)
        )
        total_value += expected_value

        row = LiveStockRow(
            ingredient_id=ingredient.id,
            name=ingredient.name,
            category=ingredient.category,
            unit_label=label,
            opening_display=show(opening),
            received_display=show(received),
            used_by_sales_display=show(sales),
            wastage_display=show(wastage),
            adjustments_display=show(adjustments),
            expected_display=show(expected),
            last_counted_display=counted_display,
            last_counted_at=last_count.cutoff_at if last_count else None,
            variance_display=variance_display,
            variance_value_cents=variance_cents,
            variance_percent=variance_pct,
            needs_attention=needs_attention,
            expected_value_cents=expected_value,
            below_par=below_par,
        )
        if attention_only and not needs_attention:
            continue
        rows.append(row)

    return LiveStockResponse(
        period_started_at=since,
        period_label=(
            f"Since the count on {since.strftime('%d %b %Y')}"
            if since else "Since you started tracking stock"
        ),
        total_expected_value_cents=total_value,
        ingredient_count=len(ingredients),
        needs_attention_count=attention,
        rows=rows,
    )


# ── Recipe coverage ──────────────────────────────────────────────────────────

class RecipeCoverageItem(BaseModel):
    menu_item_id: uuid.UUID
    name: str
    is_active: bool
    status: str            # complete | incomplete | none
    line_count: int
    sizes_total: int
    sizes_covered: int
    message: str


class RecipeCoverage(BaseModel):
    total_items: int
    complete: int
    incomplete: int
    missing: int
    items: list[RecipeCoverageItem]


@router.get("/recipe-coverage", response_model=RecipeCoverage)
async def get_recipe_coverage(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Which menu items can be sold without reducing stock correctly.

    An item with no recipe still sells — refusing a sale over missing
    reference data would be far worse — but it silently understates usage,
    so the manager needs to see it before go-live rather than discover it in
    a variance report a month later.

    "Incomplete" specifically means the item has sizes but not every size has
    a scoped line, which is the subtle case: it looks configured, and quietly
    deducts nothing for the sizes it missed.
    """
    items = (
        await db.execute(
            select(MenuItem)
            .where(
                MenuItem.business_id == user.business_id,
                MenuItem.is_deleted.is_(False),
            )
            .order_by(MenuItem.name)
        )
    ).scalars().all()

    recipes = {
        r.menu_item_id: r
        for r in (
            await db.execute(
                select(Recipe).where(
                    Recipe.business_id == user.business_id,
                    Recipe.is_active.is_(True),
                    Recipe.menu_item_id.isnot(None),
                )
            )
        ).scalars().unique().all()
    }

    out: list[RecipeCoverageItem] = []
    complete = incomplete = missing = 0

    for item in items:
        recipe = recipes.get(item.id)
        option_ids: list[str] = []
        for group in ((item.options_json or {}).get("option_groups") or []):
            for option in (group.get("options") or []):
                if option.get("id"):
                    option_ids.append(str(option["id"]))

        if not recipe or not recipe.items:
            missing += 1
            out.append(RecipeCoverageItem(
                menu_item_id=item.id, name=item.name, is_active=item.is_active,
                status="none", line_count=0,
                sizes_total=len(option_ids), sizes_covered=0,
                message="No recipe — selling this will not reduce any stock",
            ))
            continue

        scoped = {str(l.option_id) for l in recipe.items if l.option_id}
        covered = [o for o in option_ids if o in scoped]

        if option_ids and scoped and len(covered) < len(option_ids):
            incomplete += 1
            uncovered = [o for o in option_ids if o not in scoped]
            out.append(RecipeCoverageItem(
                menu_item_id=item.id, name=item.name, is_active=item.is_active,
                status="incomplete", line_count=len(recipe.items),
                sizes_total=len(option_ids), sizes_covered=len(covered),
                message=(
                    f"{len(uncovered)} option(s) have no ingredients of their own — "
                    "those sizes will under-deduct"
                ),
            ))
            continue

        complete += 1
        out.append(RecipeCoverageItem(
            menu_item_id=item.id, name=item.name, is_active=item.is_active,
            status="complete", line_count=len(recipe.items),
            sizes_total=len(option_ids), sizes_covered=len(covered),
            message=f"{len(recipe.items)} ingredient(s)",
        ))

    return RecipeCoverage(
        total_items=len(items),
        complete=complete,
        incomplete=incomplete,
        missing=missing,
        items=out,
    )
