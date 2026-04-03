"""
Business settings routes — GET/PUT /business/settings
Only accessible by OWNER or MANAGER.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.business import Business

router = APIRouter(prefix="/business", tags=["business"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class BusinessSettingsResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    business_code: str
    timezone: str
    business_hours: dict | None
    greeting_text: str | None
    fallback_text: str | None
    closed_text: str | None
    order_in_only: bool
    delivery_enabled: bool
    delivery_fee_cents: int
    require_customer_name: bool
    require_phone_number: bool
    require_delivery_address: bool
    currency: str
    plan: str
    billing_status: str
    daily_message_limit: int
    daily_llm_call_limit: int
    daily_order_limit: int
    address: str | None
    phone: str | None
    menu_image_url: str | None

    model_config = {"from_attributes": True}


class BusinessSettingsUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    business_hours: dict | None = None
    greeting_text: str | None = None
    fallback_text: str | None = None
    closed_text: str | None = None
    order_in_only: bool | None = None
    delivery_enabled: bool | None = None
    delivery_fee_cents: int | None = Field(default=None, ge=0)
    require_customer_name: bool | None = None
    require_phone_number: bool | None = None
    require_delivery_address: bool | None = None
    address: str | None = None
    phone: str | None = None
    menu_image_url: str | None = None


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("/settings", response_model=BusinessSettingsResponse)
async def get_business_settings(
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Get current business settings."""
    result = await db.execute(
        select(Business).where(Business.id == user.business_id)
    )
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    return BusinessSettingsResponse.model_validate(business)


@router.put("/settings", response_model=BusinessSettingsResponse)
async def update_business_settings(
    body: BusinessSettingsUpdate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Update business settings (partial update — only provided fields)."""
    result = await db.execute(
        select(Business).where(Business.id == user.business_id)
    )
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(business, field, value)

    await db.commit()
    await db.refresh(business)
    return BusinessSettingsResponse.model_validate(business)
