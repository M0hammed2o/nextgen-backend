"""
Multi-page menu images — ordered, named pages a business can show in the
Manager Dashboard (e.g. "Regular Menu — Page 1/2", "Secret Creations").

Purely additive alongside the existing single `businesses.menu_image_url`
field: that field, and the WhatsApp bot code path that reads it
(pipeline.py), are not touched by this module at all. A business with no
rows here behaves exactly as it always has.

Upload flow reuses the existing generic asset pipeline unchanged:
  1. POST /business/assets/upload-url  (kind=BUSINESS_MENU_IMAGE)       -- existing
  2. Frontend PUTs the file to the returned upload_url                 -- existing
  3. POST /business/menu-images        {asset_id, name}                -- new, this module
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_assets import asset_public_url
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.asset import Asset
from shared.models.menu_image import BusinessMenuImage

router = APIRouter(prefix="/business/menu-images", tags=["menu-images"])

MAX_PAGES_PER_BUSINESS = 20  # sanity ceiling; no product requirement names a number


# ── Schemas ──────────────────────────────────────────────────────────────────

class MenuImageResponse(BaseModel):
    id: uuid.UUID
    name: str
    sort_order: int
    is_active: bool
    image_url: str | None
    asset_id: uuid.UUID

    model_config = {"from_attributes": True}


class MenuImageCreate(BaseModel):
    asset_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)


class MenuImageUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    asset_id: uuid.UUID | None = None
    is_active: bool | None = None


class MenuImageReorderEntry(BaseModel):
    id: uuid.UUID
    sort_order: int


class MenuImageReorderRequest(BaseModel):
    order: list[MenuImageReorderEntry]


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _verify_owned_asset(db: AsyncSession, asset_id: uuid.UUID, business_id: uuid.UUID) -> Asset:
    """
    Re-check the asset belongs to THIS business before linking it, the same
    way every other asset_id -> entity attachment in this codebase does
    (see routes_menu.py's image_asset_id handling). A client-supplied
    asset_id for another tenant's asset must never be linkable here.
    """
    result = await db.execute(
        select(Asset).where(Asset.id == asset_id, Asset.business_id == business_id)
    )
    asset = result.scalar_one_or_none()
    if not asset:
        raise NotFoundError("Asset", str(asset_id))
    return asset


async def _to_response(row: BusinessMenuImage, asset: Asset | None) -> MenuImageResponse:
    return MenuImageResponse(
        id=row.id,
        name=row.name,
        sort_order=row.sort_order,
        is_active=row.is_active,
        asset_id=row.asset_id,
        image_url=asset_public_url(asset.storage_path) if asset else None,
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=list[MenuImageResponse])
async def list_menu_images(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """All menu-image pages for this business, ordered for display. Inactive
    pages are included (dashboard needs to show/restore them) — the
    frontend decides whether to render is_active=false differently."""
    result = await db.execute(
        select(BusinessMenuImage)
        .where(BusinessMenuImage.business_id == user.business_id)
        .order_by(BusinessMenuImage.sort_order, BusinessMenuImage.created_at)
    )
    rows = list(result.scalars().all())
    if not rows:
        return []

    asset_ids = [r.asset_id for r in rows]
    asset_result = await db.execute(
        select(Asset).where(Asset.id.in_(asset_ids), Asset.business_id == user.business_id)
    )
    assets_by_id = {a.id: a for a in asset_result.scalars().all()}
    return [await _to_response(r, assets_by_id.get(r.asset_id)) for r in rows]


@router.post("", response_model=MenuImageResponse, status_code=201)
async def create_menu_image(
    body: MenuImageCreate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    asset = await _verify_owned_asset(db, body.asset_id, user.business_id)

    count_result = await db.execute(
        select(BusinessMenuImage).where(BusinessMenuImage.business_id == user.business_id)
    )
    existing = list(count_result.scalars().all())
    if len(existing) >= MAX_PAGES_PER_BUSINESS:
        raise AppError(
            "TOO_MANY_PAGES",
            f"Maximum {MAX_PAGES_PER_BUSINESS} menu-image pages per business",
            status_code=422,
        )
    next_sort_order = (max((r.sort_order for r in existing), default=-1)) + 1

    row = BusinessMenuImage(
        business_id=user.business_id,
        asset_id=asset.id,
        name=body.name,
        sort_order=next_sort_order,
        is_active=True,
    )
    db.add(row)
    await db.commit()
    return await _to_response(row, asset)


@router.put("/{page_id}", response_model=MenuImageResponse)
async def update_menu_image(
    page_id: uuid.UUID,
    body: MenuImageUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Rename, replace the underlying image (new asset_id — e.g. after
    re-uploading a corrected photo), or toggle active/inactive."""
    result = await db.execute(
        select(BusinessMenuImage).where(
            BusinessMenuImage.id == page_id,
            BusinessMenuImage.business_id == user.business_id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise NotFoundError("BusinessMenuImage", str(page_id))

    asset: Asset | None = None
    if body.asset_id is not None:
        asset = await _verify_owned_asset(db, body.asset_id, user.business_id)
        row.asset_id = asset.id
    if body.name is not None:
        row.name = body.name
    if body.is_active is not None:
        row.is_active = body.is_active

    await db.commit()
    if asset is None:
        asset_result = await db.execute(
            select(Asset).where(Asset.id == row.asset_id, Asset.business_id == user.business_id)
        )
        asset = asset_result.scalar_one_or_none()
    return await _to_response(row, asset)


@router.post("/reorder", response_model=list[MenuImageResponse])
async def reorder_menu_images(
    body: MenuImageReorderRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-set sort_order from a full new ordering the dashboard sends
    after a drag-and-drop reorder. Entries for pages outside this business
    are silently ignored rather than erroring the whole batch -- the
    client only ever sends ids it was shown, scoped by the list endpoint
    above, so a mismatch here would mean stale client state, not a real
    cross-tenant attempt worth hard-failing the request over."""
    ids = [e.id for e in body.order]
    result = await db.execute(
        select(BusinessMenuImage).where(
            BusinessMenuImage.id.in_(ids),
            BusinessMenuImage.business_id == user.business_id,
        )
    )
    owned_ids = {r.id for r in result.scalars().all()}

    for entry in body.order:
        if entry.id not in owned_ids:
            continue
        await db.execute(
            sa_update(BusinessMenuImage)
            .where(
                BusinessMenuImage.id == entry.id,
                BusinessMenuImage.business_id == user.business_id,
            )
            .values(sort_order=entry.sort_order)
        )
    await db.commit()

    final = await db.execute(
        select(BusinessMenuImage)
        .where(BusinessMenuImage.business_id == user.business_id)
        .order_by(BusinessMenuImage.sort_order, BusinessMenuImage.created_at)
    )
    rows = list(final.scalars().all())
    asset_result = await db.execute(
        select(Asset).where(
            Asset.id.in_([r.asset_id for r in rows]), Asset.business_id == user.business_id
        )
    )
    assets_by_id = {a.id: a for a in asset_result.scalars().all()}
    return [await _to_response(r, assets_by_id.get(r.asset_id)) for r in rows]


@router.delete("/{page_id}", status_code=204)
async def delete_menu_image(
    page_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Removes the page row only. Does NOT delete the underlying Asset or
    its Supabase Storage object -- same deliberate split as every other
    entity-detach-from-asset path in this codebase (see
    routes_assets.py::delete_asset's own docstring); the owner can still
    hard-delete the asset itself via DELETE /business/assets/{asset_id}
    if they want the file gone too."""
    result = await db.execute(
        select(BusinessMenuImage).where(
            BusinessMenuImage.id == page_id,
            BusinessMenuImage.business_id == user.business_id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise NotFoundError("BusinessMenuImage", str(page_id))
    await db.delete(row)
    await db.commit()
