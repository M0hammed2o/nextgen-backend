"""
Product variants (clothing / retail) - size x colour style options of a menu
item, each with its own SKU, barcode, optional price override and stock.

Stock reuses the inventory domain: every variant owns one ``Ingredient``
(unit UNIT) so stocktake, wastage, goods receipt and the append-only ledger
work per variant. Selling is done through the ordinary POS endpoints
(``variant_id`` on an order line) - pricing stays in shared/pricing/engine.py.

TENANT ISOLATION: every query filters on ``user.business_id`` (from the JWT).
No route accepts a business_id. A variant/item id from another business is
indistinguishable from a missing one (404).
"""

import itertools
import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import (
    AuthUser,
    require_owner_or_manager,
    require_pos_enabled,
    require_staff_or_above,
)
from backend.app.db.session import get_db
from backend.app.inventory.ledger import (
    build_movement,
    opening_balance_idempotency_key,
    stock_on_hand,
)
from backend.app.inventory.units import cost_per_canonical_micro_cents
from shared.enums import AuditScope, StockMovementType, StockSourceType
from shared.models.audit import AuditEvent
from shared.models.inventory import Ingredient
from shared.models.menu import MenuItem
from shared.models.variant import ProductVariant, variant_label

router = APIRouter(prefix="/business", tags=["variants"])

MAX_MATRIX = 200


# -- Schemas -------------------------------------------------------------------

def _clean(value: str, field: str, max_len: int) -> str:
    v = (value or "").strip()
    if not v:
        raise ValueError(f"{field} must not be blank")
    if len(v) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters")
    return v


def _check_attributes(attrs: dict) -> dict:
    if not attrs or len(attrs) > 8:
        raise ValueError("attributes must have between 1 and 8 entries")
    out = {}
    for k, v in attrs.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("attributes keys and values must be strings")
        out[_clean(k, "attribute name", 32)] = _clean(v, "attribute value", 64)
    return out


class VariantCreate(BaseModel):
    sku: str
    barcode: str | None = None
    attributes: dict[str, str]
    price_cents: int | None = Field(default=None, ge=0, description="Override; omit = item base price")
    cost_cents: int | None = Field(default=None, ge=0, description="Unit cost, for stock valuation")
    sort_order: int = 0
    track_stock: bool = True
    opening_stock: int | None = Field(default=None, ge=0, le=1_000_000, description="Units on hand now")

    @field_validator("sku")
    @classmethod
    def _sku(cls, v):
        return _clean(v, "sku", 64)

    @field_validator("barcode")
    @classmethod
    def _barcode(cls, v):
        return None if v is None else _clean(v, "barcode", 64)

    @field_validator("attributes")
    @classmethod
    def _attrs(cls, v):
        return _check_attributes(v)

    @model_validator(mode="after")
    def _stock(self):
        if self.opening_stock and not self.track_stock:
            raise ValueError("opening_stock requires track_stock")
        return self


class VariantUpdate(BaseModel):
    sku: str | None = None
    barcode: str | None = None  # explicit null clears it
    attributes: dict[str, str] | None = None
    price_cents: int | None = Field(default=None, ge=0)  # explicit null = use item base price
    cost_cents: int | None = Field(default=None, ge=0)
    sort_order: int | None = None
    is_active: bool | None = None

    @field_validator("sku")
    @classmethod
    def _sku(cls, v):
        return None if v is None else _clean(v, "sku", 64)

    @field_validator("barcode")
    @classmethod
    def _barcode(cls, v):
        return None if v is None else _clean(v, "barcode", 64)

    @field_validator("attributes")
    @classmethod
    def _attrs(cls, v):
        return None if v is None else _check_attributes(v)


class MatrixCreate(BaseModel):
    sku_prefix: str
    sizes: list[str] = Field(default_factory=list)
    colours: list[str] = Field(default_factory=list)
    price_cents: int | None = Field(default=None, ge=0)
    cost_cents: int | None = Field(default=None, ge=0)
    track_stock: bool = True
    opening_stock: int | None = Field(default=None, ge=0, le=1_000_000, description="Per variant")

    @field_validator("sku_prefix")
    @classmethod
    def _prefix(cls, v):
        return _clean(v, "sku_prefix", 40)

    @field_validator("sizes", "colours")
    @classmethod
    def _axis(cls, v):
        cleaned = [_clean(x, "value", 32) for x in v]
        if len({x.lower() for x in cleaned}) != len(cleaned):
            raise ValueError("duplicate values")
        return cleaned

    @model_validator(mode="after")
    def _shape(self):
        if not self.sizes and not self.colours:
            raise ValueError("give at least one size or colour")
        if max(len(self.sizes), 1) * max(len(self.colours), 1) > MAX_MATRIX:
            raise ValueError(f"at most {MAX_MATRIX} variants per call")
        if self.opening_stock and not self.track_stock:
            raise ValueError("opening_stock requires track_stock")
        return self


