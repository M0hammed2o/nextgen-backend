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
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.errors import AppError, DuplicateError, NotFoundError
from backend.app.core.ownership import require_owned
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.inventory.ledger import (
    build_movement,
    is_idempotency_violation,
    opening_balance_idempotency_key,
    stock_on_hand,
)
from backend.app.inventory.units import (
    UnitError,
    cost_per_canonical_micro_cents,
    format_quantity,
    to_canonical,
    to_milli,
    value_cents,
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
    unit_label: str | None = Field(
        default=None, max_length=32,
        description="The noun staff say for a countable ingredient - "
                    "'portion', 'box', 'sachet'. Ignored for weight/volume.",
    )
    category: str | None = Field(default=None, max_length=64)
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
    unit_label: str | None = Field(default=None, max_length=32)
    category: str | None = Field(default=None, max_length=64)
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
    unit_label: str | None = None
    category: str | None = None
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


class SupplierUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    contact_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    is_active: bool | None = Field(
        default=None,
        description="Deactivate (false) or reactivate (true) this supplier",
    )


class SupplierResponse(BaseModel):
    id: uuid.UUID
    name: str
    contact_name: str | None
    phone: str | None
    email: str | None
    is_active: bool
    # Usage visibility, so deactivation/deletion is an informed decision —
    # derived from goods_receipts, never a cached/denormalised counter.
    goods_receipt_count: int = 0
    last_received_at: datetime | None = None


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
    idempotency_key: str | None = Field(
        default=None, max_length=128,
        description="Optional client-generated key (BUG-0112). A retry/"
                     "double-click with the same key returns the original "
                     "receipt instead of creating a duplicate. Omit entirely "
                     "to keep old-client behaviour unchanged.",
    )


class StockLevelResponse(BaseModel):
    ingredient_id: uuid.UUID
    name: str
    canonical_unit: str
    count_method: str
    unit_label: str | None = None
    category: str | None = None
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


class OpeningStockRequest(BaseModel):
    ingredient_id: uuid.UUID
    quantity: str = Field(description="Opening stock quantity — positive, unsigned")
    unit: str = Field(max_length=32)
    source: str | None = Field(
        default=None,
        max_length=255,
        description="Where this count came from, e.g. 'Physical count on "
                    "go-live day', 'Carried over from previous system'",
    )
    occurred_at: datetime | None = Field(
        default=None,
        description="When this stock position was actually true. Defaults "
                    "to now if omitted.",
    )


class OpeningStockResponse(BaseModel):
    ingredient_id: uuid.UUID
    ingredient_name: str
    quantity_milli: int
    quantity_display: str
    value_cents: int
    occurred_at: datetime


# ── Ingredients ──────────────────────────────────────────────────────────────

def _ingredient_response(ingredient: Ingredient, on_hand: int) -> IngredientResponse:
    par = ingredient.par_level_milli
    return IngredientResponse(
        id=ingredient.id,
        name=ingredient.name,
        sku=ingredient.sku,
        canonical_unit=ingredient.canonical_unit,
        count_method=ingredient.count_method,
        unit_label=ingredient.unit_label,
        category=ingredient.category,
        purchase_unit_name=ingredient.purchase_unit_name,
        purchase_pack_milli=int(ingredient.purchase_pack_milli or 0),
        cost_per_pack_cents=ingredient.cost_per_pack_cents,
        cost_per_canonical_micro_cents=int(ingredient.cost_per_canonical_micro_cents or 0),
        par_level_milli=par,
        is_active=ingredient.is_active,
        notes=ingredient.notes,
        on_hand_milli=on_hand,
        on_hand_display=format_quantity(
            on_hand, CanonicalUnit(ingredient.canonical_unit), ingredient.unit_label
        ),
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
        unit_label=(body.unit_label or "").strip() or None,
        category=(body.category or "").strip() or None,
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

    for simple in ("name", "sku", "count_method", "unit_label", "category",
                   "purchase_unit_name", "is_active", "notes"):
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
                unit_label=ingredient.unit_label,
                category=ingredient.category,
                on_hand_milli=quantity,
                on_hand_display=format_quantity(
                    quantity, CanonicalUnit(ingredient.canonical_unit), ingredient.unit_label
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

async def _supplier_usage(
    db: AsyncSession, business_id: uuid.UUID, supplier_ids: list
) -> dict:
    """
    Goods-receipt count + most recent receipt date per supplier — the usage
    signal a manager needs before deactivating or deleting one. Derived
    live from goods_receipts (never a cached counter), same discipline as
    everything else in this module being ledger/record-derived.
    """
    ids = [s for s in supplier_ids if s is not None]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(
                GoodsReceipt.supplier_id,
                func.count(GoodsReceipt.id),
                func.max(GoodsReceipt.occurred_at),
            )
            .where(
                GoodsReceipt.business_id == business_id,
                GoodsReceipt.supplier_id.in_(list(set(ids))),
            )
            .group_by(GoodsReceipt.supplier_id)
        )
    ).all()
    return {row[0]: (int(row[1]), row[2]) for row in rows}


def _supplier_response(supplier: Supplier, usage: tuple = (0, None)) -> SupplierResponse:
    count, last_at = usage
    return SupplierResponse(
        id=supplier.id,
        name=supplier.name,
        contact_name=supplier.contact_name,
        phone=supplier.phone,
        email=supplier.email,
        is_active=supplier.is_active,
        goods_receipt_count=count,
        last_received_at=last_at,
    )


@router.get("/suppliers", response_model=list[SupplierResponse])
async def list_suppliers(
    include_inactive: bool = Query(
        False, description="Include deactivated suppliers, for review before permanent deletion"
    ),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    await _require_inventory_enabled(db, user.business_id)
    stmt = select(Supplier).where(Supplier.business_id == user.business_id)
    if not include_inactive:
        stmt = stmt.where(Supplier.is_active.is_(True))
    rows = (await db.execute(stmt.order_by(Supplier.name))).scalars().all()
    usage = await _supplier_usage(db, user.business_id, [s.id for s in rows])
    return [_supplier_response(s, usage.get(s.id, (0, None))) for s in rows]


@router.post("/suppliers", response_model=SupplierResponse, status_code=201)
async def create_supplier(
    body: SupplierCreateRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    await _require_inventory_enabled(db, user.business_id)
    supplier = Supplier(business_id=user.business_id, **body.model_dump())
    db.add(supplier)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise DuplicateError("supplier", "name") from exc
    await db.refresh(supplier)
    return _supplier_response(supplier)


@router.patch("/suppliers/{supplier_id}", response_model=SupplierResponse)
async def update_supplier(
    supplier_id: uuid.UUID,
    body: SupplierUpdateRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Edit supplier details, and/or deactivate or reactivate via is_active.

    Deactivating does not touch any goods receipt already attributed to
    this supplier — it only stops the supplier appearing in the default
    (active-only) list and picker, same soft-delete discipline as
    ingredients. Every change is audited (AuditEvent), same as manual
    stock adjustments — this is reference data that affects costing and
    who a receipt is attributed to, so who changed what and when matters.
    """
    await _require_inventory_enabled(db, user.business_id)
    supplier = await require_owned(db, Supplier, supplier_id, user.business_id, label="Supplier")

    diff: dict = {}
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise AppError("INVALID_NAME", "Supplier name cannot be blank", 422)
        if new_name != supplier.name:
            diff["name"] = {"old": supplier.name, "new": new_name}
            supplier.name = new_name
    for field in ("contact_name", "phone", "email"):
        value = getattr(body, field)
        if value is not None and value != getattr(supplier, field):
            diff[field] = {"old": getattr(supplier, field), "new": value}
            setattr(supplier, field, value)
    if body.is_active is not None and body.is_active != supplier.is_active:
        diff["is_active"] = {"old": supplier.is_active, "new": body.is_active}
        supplier.is_active = body.is_active

    if diff:
        db.add(
            AuditEvent(
                scope=AuditScope.BUSINESS.value,
                business_id=user.business_id,
                actor_user_id=user.user_id,
                action="inventory.supplier.updated",
                target_type="supplier",
                target_id=supplier.id,
                diff_json={**diff, "actor_role": user.role},
            )
        )
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise DuplicateError("supplier", "name") from exc
        await db.refresh(supplier)

    usage = await _supplier_usage(db, user.business_id, [supplier.id])
    return _supplier_response(supplier, usage.get(supplier.id, (0, None)))


@router.delete("/suppliers/{supplier_id}", status_code=204)
async def delete_supplier(
    supplier_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently delete a supplier — irreversible, unlike the soft
    deactivate above (PATCH .../suppliers/{id} with is_active=false).

    Only allowed when BOTH hold, verified server-side against the actual
    data rather than assumed:
      1. The supplier has never been attributed on a goods receipt — a
         hard delete must never orphan or corrupt historical
         goods-received data (goods_receipts.supplier_id is ON DELETE SET
         NULL, so the FK itself would technically allow it, but silently
         nulling out who delivered a past receipt is exactly the kind of
         data loss this check exists to prevent).
      2. The supplier is already deactivated — the same two-step gate this
         platform already uses for staff (`DELETE
         /business/staff/{id}/permanent`), against deleting something
         still in active use by mistake.
    A supplier that fails either check should be deactivated instead,
    which is always safe and always available.
    """
    await _require_inventory_enabled(db, user.business_id)
    supplier = await require_owned(db, Supplier, supplier_id, user.business_id, label="Supplier")

    usage_count = (
        await db.execute(
            select(func.count(GoodsReceipt.id)).where(
                GoodsReceipt.business_id == user.business_id,
                GoodsReceipt.supplier_id == supplier.id,
            )
        )
    ).scalar_one()
    if usage_count > 0:
        raise AppError(
            "SUPPLIER_IN_USE",
            f"This supplier has {usage_count} goods receipt(s) on record and "
            "cannot be permanently deleted. Deactivate it instead.",
            409,
            details={"goods_receipt_count": usage_count},
        )
    if supplier.is_active:
        raise AppError(
            "SUPPLIER_STILL_ACTIVE",
            "Deactivate this supplier before permanently deleting it",
            409,
        )

    db.add(
        AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="inventory.supplier.deleted",
            target_type="supplier",
            target_id=supplier.id,
            diff_json={"name": supplier.name, "actor_role": user.role},
        )
    )
    await db.delete(supplier)
    await db.commit()
    return None


# ── Goods received ───────────────────────────────────────────────────────────

def _is_goods_receipt_idempotency_violation(exc: IntegrityError) -> bool:
    """
    True if this IntegrityError is a violation of
    uq_goods_receipts_business_idempotency_key (migration 0052) — checked
    against the driver's own error text (what Postgres actually reports for
    a unique_violation), same technique as
    routes_pos.py::_is_unique_violation / ledger.py::is_idempotency_violation,
    so this stays driver-agnostic.
    """
    return "uq_goods_receipts_business_idempotency_key" in str(
        getattr(exc, "orig", exc)
    )


async def _replay_existing_goods_receipt(
    db: AsyncSession, business_id: uuid.UUID, idempotency_key: str,
) -> dict | None:
    """
    Look up a previously-committed receipt for this (business, key). Used
    both for the fast sequential-retry path (checked before any validation
    runs) and for the losing side of a genuine concurrent-insert race
    (checked after an IntegrityError on the partial unique index) — same
    shape as routes_pos.py::_replay_idempotent_checkout.
    """
    result = await db.execute(
        select(GoodsReceipt)
        .options(selectinload(GoodsReceipt.lines))
        .where(
            GoodsReceipt.business_id == business_id,
            GoodsReceipt.idempotency_key == idempotency_key,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        return None
    return {
        "id": str(existing.id),
        "lines": len(existing.lines),
        "total_cost_cents": int(existing.total_cost_cents or 0),
    }


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

    IDEMPOTENCY (BUG-0112, migration 0052): an optional client-supplied
    idempotency_key makes a double-click, a client retry after an apparent
    timeout, or two browser tabs submitting the same delivery produce
    exactly one receipt — mirrors create_pos_order's orders.idempotency_key
    pattern exactly: an early replay lookup, a partial unique DB index
    (uq_goods_receipts_business_idempotency_key, NULLs excluded) as the
    actual concurrency guard, and a losing-race replay on IntegrityError.
    Omitting the field entirely preserves old-client behaviour unchanged —
    the column is nullable, same compatibility shape as
    orders.idempotency_key (migration 0023).
    """
    await _require_inventory_enabled(db, user.business_id)

    # ── Idempotency: fast sequential-retry path. Deliberately runs before
    #    any ingredient/supplier validation, exactly like create_pos_order's
    #    early idempotency check — a replayed request shouldn't fail
    #    validation just because, e.g., an ingredient was deleted since the
    #    original (already-successful) submission. ─────────────────────────
    if body.idempotency_key:
        replayed = await _replay_existing_goods_receipt(
            db, user.business_id, body.idempotency_key
        )
        if replayed is not None:
            return replayed

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

    if body.supplier_id is not None:
        await require_owned(db, Supplier, body.supplier_id, user.business_id, label="Supplier")

    occurred = body.occurred_at or datetime.now(timezone.utc)
    # id generated client-side (the model's default only fires at flush) so
    # it's available for the lines'/movement's FK below without needing an
    # early flush — the flush/commit is deliberately deferred to the very
    # end, after every line has been resolved and costed, so an
    # idempotency-key collision is the LAST thing that can go wrong, not
    # the first, and nothing partial is ever staged past that point.
    receipt_id = uuid.uuid4()
    receipt = GoodsReceipt(
        id=receipt_id,
        business_id=user.business_id,
        supplier_id=body.supplier_id,
        reference=body.reference,
        received_by_user_id=user.user_id,
        idempotency_key=body.idempotency_key,
        notes=body.notes,
        occurred_at=occurred,
        created_at=datetime.now(timezone.utc),
    )

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

        # COST SEMANTICS (BUG-0111 fix; see DECISIONS.md and DATABASE.md for
        # the full write-up):
        #
        # `unit_cost_cents` is the cost of ONE of the `unit` on THIS line —
        # cents per kg, cents per litre, cents per whole pack, etc. — not a
        # per-line total. The receipt line, the receipt total, and the stock
        # movement's valuation must all agree for one delivery, so the
        # actual line total is that rate multiplied by the entered quantity,
        # done in Decimal (never float) and rounded to whole cents once, at
        # the end, the same discipline units.value_cents() already uses.
        #
        # Whichever cost basis backs this line, it is then re-expressed as
        # micro-cents per CANONICAL unit — the same representation the
        # ingredient's own standing cost is held in — via the existing
        # cost_per_canonical_micro_cents() derivation (it takes any cost and
        # the milli-quantity it was paid for; a receipt line's own cost over
        # its own quantity_milli is exactly that). Feeding that back into
        # build_movement()/value_cents() is what guarantees the stock
        # movement's valuation is numerically identical to the line's own
        # recorded cost, rather than drifting between two independent
        # calculations.
        if line.unit_cost_cents is not None:
            # This receipt states its own cost — use it, not the
            # ingredient's standing cost. This is what BUG-0111 got wrong:
            # the movement used to be valued from the standing cost even
            # when the delivery note said otherwise.
            line_cost_cents = int(
                (Decimal(line.unit_cost_cents) * Decimal(str(line.quantity))).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            line_unit_cost_micro_cents = cost_per_canonical_micro_cents(
                line_cost_cents, quantity_milli
            )
        else:
            # No cost supplied on this line (legal per the schema — cost is
            # optional so a receipt can still be recorded when a delivery
            # note is missing a price). Fall back to the ingredient's
            # standing cost rather than silently recording R0.00 against
            # real stock value, which is the other half of BUG-0111.
            line_unit_cost_micro_cents = int(ingredient.cost_per_canonical_micro_cents or 0)
            line_cost_cents = value_cents(quantity_milli, line_unit_cost_micro_cents)

        total_cost += line_cost_cents

        db.add(
            GoodsReceiptLine(
                business_id=user.business_id,
                goods_receipt_id=receipt_id,
                ingredient_id=ingredient.id,
                entered_quantity=str(line.quantity),
                entered_unit=line.unit,
                quantity_milli=quantity_milli,
                unit_cost_cents=line.unit_cost_cents,
                line_cost_cents=line_cost_cents,
            )
        )
        db.add(
            build_movement(
                business_id=user.business_id,
                ingredient_id=ingredient.id,
                quantity_milli=quantity_milli,
                movement_type=StockMovementType.GOODS_RECEIVED.value,
                # Valued from THIS line's own cost basis (receipt cost if
                # supplied, else the ingredient's standing cost) — never
                # unconditionally the standing cost. Receiving goods does
                # NOT update the ingredient's standing cost itself (see
                # DECISIONS.md "Goods-receipt cost-update policy") — this
                # only affects how this one movement is valued.
                unit_cost_micro_cents=line_unit_cost_micro_cents,
                source_type=StockSourceType.GOODS_RECEIPT.value,
                source_id=receipt_id,
                created_by_user_id=user.user_id,
                occurred_at=occurred,
            )
        )

    receipt.total_cost_cents = total_cost
    db.add(receipt)

    # ── Everything above is staged in the session, nothing has hit the DB
    #    yet. A double-tapped submission with the SAME idempotency_key can
    #    race past the early replay lookup above before either side has
    #    committed — the partial unique index
    #    (uq_goods_receipts_business_idempotency_key) is what actually
    #    decides the winner. The loser's flush/commit raises IntegrityError
    #    here, caught below and replayed into the winner's receipt instead
    #    of propagating as an unhandled 500 — same shape as
    #    create_pos_order's try/except IntegrityError block. ───────────────
    try:
        await db.flush()
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if body.idempotency_key and _is_goods_receipt_idempotency_violation(exc):
            replayed = await _replay_existing_goods_receipt(
                db, user.business_id, body.idempotency_key
            )
            if replayed is not None:
                if (
                    replayed["total_cost_cents"] != total_cost
                    or replayed["lines"] != len(body.lines)
                ):
                    # Same key, materially different payload. Decision
                    # (DECISIONS.md): mirror create_pos_order exactly —
                    # replay the winning receipt silently rather than
                    # reject with a conflict, and log so a real client bug
                    # (reusing a key across two different deliveries) is
                    # still visible operationally.
                    import logging
                    logging.getLogger("nextgen").warning(
                        "Idempotency replay for goods_receipts key=%s returned a "
                        "different receipt than this request would have "
                        "created: existing total_cost_cents=%s lines=%s vs this "
                        "request's total_cost_cents=%s lines=%s",
                        body.idempotency_key, replayed["total_cost_cents"],
                        replayed["lines"], total_cost, len(body.lines),
                    )
                return replayed
        # Not an idempotency-key collision (or no key was supplied at all) —
        # a real constraint violation must never be masked as a replay.
        raise

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


# ── Opening stock ────────────────────────────────────────────────────────────

@router.post("/opening-stock", response_model=OpeningStockResponse, status_code=201)
async def set_opening_stock(
    body: OpeningStockRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Post an ingredient's one-time starting stock position, through the
    ledger like every other movement — never a direct mutation of a
    quantity column, because stock here has no cached quantity, it is
    always derived by summing stock_movements.

    NO SEPARATE "OPENING COST" FIELD
    ---------------------------------
    Deliberately absent. Every ledger movement values itself from the
    ingredient's already-configured cost_per_canonical_micro_cents at
    post time (same as goods receipts, wastage, and manual adjustments) —
    an ingredient created without a cost simply posts a R0 valuation, same
    as any other movement type would. Set the ingredient's cost via
    PATCH .../ingredients/{id} first if valuation at go-live matters.

    ONE OPENING BALANCE PER INGREDIENT, EVER — REJECTED ON A REPEAT
    ------------------------------------------------------------------
    An opening balance is by definition the stock position BEFORE any
    other movement existed. A second attempt for the same ingredient is
    ambiguous — did the first one get the quantity wrong, or did more
    stock genuinely arrive since? — and silently picking either
    interpretation (replace, or add) risks quietly corrupting the stock
    position. So a repeat attempt is REJECTED (409), not replaced and not
    additive: correct a mistake with a manual adjustment
    (POST .../adjustments, which is auditable and explicitly a
    correction), and record stock that actually arrived since with a
    goods receipt (POST .../goods-receipts) — both ledger-append,
    auditable, and unambiguous about what they mean, unlike a second
    "opening" balance would be.

    The rejection is enforced two ways: a pre-check by movement_type (which
    also catches an ingredient's opening balance already posted by the CSV
    ingredient importer, routes_import.py — that path has no idempotency
    key, only "the ingredient must be new", so it cannot be caught by the
    key below alone), and — for the genuine race between two concurrent
    submissions — the deterministic idempotency_key
    opening_balance_idempotency_key(ingredient_id), paired with the ledger's
    partial unique index (business_id, idempotency_key). The unique index
    is the actual backstop; the pre-check exists only to fail fast with a
    clear message in the non-racing, common case.
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

    quantity_milli = _convert(body.quantity, body.unit, ingredient.canonical_unit)
    if quantity_milli <= 0:
        raise AppError("INVALID_QUANTITY", "Opening stock must be greater than zero", 422)

    already = (
        await db.execute(
            select(StockMovement.id)
            .where(
                StockMovement.business_id == user.business_id,
                StockMovement.ingredient_id == ingredient.id,
                StockMovement.movement_type == StockMovementType.OPENING_BALANCE.value,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if already is not None:
        raise AppError(
            "OPENING_STOCK_ALREADY_SET",
            "Opening stock has already been recorded for this ingredient. "
            "Use a manual adjustment to correct it, or a goods receipt for "
            "stock received since.",
            409,
        )

    occurred = body.occurred_at or datetime.now(timezone.utc)
    unit_cost = int(ingredient.cost_per_canonical_micro_cents or 0)
    movement = build_movement(
        business_id=user.business_id,
        ingredient_id=ingredient.id,
        quantity_milli=quantity_milli,
        movement_type=StockMovementType.OPENING_BALANCE.value,
        unit_cost_micro_cents=unit_cost,
        source_type=StockSourceType.OPENING_STOCK.value,
        source_id=ingredient.id,
        created_by_user_id=user.user_id,
        idempotency_key=opening_balance_idempotency_key(ingredient.id),
        notes=body.source or "Opening stock",
        occurred_at=occurred,
    )
    db.add(movement)
    db.add(
        AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="inventory.stock.opening_balance_set",
            target_type="ingredient",
            target_id=ingredient.id,
            diff_json={
                "quantity_milli": quantity_milli,
                "entered": f"{body.quantity} {body.unit}",
                "source": body.source,
                "actor_role": user.role,
            },
        )
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not is_idempotency_violation(exc):
            raise
        raise AppError(
            "OPENING_STOCK_ALREADY_SET",
            "Opening stock has already been recorded for this ingredient "
            "(concurrent submission detected).",
            409,
        ) from exc

    return OpeningStockResponse(
        ingredient_id=ingredient.id,
        ingredient_name=ingredient.name,
        quantity_milli=quantity_milli,
        quantity_display=format_quantity(
            quantity_milli, CanonicalUnit(ingredient.canonical_unit), ingredient.unit_label
        ),
        value_cents=value_cents(quantity_milli, unit_cost),
        occurred_at=occurred,
    )


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
        "display": format_quantity(
            quantity_milli, CanonicalUnit(ingredient.canonical_unit), ingredient.unit_label
        ),
    }
