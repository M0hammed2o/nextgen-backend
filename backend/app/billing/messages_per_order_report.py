"""
Read-only reporting queries for "real outbound WhatsApp messages / LLM calls
per completed order" — the exact metric a R799/month pricing decision needs,
computed from PRODUCTION data once a pilot tenant has some.

Deliberately separate from `backend/app/billing/whatsapp_usage.py` (order-quota
enforcement) and `backend/app/bot/usage_tracker.py` (the write side that
increments `DailyUsage` from inside the live pipeline). This module only
READS rows that `Order`/`Message`/`DailyUsage` already contain after the
fact — it does not call, import, or modify anything in `backend/app/bot/*`
or `backend/app/llm/*`, and it writes nothing.

Two complementary queries, in increasing order of precision:

1. `get_daily_aggregate_messages_per_order` — the simplest, most robust
   number: SUM(outbound_messages) / SUM(orders_completed) from `DailyUsage`
   over a date range, per business. No per-order attribution needed (and
   therefore no heuristic/approximation risk) because `DailyUsage` already
   aggregates both at business-day granularity. Use this for a trustworthy
   top-line ratio as soon as a pilot tenant has a few days of real traffic.

2. `get_per_order_message_counts` — a finer, per-order distribution (so you
   can see min/max/median, not just a single aggregate ratio). `Message`
   carries no `order_id` FK (see shared/models/message.py), so each order's
   message count is attributed by a time-window heuristic: every OUTBOUND
   message for the same (business_id, customer_id) between the customer's
   previous completed WhatsApp order (or, if none, `lookback_hours` before
   this order) and this order's `created_at`. This is an approximation —
   documented as such below — not an exact join, because the data model has
   no exact link. It will overcount if a customer's two separate
   conversations happen to fall in the same window with no order in between
   (e.g. an abandoned browse immediately followed by a real order), and it
   is only as good as `lookback_hours` for a customer's very first order.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.enums import OrderSource
from shared.models.analytics import DailyUsage
from shared.models.business import Business
from shared.models.message import Message
from shared.models.order import Order

# Mirrors backend/app/billing/whatsapp_usage.py::WHATSAPP_BILLABLE_STATUSES —
# duplicated rather than imported to keep this reporting module fully
# independent of the billing-quota module's import surface; if the two ever
# need to diverge (e.g. a future report that deliberately includes
# cancelled orders) they can, without this module silently inheriting a
# change meant only for quota enforcement.
from shared.enums import OrderStatus

_BILLABLE_STATUSES: frozenset[str] = frozenset({
    OrderStatus.COLLECTED.value,
    OrderStatus.DELIVERED.value,
    OrderStatus.COMPLETED.value,
})


# ── 1. Simple, robust aggregate ratio (no per-order attribution needed) ──────

@dataclass
class DailyAggregateMessagesPerOrder:
    period_start: str
    period_end: str
    total_outbound_messages: int
    total_inbound_messages: int
    total_orders_completed: int
    total_llm_calls: int
    total_llm_tokens: int
    total_llm_cost_cents: int
    outbound_messages_per_completed_order: float | None
    llm_calls_per_completed_order: float | None


async def get_daily_aggregate_messages_per_order(
    db: AsyncSession,
    business: Business,
    period_start: datetime,
    period_end: datetime,
) -> DailyAggregateMessagesPerOrder:
    """
    Aggregate real messages-per-order and LLM-calls-per-order ratios from
    `DailyUsage` (already populated in production by
    `backend/app/bot/usage_tracker.py::increment_usage` on every real turn).

    `period_start`/`period_end` are compared against `DailyUsage.day`
    (inclusive on both ends).
    """
    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(DailyUsage.outbound_messages), 0),
                func.coalesce(func.sum(DailyUsage.inbound_messages), 0),
                func.coalesce(func.sum(DailyUsage.orders_completed), 0),
                func.coalesce(func.sum(DailyUsage.llm_calls), 0),
                func.coalesce(func.sum(DailyUsage.llm_tokens), 0),
                func.coalesce(func.sum(DailyUsage.llm_cost_cents), 0),
            ).where(
                DailyUsage.business_id == business.id,
                DailyUsage.day >= period_start.date(),
                DailyUsage.day <= period_end.date(),
            )
        )
    ).one()

    (
        total_outbound,
        total_inbound,
        total_orders_completed,
        total_llm_calls,
        total_llm_tokens,
        total_llm_cost_cents,
    ) = row

    return DailyAggregateMessagesPerOrder(
        period_start=period_start.date().isoformat(),
        period_end=period_end.date().isoformat(),
        total_outbound_messages=int(total_outbound),
        total_inbound_messages=int(total_inbound),
        total_orders_completed=int(total_orders_completed),
        total_llm_calls=int(total_llm_calls),
        total_llm_tokens=int(total_llm_tokens),
        total_llm_cost_cents=int(total_llm_cost_cents),
        outbound_messages_per_completed_order=(
            total_outbound / total_orders_completed
            if total_orders_completed else None
        ),
        llm_calls_per_completed_order=(
            total_llm_calls / total_orders_completed
            if total_orders_completed else None
        ),
    )


# ── 2. Per-order distribution via a time-window heuristic join ──────────────

@dataclass
class PerOrderMessageCount:
    order_id: str
    order_number: str
    customer_id: str | None
    order_created_at: str
    window_start: str
    outbound_message_count: int
    llm_message_count: int


async def get_per_order_message_counts(
    db: AsyncSession,
    business: Business,
    period_start: datetime,
    period_end: datetime,
    lookback_hours: int = 24,
) -> list[PerOrderMessageCount]:
    """
    Per-order outbound-message and LLM-call counts for every billable
    WHATSAPP-source order in [period_start, period_end], attributed by the
    time-window heuristic described in this module's docstring (APPROXIMATE
    — see caveats above; prefer `get_daily_aggregate_messages_per_order` for
    a trustworthy top-line number, use this only for distribution shape).

    Read-only: does not write to `Order`, `Message`, or `DailyUsage`, and
    does not import or call anything under `backend/app/bot/*` or
    `backend/app/llm/*`.
    """
    orders_result = await db.execute(
        select(Order.id, Order.order_number, Order.customer_id, Order.created_at)
        .where(
            Order.business_id == business.id,
            Order.source == OrderSource.WHATSAPP.value,
            Order.status.in_(_BILLABLE_STATUSES),
            Order.created_at >= period_start,
            Order.created_at <= period_end,
        )
        .order_by(Order.customer_id, Order.created_at)
    )
    orders = orders_result.all()

    results: list[PerOrderMessageCount] = []
    # Track each customer's previous order timestamp in this result set so
    # the window for order N starts right after order N-1, not from a fixed
    # lookback every time (reduces double-counting across consecutive orders
    # from the same repeat customer).
    last_order_at_by_customer: dict = {}

    for order_id, order_number, customer_id, created_at in orders:
        prev_at = last_order_at_by_customer.get(customer_id)
        if prev_at is None:
            from datetime import timedelta
            window_start = created_at - timedelta(hours=lookback_hours)
        else:
            window_start = prev_at
        last_order_at_by_customer[customer_id] = created_at

        outbound_count_row = (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.business_id == business.id,
                    Message.customer_id == customer_id,
                    Message.direction == "OUTBOUND",
                    Message.created_at > window_start,
                    Message.created_at <= created_at,
                )
            )
        ).one()
        outbound_count = int(outbound_count_row[0])

        llm_count_row = (
            await db.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.business_id == business.id,
                    Message.customer_id == customer_id,
                    Message.direction == "OUTBOUND",
                    Message.is_llm.is_(True),
                    Message.created_at > window_start,
                    Message.created_at <= created_at,
                )
            )
        ).one()
        llm_count = int(llm_count_row[0])

        results.append(
            PerOrderMessageCount(
                order_id=str(order_id),
                order_number=order_number,
                customer_id=str(customer_id) if customer_id else None,
                order_created_at=created_at.isoformat(),
                window_start=window_start.isoformat(),
                outbound_message_count=outbound_count,
                llm_message_count=llm_count,
            )
        )

    return results
