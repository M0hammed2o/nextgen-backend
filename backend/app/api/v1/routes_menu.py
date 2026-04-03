"""
Menu routes — CRUD for categories and items.
OWNER / MANAGER can manage. Items support soft delete.
Category delete is blocked when active items are still linked.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.menu import MenuCategory, MenuItem

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


# ── Item Schemas ─────────────────────────────────────────────────────────────

class ItemCreate(BaseModel):
    category_id: uuid.UUID | None = None
    name: str = Field(max_length=255)
    description: str | None = None
    price_cents: int = Field(ge=0)
    options_json: dict | None = None
    sort_order: int = 0
    image_asset_id: uuid.UUID | None = None


class ItemUpdate(BaseModel):
    category_id: uuid.UUID | None = None
    name: str | None = None
    description: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    options_json: dict | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    image_asset_id: uuid.UUID | None = None


class ItemResponse(BaseModel):
    id: uuid.UUID
    category_id: uuid.UUID | None
    name: str
    description: str | None
    price_cents: int
    currency: str
    options_json: dict | None
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
