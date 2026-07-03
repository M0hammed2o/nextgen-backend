"""
Menu routes — CRUD for categories, items, and paid add-ons.
OWNER / MANAGER can manage. Items support soft delete.
Category delete is blocked when active items are still linked.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.menu import MenuAddOn, MenuCategory, MenuItem, menu_item_add_ons

router = APIRouter(prefix="/business/menu", tags=["menu"])


# ── Category Schemas ─────────────────────────────────────────────────────────

class CategoryCreate(BaseModel):
    name: str = Field(max_length=255)
    description: str | None = None
    sort_order: int = 0


class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class CategoryResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    sort_order: int
    is_active: bool
    model_config = {"from_attributes": True}


# ── Option Group Schemas (validation only — no pricing in Phase 5A) ──────────

class OptionChoiceSchema(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    # Phase 8: signed price delta. 0 = free modifier. Positive = more expensive.
    # Negative is allowed (e.g. smaller size = cheaper).
    price_delta_cents: int = Field(default=0, description="Price change in cents (signed). 0 = no charge.")
    sort_order: int = Field(default=0, ge=0)
    is_enabled: bool = True


class OptionGroupSchema(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    required: bool = False
    min_selections: int = Field(default=0, ge=0)
    max_selections: int = Field(default=1, ge=1)
    sort_order: int = Field(default=0, ge=0)
    is_enabled: bool = True
    default_option_id: str | None = None
    options: list[OptionChoiceSchema] = Field(min_length=1)

    @model_validator(mode="after")
    def check_selections_and_default(self) -> "OptionGroupSchema":
        if self.min_selections > self.max_selections:
            raise ValueError(
                f"Group '{self.name}': min_selections ({self.min_selections}) "
                f"cannot exceed max_selections ({self.max_selections})"
            )
        if self.required and self.min_selections == 0:
            self.min_selections = 1
        if self.default_option_id is not None:
            ids = {o.id for o in self.options}
            if self.default_option_id not in ids:
                raise ValueError(
                    f"Group '{self.name}': default_option_id '{self.default_option_id}' "
                    "is not present in options"
                )
        return self


class MenuItemOptionsSchema(BaseModel):
    option_groups: list[OptionGroupSchema] = Field(min_length=1)


def _validate_options_json(options_json: dict | None) -> None:
    """Raise ValueError if options_json does not match the expected schema."""
    if not options_json:
        return
    try:
        MenuItemOptionsSchema.model_validate(options_json)
    except ValidationError as exc:
        raise ValueError(f"Invalid options_json: {exc.errors(include_url=False)}") from exc


# ── Item Schemas ─────────────────────────────────────────────────────────────

class ItemCreate(BaseModel):
    category_id: uuid.UUID | None = None
    name: str = Field(max_length=255)
    description: str | None = None
    price_cents: int = Field(ge=0)
    options_json: dict | None = None
    sort_order: int = 0
    image_asset_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_options(self) -> "ItemCreate":
        _validate_options_json(self.options_json)
        return self


class ItemUpdate(BaseModel):
    category_id: uuid.UUID | None = None
    name: str | None = None
    description: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    options_json: dict | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    image_asset_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_options(self) -> "ItemUpdate":
        _validate_options_json(self.options_json)
        return self


class AddOnResponse(BaseModel):
    id: uuid.UUID
    name: str
    price_cents: int
    min_qty: int
    max_qty: int
    default_qty: int
    is_active: bool
    sort_order: int
    model_config = {"from_attributes": True}


class ItemResponse(BaseModel):
    id: uuid.UUID
    category_id: uuid.UUID | None
    name: str
    description: str | None
    price_cents: int
    currency: str
    options_json: dict | None
    add_ons: list[AddOnResponse] = []
    is_active: bool
    sort_order: int
    image_url: str | None
    model_config = {"from_attributes": True}


# ── Category Routes ──────────────────────────────────────────────────────────

@router.get("/categories", response_model=list[CategoryResponse])
async def list_categories(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MenuCategory)
        .where(MenuCategory.business_id == user.business_id)
        .order_by(MenuCategory.sort_order, MenuCategory.name)
    )
    return [CategoryResponse.model_validate(c) for c in result.scalars().all()]


@router.post("/categories", response_model=CategoryResponse, status_code=201)
async def create_category(
    body: CategoryCreate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    cat = MenuCategory(business_id=user.business_id, **body.model_dump())
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return CategoryResponse.model_validate(cat)


@router.put("/categories/{category_id}", response_model=CategoryResponse)
async def update_category(
    category_id: uuid.UUID,
    body: CategoryUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MenuCategory).where(
            MenuCategory.id == category_id,
            MenuCategory.business_id == user.business_id,
        )
    )
    cat = result.scalar_one_or_none()
    if not cat:
        raise NotFoundError("Category", str(category_id))

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(cat, field, value)

    await db.commit()
    await db.refresh(cat)
    return CategoryResponse.model_validate(cat)


@router.delete("/categories/{category_id}", status_code=204)
async def delete_category(
    category_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Delete a category.

    Blocked if the category still has active (non-deleted) menu items.
    Remove or reassign the items first, then retry.
    """
    result = await db.execute(
        select(MenuCategory).where(
            MenuCategory.id == category_id,
            MenuCategory.business_id == user.business_id,
        )
    )
    cat = result.scalar_one_or_none()
    if not cat:
        raise NotFoundError("Category", str(category_id))

    # Count active items still linked to this category
    item_count_result = await db.execute(
        select(func.count(MenuItem.id)).where(
            MenuItem.category_id == category_id,
            MenuItem.business_id == user.business_id,
            MenuItem.is_deleted == False,
        )
    )
    active_item_count = item_count_result.scalar_one()

    if active_item_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete category '{cat.name}': it still has "
                f"{active_item_count} active item{'s' if active_item_count != 1 else ''}. "
                "Remove or reassign the items first."
            ),
        )

    await db.delete(cat)
    await db.commit()


