"""
Specials routes — CRUD with scheduling and day-of-week rules.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.specials import Special

router = APIRouter(prefix="/business/specials", tags=["specials"])


class SpecialCreate(BaseModel):
    title: str = Field(max_length=255)
    description: str | None = None
    days_of_week: list[str] | None = None  # ["mon", "wed"]
    start_at: datetime | None = None
    end_at: datetime | None = None
    rule_json: dict | None = None
    sort_order: int = 0
    image_asset_id: uuid.UUID | None = None


class SpecialUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    days_of_week: list[str] | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    rule_json: dict | None = None
    is_active: bool | None = None
    sort_order: int | None = None
    image_asset_id: uuid.UUID | None = None


class SpecialResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    days_of_week: list[str] | None
    start_at: datetime | None
    end_at: datetime | None
    rule_json: dict | None
    is_active: bool
    sort_order: int
    image_url: str | None
    created_at: datetime
    model_config = {"from_attributes": True}


@router.get("", response_model=list[SpecialResponse])
async def list_specials(
    active_only: bool = True,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    query = select(Special).where(Special.business_id == user.business_id)
    if active_only:
        query = query.where(Special.is_active == True)
    query = query.order_by(Special.sort_order, Special.title)
    result = await db.execute(query)
    return [SpecialResponse.model_validate(s) for s in result.scalars().all()]


@router.post("", response_model=SpecialResponse, status_code=201)
async def create_special(
    body: SpecialCreate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    special = Special(business_id=user.business_id, **body.model_dump())
    db.add(special)
    await db.commit()
    await db.refresh(special)
    return SpecialResponse.model_validate(special)


@router.put("/{special_id}", response_model=SpecialResponse)
async def update_special(
    special_id: uuid.UUID,
    body: SpecialUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Special).where(Special.id == special_id, Special.business_id == user.business_id)
    )
    special = result.scalar_one_or_none()
    if not special:
        raise NotFoundError("Special", str(special_id))

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(special, field, value)
    await db.commit()
    await db.refresh(special)
    return SpecialResponse.model_validate(special)


@router.delete("/{special_id}", status_code=204)
async def delete_special(
    special_id: uuid.UUID,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Special).where(Special.id == special_id, Special.business_id == user.business_id)
    )
    special = result.scalar_one_or_none()
    if not special:
        raise NotFoundError("Special", str(special_id))
    await db.delete(special)
    await db.commit()
