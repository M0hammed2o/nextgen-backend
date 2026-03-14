"""
Admin business management — CRUD, suspend/unsuspend, set limits.
Only accessible by SUPER_ADMIN.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError, DuplicateError, NotFoundError
from backend.app.core.rbac import AuthUser, require_super_admin
from backend.app.db.session import get_db
from shared.models.business import Business
from shared.utils import generate_business_code

router = APIRouter(prefix="/admin/businesses", tags=["admin-businesses"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class BusinessCreate(BaseModel):
    name: str = Field(max_length=255)
    slug: str = Field(max_length=255, pattern=r"^[a-z0-9\-]+$")
    timezone: str = "Africa/Johannesburg"
    plan: str = "STARTER"
    whatsapp_phone_number_id: str | None = None
    daily_message_limit: int = 800
    daily_llm_call_limit: int = 400
    daily_order_limit: int = 200


class BusinessUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    plan: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_business_account_id: str | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    billing_status: str | None = None


class LimitsUpdate(BaseModel):
    daily_message_limit: int | None = Field(default=None, ge=0)
    daily_llm_call_limit: int | None = Field(default=None, ge=0)
    daily_order_limit: int | None = Field(default=None, ge=0)


class SuspendRequest(BaseModel):
    reason: str = Field(max_length=512)


class BusinessAdminResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    business_code: str
    is_active: bool
    suspended_reason: str | None
    timezone: str
    plan: str
    billing_status: str
    currency: str
    whatsapp_phone_number_id: str | None
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    daily_message_limit: int
    daily_llm_call_limit: int
    daily_order_limit: int
    last_webhook_received_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}


# ── Routes ───────────────────────────────────────────────────────────────────

@router.get("", response_model=list[BusinessAdminResponse])
async def list_businesses(
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    query = select(Business)
    if is_active is not None:
        query = query.where(Business.is_active == is_active)
    query = query.order_by(Business.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    return [BusinessAdminResponse.model_validate(b) for b in result.scalars().all()]


@router.post("", response_model=BusinessAdminResponse, status_code=201)
async def create_business(
    body: BusinessCreate,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    # Check slug uniqueness
    existing = await db.execute(
        select(Business.id).where(Business.slug == body.slug)
    )
    if existing.scalar_one_or_none():
        raise DuplicateError("Business", "slug")

    # Generate unique business code
    for _ in range(10):
        code = generate_business_code()
        check = await db.execute(
            select(Business.id).where(Business.business_code == code)
        )
        if not check.scalar_one_or_none():
            break
    else:
        raise AppError("CODE_GENERATION_FAILED", "Could not generate unique business code", 500)

    biz = Business(
        business_code=code,
        **body.model_dump(),
    )
    db.add(biz)
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.get("/{business_id}", response_model=BusinessAdminResponse)
async def get_business(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))
    return BusinessAdminResponse.model_validate(biz)


@router.patch("/{business_id}", response_model=BusinessAdminResponse)
async def update_business(
    business_id: uuid.UUID,
    body: BusinessUpdate,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(biz, field, value)
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.post("/{business_id}/suspend", response_model=BusinessAdminResponse)
async def suspend_business(
    business_id: uuid.UUID,
    body: SuspendRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    biz.is_active = False
    biz.suspended_reason = body.reason
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.post("/{business_id}/unsuspend", response_model=BusinessAdminResponse)
async def unsuspend_business(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    biz.is_active = True
    biz.suspended_reason = None
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.post("/{business_id}/limits", response_model=BusinessAdminResponse)
async def set_business_limits(
    business_id: uuid.UUID,
    body: LimitsUpdate,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(biz, field, value)
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)
