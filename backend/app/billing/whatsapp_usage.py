"""
WhatsApp-specific order-usage computation for the new "WhatsApp Only"
subscription package (migration 0057_pos_entitlement).

Deliberately separate from `backend/app/billing/usage.py::get_order_quota_status`
(the existing ALL-CHANNEL `monthly_order_quota` quota) — this counts only
`Order.source == 'WHATSAPP'` orders against the NEW, separate
`Business.whatsapp_order_allowance` column, which is NULL (no allowance
configured) for every tenant unless a SUPER_ADMIN explicitly sets one.

Computed directly from the `orders` table — NOT from `DailyUsage` (which
aggregates ALL sources together) — so this never has to be reconciled
against, or risk double-counting with, the all-channel quota above.

Does not touch, call, or import anything from the WhatsApp bot pipeline
(`backend/app/bot/*`) — it only reads `Order` rows the pipeline already
wrote, after the fact.
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.enums import OrderSource, OrderStatus
from shared.models.business import Business
from shared.models.order import Order

# A WhatsApp order counts as billable usage only once it has actually been
# fulfilled — confirmed (not guessed) against shared/enums.py::OrderStatus
# and ORDER_STATUS_TRANSITIONS: these three are the only terminal,
# non-CANCELLED states an order can reach (COLLECTED = pickup, DELIVERED =
# delivery, COMPLETED = dine-in / grab-and-go). CANCELLED is the one
# terminal "never happened" state and is deliberately excluded — there is
# no separate "VOIDED" value on Order.status itself (VOIDED only exists on
# PaymentStatus, for a till-native POS reversal, which is not reachable for
# a WHATSAPP-source order).
WHATSAPP_BILLABLE_STATUSES: frozenset[str] = frozenset({
    OrderStatus.COLLECTED.value,
    OrderStatus.DELIVERED.value,
    OrderStatus.COMPLETED.value,
})


@dataclass
class WhatsAppOrderUsage:
    period_start: str
    orders_used: int
    orders_total_all_statuses: int
    orders_allowance: int | None
    orders_remaining: int | None
    pct_used: float | None
    tier: str | None


async def get_whatsapp_order_usage(db: AsyncSession, business: Business) -> WhatsAppOrderUsage:
    """
    `orders_used` (billing-relevant): WHATSAPP-source orders in the current
    billing period that reached a terminal, non-cancelled status.

    `orders_total_all_statuses` (transparency only, never used for billing):
    every WHATSAPP-source order created in the period regardless of status
    — including still-in-flight and CANCELLED orders — so an owner/admin
    can see the gap between "orders placed" and "orders that count."

    `orders_allowance`/`orders_remaining`/`pct_used`/`tier` are all None
    when `business.whatsapp_order_allowance` is NULL (not configured for
    this tenant) — a tenant with no allowance simply has no threshold to
    report against, not a silently-assumed default.
    """
    period_start = business.billing_period_started_at

    used_result = await db.execute(
        select(func.count()).select_from(Order).where(
            Order.business_id == business.id,
            Order.source == OrderSource.WHATSAPP.value,
            Order.status.in_(WHATSAPP_BILLABLE_STATUSES),
            Order.created_at >= period_start,
        )
    )
    orders_used = int(used_result.scalar_one() or 0)

    total_result = await db.execute(
        select(func.count()).select_from(Order).where(
            Order.business_id == business.id,
            Order.source == OrderSource.WHATSAPP.value,
            Order.created_at >= period_start,
        )
    )
    orders_total_all_statuses = int(total_result.scalar_one() or 0)

    allowance = business.whatsapp_order_allowance
    if allowance is None:
        return WhatsAppOrderUsage(
            period_start=period_start.isoformat(),
            orders_used=orders_used,
            orders_total_all_statuses=orders_total_all_statuses,
            orders_allowance=None,
            orders_remaining=None,
            pct_used=None,
            tier=None,
        )

    orders_remaining = max(0, allowance - orders_used)
    pct_used = (orders_used / allowance * 100) if allowance > 0 else 100.0
    if pct_used >= 100:
        tier = "exceeded"
    elif pct_used >= 90:
        tier = "warning_90"
    elif pct_used >= 80:
        tier = "warning_80"
    else:
        tier = "ok"

    return WhatsAppOrderUsage(
        period_start=period_start.isoformat(),
        orders_used=orders_used,
        orders_total_all_statuses=orders_total_all_statuses,
        orders_allowance=allowance,
        orders_remaining=orders_remaining,
        pct_used=round(pct_used, 2),
        tier=tier,
    )