# ── Item Routes ──────────────────────────────────────────────────────────────

@router.get("/items", response_model=list[ItemResponse])
async def list_items(
    category_id: uuid.UUID | None = None,
    include_inactive: bool = False,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(MenuItem)
        .where(
            MenuItem.business_id == user.business_id,
            MenuItem.is_deleted == False,
        )
    )
    if category_id:
        query = query.where(MenuItem.category_id == category_id)
    if not include_inactive:
        query = query.where(MenuItem.is_active == True)

    query = query.order_by(MenuItem.sort_order, MenuItem.name)
    result = await db.execute(query)
    return [ItemResponse.model_validate(item) for item in result.scalars().all()]


@router.post("/items", response_model=ItemResponse, status_code=201)
async def create_item(
    body: ItemCreate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    item = MenuItem(
        business_id=user.business_id,
        currency=( # Inherit from business
            await db.execute(
                select(MenuItem.currency).limit(1)
            )
        ).scalar() or "ZAR",
        **body.model_dump(),
    )
    # Actually get currency from business
    from shared.models.business import Business
    biz = await db.get(Business, user.business_id)
    item.currency = biz.currency if biz else "ZAR"

    db.add(item)
    await db.commit()
    await db.refresh(item)
    return ItemResponse.model_validate(item)


@router.put("/items/{item_id}", response_model=ItemResponse)
async def update_item(
    item_id: uuid.UUID,
    body: ItemUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MenuItem).where(
            MenuItem.id == item_id,
            MenuItem.business_id == user.business_id,
            MenuItem.is_deleted == False,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise NotFoundError("MenuItem", str(item_id))

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value)

    await db.commit()
    await db.refresh(item)
    return ItemResponse.model_validate(item)


@router.delete("/items/{item_id}", status_code=204)
async def soft_delete_item(
    item_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Soft delete — sets is_deleted=True to preserve order history."""
    result = await db.execute(
        select(MenuItem).where(
            MenuItem.id == item_id,
            MenuItem.business_id == user.business_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise NotFoundError("MenuItem", str(item_id))

    item.is_deleted = True
    item.is_active = False
    await db.commit()


# ── Add-on Schemas ────────────────────────────────────────────────────────────

class AddOnCreate(BaseModel):
    name: str = Field(max_length=255)
    price_cents: int = Field(ge=0)
    min_qty: int = Field(default=0, ge=0)
    max_qty: int = Field(default=10, ge=1)
    default_qty: int = Field(default=1, ge=0)
    sort_order: int = 0

    @model_validator(mode="after")
    def check_qty_range(self) -> "AddOnCreate":
        if self.min_qty > self.max_qty:
            raise ValueError("min_qty cannot exceed max_qty")
        if self.default_qty < self.min_qty or self.default_qty > self.max_qty:
            raise ValueError("default_qty must be between min_qty and max_qty")
        return self


class AddOnUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    price_cents: int | None = Field(default=None, ge=0)
    min_qty: int | None = Field(default=None, ge=0)
    max_qty: int | None = Field(default=None, ge=1)
    default_qty: int | None = Field(default=None, ge=0)
    is_active: bool | None = None
    sort_order: int | None = None


# ── Add-on Routes ─────────────────────────────────────────────────────────────

@router.get("/add-ons", response_model=list[AddOnResponse])
async def list_add_ons(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MenuAddOn)
        .where(
            MenuAddOn.business_id == user.business_id,
            MenuAddOn.is_deleted == False,
        )
        .order_by(MenuAddOn.sort_order, MenuAddOn.name)
    )
    return [AddOnResponse.model_validate(a) for a in result.scalars().all()]


@router.post("/add-ons", response_model=AddOnResponse, status_code=201)
async def create_add_on(
    body: AddOnCreate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    add_on = MenuAddOn(business_id=user.business_id, **body.model_dump())
    db.add(add_on)
    await db.commit()
    await db.refresh(add_on)
    return AddOnResponse.model_validate(add_on)


@router.put("/add-ons/{add_on_id}", response_model=AddOnResponse)
async def update_add_on(
    add_on_id: uuid.UUID,
    body: AddOnUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MenuAddOn).where(
            MenuAddOn.id == add_on_id,
            MenuAddOn.business_id == user.business_id,
            MenuAddOn.is_deleted == False,
        )
    )
    add_on = result.scalar_one_or_none()
    if not add_on:
        raise NotFoundError("MenuAddOn", str(add_on_id))
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(add_on, field, value)
    await db.commit()
    await db.refresh(add_on)
    return AddOnResponse.model_validate(add_on)


@router.delete("/add-ons/{add_on_id}", status_code=204)
async def delete_add_on(
    add_on_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Soft delete — preserves order history references."""
    result = await db.execute(
        select(MenuAddOn).where(
            MenuAddOn.id == add_on_id,
            MenuAddOn.business_id == user.business_id,
        )
    )
    add_on = result.scalar_one_or_none()
    if not add_on:
        raise NotFoundError("MenuAddOn", str(add_on_id))
    add_on.is_deleted = True
    add_on.is_active = False
    await db.commit()


@router.post("/items/{item_id}/add-ons/{add_on_id}", status_code=204)
async def attach_add_on(
    item_id: uuid.UUID,
    add_on_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Attach an add-on to a menu item (idempotent)."""
    # Verify ownership of both item and add-on
    item_res = await db.execute(
        select(MenuItem).where(
            MenuItem.id == item_id,
            MenuItem.business_id == user.business_id,
            MenuItem.is_deleted == False,
        )
    )
    if not item_res.scalar_one_or_none():
        raise NotFoundError("MenuItem", str(item_id))

    ao_res = await db.execute(
        select(MenuAddOn).where(
            MenuAddOn.id == add_on_id,
            MenuAddOn.business_id == user.business_id,
            MenuAddOn.is_deleted == False,
        )
    )
    if not ao_res.scalar_one_or_none():
        raise NotFoundError("MenuAddOn", str(add_on_id))

    # Insert into join table — ignore if already exists
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(menu_item_add_ons).values(
        menu_item_id=item_id, add_on_id=add_on_id
    ).on_conflict_do_nothing()
    await db.execute(stmt)
    await db.commit()


@router.delete("/items/{item_id}/add-ons/{add_on_id}", status_code=204)
async def detach_add_on(
    item_id: uuid.UUID,
    add_on_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Detach an add-on from a menu item."""
    from sqlalchemy import delete as _delete
    await db.execute(
        _delete(menu_item_add_ons).where(
            menu_item_add_ons.c.menu_item_id == item_id,
            menu_item_add_ons.c.add_on_id == add_on_id,
        )
    )
    await db.commit()
