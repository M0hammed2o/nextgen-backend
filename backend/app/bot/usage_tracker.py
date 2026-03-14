"""
Usage Tracker — upsert increments to daily_usage.
Also enforces daily limits (messages, LLM calls, orders).
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import DailyLimitError
from shared.models.analytics import DailyUsage
from shared.models.business import Business
from shared.utils.time import today_date_for_business


async def increment_usage(
    db: AsyncSession,
    business_id: uuid.UUID,
    tz_name: str = "Africa/Johannesburg",
    inbound_messages: int = 0,
    outbound_messages: int = 0,
    llm_calls: int = 0,
    llm_tokens: int = 0,
    llm_cost_cents: int = 0,
    orders_created: int = 0,
    orders_completed: int = 0,
    cancelled_orders: int = 0,
    revenue_cents: int = 0,
    unique_customers: int = 0,
) -> None:
    """
    Upsert daily usage with atomic increments.
    Uses PostgreSQL ON CONFLICT ... DO UPDATE for safety.
    """
    today = today_date_for_business(tz_name)

    stmt = pg_insert(DailyUsage).values(
        business_id=business_id,
        day=today,
        inbound_messages=inbound_messages,
        outbound_messages=outbound_messages,
        llm_calls=llm_calls,
        llm_tokens=llm_tokens,
        llm_cost_cents=llm_cost_cents,
        orders_created=orders_created,
        orders_completed=orders_completed,
        cancelled_orders=cancelled_orders,
        revenue_cents=revenue_cents,
        unique_customers=unique_customers,
    ).on_conflict_do_update(
        constraint="uq_daily_usage_business_day",
        set_={
            "inbound_messages": DailyUsage.inbound_messages + inbound_messages,
            "outbound_messages": DailyUsage.outbound_messages + outbound_messages,
            "llm_calls": DailyUsage.llm_calls + llm_calls,
            "llm_tokens": DailyUsage.llm_tokens + llm_tokens,
            "llm_cost_cents": DailyUsage.llm_cost_cents + llm_cost_cents,
            "orders_created": DailyUsage.orders_created + orders_created,
            "orders_completed": DailyUsage.orders_completed + orders_completed,
            "cancelled_orders": DailyUsage.cancelled_orders + cancelled_orders,
            "revenue_cents": DailyUsage.revenue_cents + revenue_cents,
            "unique_customers": DailyUsage.unique_customers + unique_customers,
        },
    )
    await db.execute(stmt)
    await db.flush()


async def check_daily_limit(
    db: AsyncSession,
    business: Business,
    limit_type: str,  # "messages", "llm_calls", "orders"
) -> None:
    """
    Check if a daily limit has been reached. Raises DailyLimitError if so.
    """
    today = today_date_for_business(business.timezone)

    result = await db.execute(
        select(DailyUsage).where(
            DailyUsage.business_id == business.id,
            DailyUsage.day == today,
        )
    )
    usage = result.scalar_one_or_none()

    if not usage:
        return  # No usage yet today

    if limit_type == "messages":
        current = usage.inbound_messages + usage.outbound_messages
        limit = business.daily_message_limit
    elif limit_type == "llm_calls":
        current = usage.llm_calls
        limit = business.daily_llm_call_limit
    elif limit_type == "orders":
        current = usage.orders_created
        limit = business.daily_order_limit
    else:
        return

    if current >= limit:
        raise DailyLimitError(limit_type, limit)