class VariantResponse(BaseModel):
    id: uuid.UUID
    menu_item_id: uuid.UUID
    sku: str
    barcode: str | None
    attributes: dict
    label: str
    price_cents: int | None
    effective_price_cents: int
    cost_cents: int | None = None
    is_active: bool
    sort_order: int
    ingredient_id: uuid.UUID | None
    stock_on_hand: int | None = Field(default=None, description="Units; null when stock is not tracked")


class LookupResponse(BaseModel):
    menu_item_id: uuid.UUID
    menu_item_name: str
    base_price_cents: int
    variant: VariantResponse


# -- Helpers -------------------------------------------------------------------

async def _load_item(db: AsyncSession, business_id: uuid.UUID, item_id: uuid.UUID) -> MenuItem:
    item = (await db.execute(
        select(MenuItem).where(
            MenuItem.id == item_id, MenuItem.business_id == business_id, MenuItem.is_deleted.is_(False),
        )
    )).scalar_one_or_none()
    if not item:
        raise NotFoundError("Menu item", str(item_id))
    return item


async def _load_variant(db: AsyncSession, business_id: uuid.UUID, variant_id: uuid.UUID) -> ProductVariant:
    v = (await db.execute(
        select(ProductVariant).where(ProductVariant.id == variant_id, ProductVariant.business_id == business_id)
    )).scalar_one_or_none()
    if not v:
        raise NotFoundError("Variant", str(variant_id))
    return v


async def _assert_free(db, business_id, *, skus=(), barcodes=(), exclude_id=None) -> None:
    """Pre-check so the caller gets a clear 409; the DB unique indexes remain the real guard."""
    if skus:
        q = select(ProductVariant.sku).where(
            ProductVariant.business_id == business_id, ProductVariant.sku.in_(list(skus)))
        if exclude_id:
            q = q.where(ProductVariant.id != exclude_id)
        taken = [r[0] for r in (await db.execute(q)).all()]
        if taken:
            raise AppError("SKU_TAKEN", "SKU already in use in this business", 409, details={"skus": taken})
    if barcodes:
        q = select(ProductVariant.barcode).where(
            ProductVariant.business_id == business_id, ProductVariant.barcode.in_(list(barcodes)))
        if exclude_id:
            q = q.where(ProductVariant.id != exclude_id)
        taken = [r[0] for r in (await db.execute(q)).all()]
        if taken:
            raise AppError("BARCODE_TAKEN", "Barcode already in use in this business", 409,
                           details={"barcodes": taken})


def _ingredient_cost(cost_cents: int | None) -> tuple[int | None, int]:
    if cost_cents is None:
        return None, 0
    return cost_cents, cost_per_canonical_micro_cents(cost_cents, 1000)  # pack of exactly 1 each


def _make_stock_ingredient(business_id, item: MenuItem, sku: str, attributes: dict, cost_cents) -> Ingredient:
    pack_cost, micro = _ingredient_cost(cost_cents)
    name = f"{item.name[:150]} - {variant_label(attributes)} [{sku}]"[:255]
    return Ingredient(
        business_id=business_id, name=name, sku=sku[:64], canonical_unit="UNIT", count_method="UNIT_COUNT",
        unit_label="pc", category="Retail stock", purchase_unit_name="each", purchase_pack_milli=1000,
        cost_per_pack_cents=pack_cost, cost_per_canonical_micro_cents=micro,
    )


def _opening_movement(user: AuthUser, ingredient: Ingredient, units: int):
    return build_movement(
        business_id=user.business_id, ingredient_id=ingredient.id, quantity_milli=units * 1000,
        movement_type=StockMovementType.OPENING_BALANCE.value,
        unit_cost_micro_cents=int(ingredient.cost_per_canonical_micro_cents or 0),
        source_type=StockSourceType.OPENING_STOCK.value, source_id=ingredient.id,
        created_by_user_id=user.user_id, idempotency_key=opening_balance_idempotency_key(ingredient.id),
        notes="Opening stock (variant created)",
    )


