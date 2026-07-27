"""
Monthly order-quota computation for the NextGen Restaurant plan's
"1,500 Included Customer Orders per Month" + Order Pack pricing.

Deliberately computed from the existing DailyUsage.orders_created signal
(summed over the current billing period) rather than a separately
maintained counter — a maintained counter can drift from reality; a sum
over an already-correct source of truth cannot.

This module does NOT enforce anything (no order is ever blocked) — the
commercial spec doesn't define overage behavior (block vs. bill), and
guessing that wrong would be a real business-policy mistake, not a
technical one. See TODO.md.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.analytics import DailyUsage
from shared.models.business import Business


@dataclass
class OrderQuotaStatus:
    quota: int
    pack_credits: int
    used: int
    remaining: int
    period_started_at: str


async def get_order_quota_status(db: AsyncSession, business: Business) -> OrderQuotaStatus:
    result = await db.execute(
        select(func.coalesce(func.sum(DailyUsage.orders_created), 0)).where(
            DailyUsage.business_id == business.id,
            DailyUsage.day >= business.billing_period_started_at.date(),
        )
    )
    used = result.scalar_one() or 0
    total_allowance = business.monthly_order_quota + business.order_pack_credits
    return OrderQuotaStatus(
        quota=business.monthly_order_quota,
        pack_credits=business.order_pack_credits,
        used=used,
        remaining=max(0, total_allowance - used),
        period_started_at=business.billing_period_started_at.isoformat(),
    )
