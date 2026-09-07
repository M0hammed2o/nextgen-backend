"""
Inventory routes — ingredients, suppliers, stock levels, goods receiving and
manual adjustments.

TENANT ISOLATION
----------------
Every query filters on ``user.business_id``, taken from the JWT. No endpoint
here accepts a business_id from the client. This is the platform's core
security invariant (SECURITY.md, Tenant Isolation) and the inventory module
does not get an exception.

RBAC, matched to how a restaurant actually runs
-----------------------------------------------
* Ingredients / suppliers  — OWNER or MANAGER. Reference data that changes
  costing and variance for everyone.
* Stock levels (read)      — any staff. The till and kitchen need to see it.
* Goods received           — any staff. Deliveries genuinely arrive when only
  staff are on the floor, and forcing a manager would mean deliveries go
  unrecorded, which is far more damaging to accuracy than the control is
  worth. Every receipt is attributed and audited.
* Manual adjustment        — OWNER or MANAGER only. This is the one movement
  type with no external document behind it, so it is the one that must be
  controlled.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.inventory.ledger import build_movement, stock_on_hand
from backend.app.inventory.units import (
    UnitError,
    cost_per_canonical_micro_cents,
    format_quantity,
    to_canonical,
    to_milli,
)
from shared.enums import (
    AuditScope,
    CanonicalUnit,
    CountMethod,
    StockMovementType,
    StockSourceType,
)
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.inventory import (
    GoodsReceipt,
    GoodsReceiptLine,
    Ingredient,
    StockMovement,
    Supplier,
)

router = APIRouter(prefix="/business/inventory", tags=["inventory"])


# ── Guard ────────────────────────────────────────────────────────────────────

async def _require_inventory_enabled(db: AsyncSession, business_id: uuid.UUID) -> Business:
    """
    The inventory module is opt-in per tenant. A tenant without it gets a
    clear 409 rather than a half-working stock system.
    """
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


def _convert(quantity, unit: str, canonical: str) -> int:
    """Convert a user-entered quantity to canonical milli-units, or 422."""
    try:
        return to_milli(quantity, unit, canonical=CanonicalUnit(canonical))
    except (UnitError, ValueError) as exc:
        raise AppError("INVALID_UNIT", str(exc), 422) from exc


# ── Schemas ──────────────────────────────────────────────────────────────────

class IngredientCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    canonical_unit: str = Field(pattern="^(GRAM|MILLILITRE|UNIT)$")
    count_method: str = Field(default="UNIT_COUNT", pattern="^(UNIT_COUNT|WEIGHT|VOLUME)$")
    sku: str | None = Field(default=None, max_length=64)
    purchase_unit_name: str | None = Field(default=None, max_length=64)
    purchase_pack_quantity: str = Field(
        default="1",
        description="How much one purchase pack holds, in purchase_pack_unit (e.g. '2' kg)",
    )
    purchase_pack_unit: str = Field(default="UNIT", max_length=32)
    cost_per_pack_cents: int | None = Field(default=None, ge=0)
    par_level_quantity: str | None = None
    par_level_unit: str | None = Field(default=None, max_length=32)
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        return v.strip()


class IngredientUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    count_method: str | None = Field(default=None, pattern="^(UNIT_COUNT|WEIGHT|VOLUME)$")
    sku: str | None = Field(default=None, max_length=64)
    purchase_unit_name: str | None = Field(default=None, max_length=64)
    purchase_pack_quantity: str | None = None
    purchase_pack_unit: str | None = Field(default=None, max_length=32)
    cost_per_pack_cents: int | None = Field(default=None, ge=0)
    par_level_quantity: str | None = None
    par_level_unit: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None
    notes: str | None = None


class IngredientResponse(BaseModel):
    id: uuid.UUID
    name: str
    sku: str | None
    canonical_unit: str
    count_method: str
    purchase_unit_name: str | None
    purchase_pack_milli: int
    cost_per_pack_cents: int | None
    cost_per_canonical_micro_cents: int
    par_level_milli: int | None
    is_active: bool
    notes: str | None

    # Convenience fields so a client never re-implements the unit arithmetic
    on_hand_milli: int = 0
    on_hand_display: str = ""
    below_par: bool = False


class SupplierCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    contact_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)


class SupplierResponse(BaseModel):
    id: uuid.UUID
    name: str
    contact_name: str | None
    phone: str | None
    email: str | None
    is_active: bool
    model_config = {"from_attributes": True}


class GoodsReceiptLineRequest(BaseModel):
    ingredient_id: uuid.UUID
    quantity: str = Field(description="What was actually COUNTED on arrival, not ordered")
    unit: str = Field(
        default="PACK",
        max_length=32,
        description="PACK uses the ingredient's own pack size; otherwise KG/G/L/ML/UNIT",
    )
    unit_cost_cents: int | None = Field(default=None, ge=0)


class GoodsReceiptCreateRequest(BaseModel):
    lines: list[GoodsReceiptLineRequest] = Field(min_length=1)
    supplier_id: uuid.UUID | None = None
    reference: str | None = Field(default=None, max_length=128)
    notes: str | None = None
    occurred_at: datetime | None = None


class StockLevelResponse(BaseModel):
    ingredient_id: uuid.UUID
    name: str
    canonical_unit: str
    count_method: str
    on_hand_milli: int
    on_hand_display: str
    par_level_milli: int | None
    below_par: bool
    unit_cost_micro_cents: int
    value_cents: int


class AdjustmentRequest(BaseModel):
    ingredient_id: uuid.UUID
    quantity: str = Field(description="Signed: negative removes stock")
    unit: str = Field(max_length=32)
    reason: str = Field(min_length=3, max_length=255)


# ── Ingredients ──────────────────────────────────────────────────────────────

def _ingredient_response(ingredient: Ingredient, on_hand: int) -> IngredientResponse:
    par = ingredient.par_level_milli
    return IngredientResponse(
        id=ingredient.id,
        name=ingredient.name,
        sku=ingredient.sku,
        canonical_unit=ingredient.canonical_unit,
        count_method=ingredient.count_method,
        purchase_unit_name=ingredient.purchase_unit_name,
        purchase_pack_milli=int(ingredient.purchase_pack_milli or 0),
        cost_per_pack_cents=ingredient.cost_per_pack_cents,
        cost_per_canonical_micro_cents=int(ingredient.cost_per_canonical_micro_cents or 0),
        par_level_milli=par,
        is_active=ingredient.is_active,
        notes=ingredient.notes,
        on_hand_milli=on_hand,
        on_hand_display=format_quantity(on_hand, CanonicalUnit(ingredient.canonical_unit)),
        below_par=par is not None and on_hand < par,
    )


@router.get("/ingredients", response_model=list[IngredientResponse])
async def list_ingredients(
    include_inactive: bool = Query(False),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Every ingredient for this business, with its current stock position."""
    await _require_inventory_enabled(db, user.business_id)

    stmt = select(Ingredient).where(
        Ingredient.business_id == user.business_id,
        Ingredient.is_deleted.is_(False),
    )
    if not include_inactive:
        stmt = stmt.where(Ingredient.is_active.is_(True))

    ingredients = (await db.execute(stmt.order_by(Ingredient.name))).scalars().all()
    on_hand = await stock_on_hand(db, user.business_id)
    return [_ingredient_response(i, on_hand.get(i.id, 0)) for i in ingredients]


