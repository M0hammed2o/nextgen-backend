"""
Admin usage and audit routes — platform-wide visibility.
"""

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.rbac import AuthUser, require_super_admin
from backend.app.db.session import get_db
from shared.models.analytics import DailyUsage
from shared.models.audit import AuditEvent

router = APIRouter(prefix="/admin", tags=["admin-usage"])


class BusinessUsageSummary(BaseModel):
    business_id: uuid.UUID
    total_messages: int
    total_llm_calls: int
    total_llm_cost_cents: int
    total_orders: int
    total_revenue_cents: int


class AuditEventResponse(BaseModel):
    id: uuid.UUID
    scope: str
    business_id: uuid.UUID | None
    actor_user_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: uuid.UUID | None
    diff_json: dict | None
    ip_address: str | None
    created_at: datetime
    model_config = {"from_attributes": True}


@router.get("/usage", response_model=list[BusinessUsageSummary])
async def get_usage(
    start_date: date = Query(...),
    end_date: date = Query(...),
    business_id: uuid.UUID | None = None,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get usage summary per business for a date range."""
    query = (
        select(
            DailyUsage.business_id,
            func.sum(DailyUsage.inbound_messages + DailyUsage.outbound_messages).label("total_messages"),
            func.sum(DailyUsage.llm_calls).label("total_llm_calls"),
            func.sum(DailyUsage.llm_cost_cents).label("total_llm_cost_cents"),
            func.sum(DailyUsage.orders_created).label("total_orders"),
            func.sum(DailyUsage.revenue_cents).label("total_revenue_cents"),
        )
        .where(DailyUsage.day >= start_date, DailyUsage.day <= end_date)
        .group_by(DailyUsage.business_id)
    )
    if business_id:
        query = query.where(DailyUsage.business_id == business_id)

    result = await db.execute(query)
    return [
        BusinessUsageSummary(
            business_id=row.business_id,
            total_messages=row.total_messages or 0,
            total_llm_calls=row.total_llm_calls or 0,
            total_llm_cost_cents=row.total_llm_cost_cents or 0,
            total_orders=row.total_orders or 0,
            total_revenue_cents=row.total_revenue_cents or 0,
        )
        for row in result.all()
    ]


@router.get("/audit", response_model=list[AuditEventResponse])
async def get_audit_log(
    scope: str | None = None,
    business_id: uuid.UUID | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=50, ge=1, le=100),
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get platform audit log with optional filters."""
    query = select(AuditEvent).order_by(AuditEvent.created_at.desc())
    if scope:
        query = query.where(AuditEvent.scope == scope)
    if business_id:
        query = query.where(AuditEvent.business_id == business_id)
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    return [AuditEventResponse.model_validate(e) for e in result.scalars().all()]
