"""
Analytics routes — dashboard summary data (today/7d/30d).
Top items, peak hours, order breakdown.
"""

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.analytics import DailyUsage
from shared.models.order import Order, OrderItem
from shared.utils.time import today_date_for_business

router = APIRouter(prefix="/business/analytics", tags=["analytics"])


class AnalyticsSummary(BaseModel):
    period: str
    total_orders: int
    completed_orders: int
    cancelled_orders: int
    revenue_cents: int
    total_messages: int
    llm_calls: int
    llm_cost_cents: int
    unique_customers: int


class TopItem(BaseModel):
    name: str
    quantity: int
    revenue_cents: int


class DailyBreakdown(BaseModel):
    day: date
    orders: int
    revenue_cents: int
    messages: int


@router.get("/summary", response_model=AnalyticsSummary)
async def get_summary(
    period: str = Query(default="7d", pattern=r"^(today|7d|30d)$"),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Get analytics summary for today, 7 days, or 30 days."""
    from shared.models.business import Business
    biz = await db.get(Business, user.business_id)
    tz_name = biz.timezone if biz else "Africa/Johannesburg"
    today = today_date_for_business(tz_name)

    if period == "today":
        start_date = today
    elif period == "7d":
        start_date = today - timedelta(days=6)
    else:
        start_date = today - timedelta(days=29)

    result = await db.execute(
        select(
            func.coalesce(func.sum(DailyUsage.orders_created), 0).label("total_orders"),
            func.coalesce(func.sum(DailyUsage.orders_completed), 0).label("completed_orders"),
            func.coalesce(func.sum(DailyUsage.cancelled_orders), 0).label("cancelled_orders"),
            func.coalesce(func.sum(DailyUsage.revenue_cents), 0).label("revenue_cents"),
            func.coalesce(
                func.sum(DailyUsage.inbound_messages) + func.sum(DailyUsage.outbound_messages), 0
            ).label("total_messages"),
            func.coalesce(func.sum(DailyUsage.llm_calls), 0).label("llm_calls"),
            func.coalesce(func.sum(DailyUsage.llm_cost_cents), 0).label("llm_cost_cents"),
            func.coalesce(func.sum(DailyUsage.unique_customers), 0).label("unique_customers"),
        ).where(
            DailyUsage.business_id == user.business_id,
            DailyUsage.day >= start_date,
        )
    )
    row = result.one()

    return AnalyticsSummary(
        period=period,
        total_orders=row.total_orders,
        completed_orders=row.completed_orders,
        cancelled_orders=row.cancelled_orders,
        revenue_cents=row.revenue_cents,
        total_messages=row.total_messages,
        llm_calls=row.llm_calls,
        llm_cost_cents=row.llm_cost_cents,
        unique_customers=row.unique_customers,
    )


@router.get("/top-items", response_model=list[TopItem])
async def get_top_items(
    days: int = Query(default=30, ge=1, le=90),
    limit: int = Query(default=10, ge=1, le=50),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Get top-selling items by quantity over the given period."""
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    result = await db.execute(
        select(
            OrderItem.name_snapshot,
            func.sum(OrderItem.quantity).label("total_qty"),
            func.sum(OrderItem.line_total_cents).label("total_revenue"),
        )
        .join(Order, OrderItem.order_id == Order.id)
        .where(
            OrderItem.business_id == user.business_id,
            Order.created_at >= cutoff,
            Order.status.notin_(["CANCELLED"]),
        )
        .group_by(OrderItem.name_snapshot)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(limit)
    )

    return [
        TopItem(name=row.name_snapshot, quantity=row.total_qty, revenue_cents=row.total_revenue)
        for row in result.all()
    ]


@router.get("/daily", response_model=list[DailyBreakdown])
async def get_daily_breakdown(
    days: int = Query(default=30, ge=1, le=90),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Get daily breakdown of orders, revenue, and messages."""
    from shared.models.business import Business
    biz = await db.get(Business, user.business_id)
    tz_name = biz.timezone if biz else "Africa/Johannesburg"
    start_date = today_date_for_business(tz_name) - timedelta(days=days - 1)

    result = await db.execute(
        select(DailyUsage)
        .where(
            DailyUsage.business_id == user.business_id,
            DailyUsage.day >= start_date,
        )
        .order_by(DailyUsage.day)
    )

    return [
        DailyBreakdown(
            day=row.day,
            orders=row.orders_created,
            revenue_cents=row.revenue_cents,
            messages=row.inbound_messages + row.outbound_messages,
        )
        for row in result.scalars().all()
    ]