@router.post("/ingredients", response_model=IngredientResponse, status_code=201)
async def create_ingredient(
    body: IngredientCreateRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Create an ingredient, converting its pack size and par level to canonical units."""
    await _require_inventory_enabled(db, user.business_id)

    existing = (
        await db.execute(
            select(Ingredient).where(
                Ingredient.business_id == user.business_id,
                Ingredient.name == body.name,
                Ingredient.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise AppError(
            "INGREDIENT_EXISTS", f"An ingredient named {body.name!r} already exists", 409
        )

    pack_milli = _convert(body.purchase_pack_quantity, body.purchase_pack_unit, body.canonical_unit)
    if pack_milli <= 0:
        raise AppError("INVALID_PACK_SIZE", "Purchase pack size must be greater than zero", 422)

    par_milli = None
    if body.par_level_quantity is not None:
        par_milli = _convert(
            body.par_level_quantity, body.par_level_unit or body.purchase_pack_unit, body.canonical_unit
        )

    unit_cost = (
        cost_per_canonical_micro_cents(body.cost_per_pack_cents, pack_milli)
        if body.cost_per_pack_cents
        else 0
    )

    ingredient = Ingredient(
        business_id=user.business_id,
        name=body.name,
        sku=body.sku,
        canonical_unit=body.canonical_unit,
        count_method=body.count_method,
        purchase_unit_name=body.purchase_unit_name,
        purchase_pack_milli=pack_milli,
        cost_per_pack_cents=body.cost_per_pack_cents,
        cost_per_canonical_micro_cents=unit_cost,
        par_level_milli=par_milli,
        notes=body.notes,
    )
    db.add(ingredient)
    await db.commit()
    await db.refresh(ingredient)
    return _ingredient_response(ingredient, 0)


@router.patch("/ingredients/{ingredient_id}", response_model=IngredientResponse)
async def update_ingredient(
    ingredient_id: uuid.UUID,
    body: IngredientUpdateRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Update an ingredient. Changing pack size or pack cost recomputes the unit
    cost, which affects only FUTURE movements — existing ledger rows keep the
    cost snapshotted at the time they were posted, so history never reprices.
    """
    await _require_inventory_enabled(db, user.business_id)

    ingredient = (
        await db.execute(
            select(Ingredient).where(
                Ingredient.id == ingredient_id,
                Ingredient.business_id == user.business_id,
                Ingredient.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not ingredient:
        raise NotFoundError("Ingredient", str(ingredient_id))

    for simple in ("name", "sku", "count_method", "purchase_unit_name", "is_active", "notes"):
        value = getattr(body, simple, None)
        if value is not None:
            setattr(ingredient, simple, value)

    if body.purchase_pack_quantity is not None:
        pack_unit = body.purchase_pack_unit or ingredient.canonical_unit
        pack_milli = _convert(body.purchase_pack_quantity, pack_unit, ingredient.canonical_unit)
        if pack_milli <= 0:
            raise AppError("INVALID_PACK_SIZE", "Purchase pack size must be greater than zero", 422)
        ingredient.purchase_pack_milli = pack_milli

    if body.cost_per_pack_cents is not None:
        ingredient.cost_per_pack_cents = body.cost_per_pack_cents

    if body.purchase_pack_quantity is not None or body.cost_per_pack_cents is not None:
        ingredient.cost_per_canonical_micro_cents = (
            cost_per_canonical_micro_cents(
                ingredient.cost_per_pack_cents or 0, ingredient.purchase_pack_milli
            )
            if ingredient.cost_per_pack_cents
            else 0
        )

    if body.par_level_quantity is not None:
        ingredient.par_level_milli = _convert(
            body.par_level_quantity,
            body.par_level_unit or ingredient.canonical_unit,
            ingredient.canonical_unit,
        )

    await db.commit()
    await db.refresh(ingredient)
    on_hand = await stock_on_hand(db, user.business_id, ingredient_ids=[ingredient.id])
    return _ingredient_response(ingredient, on_hand.get(ingredient.id, 0))


@router.delete("/ingredients/{ingredient_id}", status_code=204)
async def delete_ingredient(
    ingredient_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Soft-delete only. The ledger references ingredients with ON DELETE
    RESTRICT precisely so a hard delete can never erase the stock history
    that explains a past variance.
    """
    await _require_inventory_enabled(db, user.business_id)
    ingredient = (
        await db.execute(
            select(Ingredient).where(
                Ingredient.id == ingredient_id, Ingredient.business_id == user.business_id
            )
        )
    ).scalar_one_or_none()
    if not ingredient:
        raise NotFoundError("Ingredient", str(ingredient_id))
    ingredient.is_deleted = True
    ingredient.is_active = False
    await db.commit()
    return None


# ── Stock levels ─────────────────────────────────────────────────────────────

@router.get("/stock", response_model=list[StockLevelResponse])
async def get_stock_levels(
    below_par_only: bool = Query(False),
    as_of: datetime | None = Query(None),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Current stock per ingredient, derived by summing the ledger — there is no
    denormalised quantity column that could drift out of step with history.
    """
    await _require_inventory_enabled(db, user.business_id)

    ingredients = (
        (
            await db.execute(
                select(Ingredient)
                .where(
                    Ingredient.business_id == user.business_id,
                    Ingredient.is_deleted.is_(False),
                    Ingredient.is_active.is_(True),
                )
                .order_by(Ingredient.name)
            )
        )
        .scalars()
        .all()
    )
    on_hand = await stock_on_hand(db, user.business_id, as_of=as_of)

    out: list[StockLevelResponse] = []
    for ingredient in ingredients:
        quantity = on_hand.get(ingredient.id, 0)
        below = ingredient.par_level_milli is not None and quantity < ingredient.par_level_milli
        if below_par_only and not below:
            continue
        unit_cost = int(ingredient.cost_per_canonical_micro_cents or 0)
        from backend.app.inventory.units import value_cents as _value

        out.append(
            StockLevelResponse(
                ingredient_id=ingredient.id,
                name=ingredient.name,
                canonical_unit=ingredient.canonical_unit,
                count_method=ingredient.count_method,
                on_hand_milli=quantity,
                on_hand_display=format_quantity(
                    quantity, CanonicalUnit(ingredient.canonical_unit)
                ),
                par_level_milli=ingredient.par_level_milli,
                below_par=below,
                unit_cost_micro_cents=unit_cost,
                value_cents=_value(quantity, unit_cost),
            )
        )
    return out


@router.get("/stock/{ingredient_id}/movements")
async def get_ingredient_movements(
    ingredient_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """The ledger for one ingredient, newest first — the audit trail behind a variance."""
    await _require_inventory_enabled(db, user.business_id)
    rows = (
        (
            await db.execute(
                select(StockMovement)
                .where(
                    StockMovement.business_id == user.business_id,
                    StockMovement.ingredient_id == ingredient_id,
                )
                .order_by(StockMovement.occurred_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(m.id),
            "quantity_milli": int(m.quantity_milli),
            "movement_type": m.movement_type,
            "source_type": m.source_type,
            "source_id": str(m.source_id) if m.source_id else None,
            "value_cents": int(m.value_cents or 0),
            "notes": m.notes,
            "occurred_at": m.occurred_at.isoformat() if m.occurred_at else None,
        }
        for m in rows
    ]


# ── Suppliers ────────────────────────────────────────────────────────────────

@router.get("/suppliers", response_model=list[SupplierResponse])
async def list_suppliers(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    await _require_inventory_enabled(db, user.business_id)
    rows = (
        (
            await db.execute(
                select(Supplier)
                .where(Supplier.business_id == user.business_id, Supplier.is_active.is_(True))
                .order_by(Supplier.name)
            )
        )
        .scalars()
        .all()
    )
    return [SupplierResponse.model_validate(s) for s in rows]


@router.post("/suppliers", response_model=SupplierResponse, status_code=201)
async def create_supplier(
    body: SupplierCreateRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_inventory_enabled(db, user.business_id)
    supplier = Supplier(business_id=user.business_id, **body.model_dump())
    db.add(supplier)
    await db.commit()
    await db.refresh(supplier)
    return SupplierResponse.model_validate(supplier)


# ── Goods received ───────────────────────────────────────────────────────────

@router.post("/goods-receipts", status_code=201)
async def create_goods_receipt(
    body: GoodsReceiptCreateRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Record a delivery and post positive stock movements for it.

    Quantities are what was COUNTED on arrival. Receiving at ordered quantity
    instead of counted quantity is one of the top causes of an unexplained
    variance surfacing weeks later, so the field is labelled accordingly and
    there is deliberately no "expected quantity" to copy from.
    """
    await _require_inventory_enabled(db, user.business_id)

    ingredient_ids = [line.ingredient_id for line in body.lines]
    ingredients = {
        i.id: i
        for i in (
            (
                await db.execute(
                    select(Ingredient).where(
                        Ingredient.business_id == user.business_id,
                        Ingredient.id.in_(ingredient_ids),
                        Ingredient.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    missing = [str(i) for i in ingredient_ids if i not in ingredients]
    if missing:
        raise AppError(
            "UNKNOWN_INGREDIENT", f"Unknown ingredient(s): {', '.join(missing)}", 422
        )

    occurred = body.occurred_at or datetime.now(timezone.utc)
    receipt = GoodsReceipt(
        business_id=user.business_id,
        supplier_id=body.supplier_id,
        reference=body.reference,
        received_by_user_id=user.user_id,
        notes=body.notes,
        occurred_at=occurred,
        created_at=datetime.now(timezone.utc),
    )
    db.add(receipt)
    await db.flush()

    total_cost = 0
    for line in body.lines:
        ingredient = ingredients[line.ingredient_id]

        # "PACK" means whole purchase packs of this ingredient's own size —
        # the common case (5 x 2 kg bags) and the one most likely to be
        # mis-converted if the client had to do the maths itself.
        if line.unit.strip().upper() == "PACK":
            from backend.app.inventory.units import pack_to_milli

            try:
                quantity_milli = pack_to_milli(line.quantity, ingredient.purchase_pack_milli)
            except UnitError as exc:
                raise AppError("INVALID_QUANTITY", str(exc), 422) from exc
        else:
            quantity_milli = _convert(line.quantity, line.unit, ingredient.canonical_unit)

        if quantity_milli <= 0:
            raise AppError(
                "INVALID_QUANTITY", "Received quantity must be greater than zero", 422
            )

        line_cost = int(line.unit_cost_cents or 0)
        total_cost += line_cost

        db.add(
            GoodsReceiptLine(
                business_id=user.business_id,
                goods_receipt_id=receipt.id,
                ingredient_id=ingredient.id,
                entered_quantity=str(line.quantity),
                entered_unit=line.unit,
                quantity_milli=quantity_milli,
                unit_cost_cents=line.unit_cost_cents,
                line_cost_cents=line_cost,
            )
        )
        db.add(
            build_movement(
                business_id=user.business_id,
                ingredient_id=ingredient.id,
                quantity_milli=quantity_milli,
                movement_type=StockMovementType.GOODS_RECEIVED.value,
                unit_cost_micro_cents=int(ingredient.cost_per_canonical_micro_cents or 0),
                source_type=StockSourceType.GOODS_RECEIPT.value,
                source_id=receipt.id,
                created_by_user_id=user.user_id,
                occurred_at=occurred,
            )
        )

    receipt.total_cost_cents = total_cost
    await db.commit()
    await db.refresh(receipt)
    return {"id": str(receipt.id), "lines": len(body.lines), "total_cost_cents": total_cost}


@router.get("/goods-receipts")
async def list_goods_receipts(
    limit: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    await _require_inventory_enabled(db, user.business_id)
    rows = (
        (
            await db.execute(
                select(GoodsReceipt)
                .options(selectinload(GoodsReceipt.lines))
                .where(GoodsReceipt.business_id == user.business_id)
                .order_by(GoodsReceipt.occurred_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return [
        {
            "id": str(r.id),
            "reference": r.reference,
            "supplier_id": str(r.supplier_id) if r.supplier_id else None,
            "total_cost_cents": int(r.total_cost_cents or 0),
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "line_count": len(r.lines or []),
        }
        for r in rows
    ]


# ── Manual adjustment ────────────────────────────────────────────────────────

@router.post("/adjustments", status_code=201)
async def create_adjustment(
    body: AdjustmentRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    An authorised correction with no external document behind it.

    OWNER/MANAGER only and always audited, because this is the one movement
    type that can make stock appear or disappear on somebody's say-so.
    """
    await _require_inventory_enabled(db, user.business_id)

    ingredient = (
        await db.execute(
            select(Ingredient).where(
                Ingredient.id == body.ingredient_id,
                Ingredient.business_id == user.business_id,
                Ingredient.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not ingredient:
        raise NotFoundError("Ingredient", str(body.ingredient_id))

    try:
        magnitude = Decimal(str(body.quantity).strip())
    except Exception as exc:
        raise AppError("INVALID_QUANTITY", f"{body.quantity!r} is not a number", 422) from exc
    if magnitude == 0:
        raise AppError("INVALID_QUANTITY", "Adjustment cannot be zero", 422)

    quantity_milli = _convert(abs(magnitude), body.unit, ingredient.canonical_unit)
    if magnitude < 0:
        quantity_milli = -quantity_milli

    movement = build_movement(
        business_id=user.business_id,
        ingredient_id=ingredient.id,
        quantity_milli=quantity_milli,
        movement_type=StockMovementType.MANUAL_ADJUSTMENT.value,
        unit_cost_micro_cents=int(ingredient.cost_per_canonical_micro_cents or 0),
        source_type=StockSourceType.MANUAL.value,
        created_by_user_id=user.user_id,
        notes=body.reason,
    )
    db.add(movement)
    db.add(
        AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="inventory.stock.adjusted",
            target_type="ingredient",
            target_id=ingredient.id,
            diff_json={
                "quantity_milli": quantity_milli,
                "entered": f"{body.quantity} {body.unit}",
                "reason": body.reason,
                "actor_role": user.role,
            },
        )
    )
    await db.commit()
    return {
        "ingredient_id": str(ingredient.id),
        "quantity_milli": quantity_milli,
        "display": format_quantity(quantity_milli, CanonicalUnit(ingredient.canonical_unit)),
    }