async def _responses(db, business_id, item: MenuItem, variants: list[ProductVariant]) -> list[VariantResponse]:
    ing_ids = [v.ingredient_id for v in variants if v.ingredient_id]
    stock = await stock_on_hand(db, business_id, ingredient_ids=ing_ids) if ing_ids else {}
    costs = {}
    if ing_ids:
        rows = (await db.execute(
            select(Ingredient.id, Ingredient.cost_per_pack_cents).where(
                Ingredient.business_id == business_id, Ingredient.id.in_(ing_ids))
        )).all()
        costs = {r[0]: r[1] for r in rows}
    return [
        VariantResponse(
            id=v.id, menu_item_id=v.menu_item_id, sku=v.sku, barcode=v.barcode, attributes=v.attributes or {},
            label=variant_label(v.attributes), price_cents=v.price_cents,
            effective_price_cents=v.price_cents if v.price_cents is not None else item.price_cents,
            cost_cents=costs.get(v.ingredient_id) if v.ingredient_id else None,
            is_active=v.is_active, sort_order=v.sort_order, ingredient_id=v.ingredient_id,
            stock_on_hand=(int(stock.get(v.ingredient_id, 0)) // 1000) if v.ingredient_id else None,
        )
        for v in variants
    ]


def _audit(user, action, target_id, diff):
    return AuditEvent(
        scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
        action=action, target_type="product_variant", target_id=target_id, diff_json=diff,
    )


# -- Routes --------------------------------------------------------------------

@router.get("/menu/items/{item_id}/variants", response_model=list[VariantResponse])
async def list_variants(
    item_id: uuid.UUID,
    include_inactive: bool = Query(default=False),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    item = await _load_item(db, user.business_id, item_id)
    q = select(ProductVariant).where(
        ProductVariant.business_id == user.business_id, ProductVariant.menu_item_id == item.id,
    ).order_by(ProductVariant.sort_order, ProductVariant.created_at)
    if not include_inactive:
        q = q.where(ProductVariant.is_active.is_(True))
    return await _responses(db, user.business_id, item, list((await db.execute(q)).scalars().all()))


@router.post("/menu/items/{item_id}/variants", response_model=VariantResponse, status_code=201)
async def create_variant(
    item_id: uuid.UUID, body: VariantCreate,
    user: AuthUser = Depends(require_owner_or_manager), db: AsyncSession = Depends(get_db),
):
    item = await _load_item(db, user.business_id, item_id)
    await _assert_free(db, user.business_id, skus=[body.sku], barcodes=[body.barcode] if body.barcode else [])
    try:
        ing = None
        if body.track_stock:
            ing = _make_stock_ingredient(user.business_id, item, body.sku, body.attributes, body.cost_cents)
            db.add(ing)
            await db.flush()
        variant = ProductVariant(
            business_id=user.business_id, menu_item_id=item.id, sku=body.sku, barcode=body.barcode,
            attributes=body.attributes, price_cents=body.price_cents, sort_order=body.sort_order,
            ingredient_id=ing.id if ing else None,
        )
        db.add(variant)
        await db.flush()
        if ing is not None and body.opening_stock:
            db.add(_opening_movement(user, ing, body.opening_stock))
        db.add(_audit(user, "variant.created", variant.id, {"sku": body.sku, "attributes": body.attributes}))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError("VARIANT_CONFLICT", "SKU, barcode or stock name already in use", 409)
    return (await _responses(db, user.business_id, item, [variant]))[0]


@router.post("/menu/items/{item_id}/variants/matrix", response_model=list[VariantResponse], status_code=201)
async def create_variant_matrix(
    item_id: uuid.UUID, body: MatrixCreate,
    user: AuthUser = Depends(require_owner_or_manager), db: AsyncSession = Depends(get_db),
):
    """
    sizes x colours in one call. SKU = <sku_prefix>-<SIZE>-<COLOUR> (upper-cased,
    spaces removed). All-or-nothing: any SKU clash rejects the whole request.
    """
    item = await _load_item(db, user.business_id, item_id)
    combos = list(itertools.product(body.sizes or [None], body.colours or [None]))
    planned = []
    for order, (size, colour) in enumerate(combos):
        attrs = {}
        if size:
            attrs["size"] = size
        if colour:
            attrs["colour"] = colour
        parts = [body.sku_prefix] + [p for p in (size, colour) if p]
        sku = "-".join(p.replace(" ", "").upper() for p in parts)[:64]
        planned.append((sku, attrs, order))
    if len({p[0] for p in planned}) != len(planned):
        raise AppError("SKU_PATTERN_COLLISION", "Generated SKUs are not unique - use distinct sizes/colours", 422)
    await _assert_free(db, user.business_id, skus=[p[0] for p in planned])
    created = []
    try:
        for sku, attrs, order in planned:
            ing = None
            if body.track_stock:
                ing = _make_stock_ingredient(user.business_id, item, sku, attrs, body.cost_cents)
                db.add(ing)
                await db.flush()
            v = ProductVariant(
                business_id=user.business_id, menu_item_id=item.id, sku=sku, attributes=attrs,
                price_cents=body.price_cents, sort_order=order, ingredient_id=ing.id if ing else None,
            )
            db.add(v)
            await db.flush()
            if ing is not None and body.opening_stock:
                db.add(_opening_movement(user, ing, body.opening_stock))
            created.append(v)
        db.add(_audit(user, "variant.matrix_created", item.id,
                      {"count": len(created), "skus": [p[0] for p in planned]}))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError("VARIANT_CONFLICT", "SKU or stock name already in use", 409)
    return await _responses(db, user.business_id, item, created)


@router.patch("/menu/variants/{variant_id}", response_model=VariantResponse)
async def update_variant(
    variant_id: uuid.UUID, body: VariantUpdate,
    user: AuthUser = Depends(require_owner_or_manager), db: AsyncSession = Depends(get_db),
):
    variant = await _load_variant(db, user.business_id, variant_id)
    item = await _load_item(db, user.business_id, variant.menu_item_id)
    sent = body.model_fields_set
    await _assert_free(
        db, user.business_id,
        skus=[body.sku] if body.sku and body.sku != variant.sku else [],
        barcodes=[body.barcode] if body.barcode and body.barcode != variant.barcode else [],
        exclude_id=variant.id,
    )
    if body.sku is not None:
        variant.sku = body.sku
    if "barcode" in sent:
        variant.barcode = body.barcode
    if body.attributes is not None:
        variant.attributes = body.attributes
    if "price_cents" in sent:
        variant.price_cents = body.price_cents
    if body.sort_order is not None:
        variant.sort_order = body.sort_order
    if body.is_active is not None:
        variant.is_active = body.is_active
    if "cost_cents" in sent and variant.ingredient_id:
        ing = (await db.execute(select(Ingredient).where(
            Ingredient.id == variant.ingredient_id, Ingredient.business_id == user.business_id,
        ))).scalar_one_or_none()
        if ing:
            ing.cost_per_pack_cents, ing.cost_per_canonical_micro_cents = _ingredient_cost(body.cost_cents)
    db.add(_audit(user, "variant.updated", variant.id, body.model_dump(exclude_unset=True)))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise AppError("VARIANT_CONFLICT", "SKU or barcode already in use", 409)
    return (await _responses(db, user.business_id, item, [variant]))[0]


@router.delete("/menu/variants/{variant_id}", response_model=VariantResponse)
async def deactivate_variant(
    variant_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager), db: AsyncSession = Depends(get_db),
):
    """Soft: the variant stops being sellable; order history, SKU and stock ledger stay intact."""
    variant = await _load_variant(db, user.business_id, variant_id)
    item = await _load_item(db, user.business_id, variant.menu_item_id)
    variant.is_active = False
    db.add(_audit(user, "variant.deactivated", variant.id, {"sku": variant.sku}))
    await db.commit()
    return (await _responses(db, user.business_id, item, [variant]))[0]


@router.get("/pos/lookup", response_model=LookupResponse, dependencies=[Depends(require_pos_enabled)])
async def pos_lookup(
    barcode: str | None = Query(default=None, max_length=64),
    sku: str | None = Query(default=None, max_length=64),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Scan a barcode (or type a SKU) at the till -> the item and the exact variant. Active only."""
    if bool(barcode) == bool(sku):
        raise AppError("LOOKUP_KEY_REQUIRED", "Provide exactly one of barcode or sku", 422)
    q = select(ProductVariant, MenuItem).join(MenuItem, MenuItem.id == ProductVariant.menu_item_id).where(
        ProductVariant.business_id == user.business_id,
        MenuItem.business_id == user.business_id,
        ProductVariant.is_active.is_(True),
        MenuItem.is_active.is_(True),
        MenuItem.is_deleted.is_(False),
    )
    q = q.where(ProductVariant.barcode == barcode.strip()) if barcode else q.where(ProductVariant.sku == sku.strip())
    row = (await db.execute(q)).first()
    if not row:
        raise NotFoundError("Variant", (barcode or sku))
    variant, item = row
    return LookupResponse(
        menu_item_id=item.id, menu_item_name=item.name, base_price_cents=item.price_cents,
        variant=(await _responses(db, user.business_id, item, [variant]))[0],
    )
