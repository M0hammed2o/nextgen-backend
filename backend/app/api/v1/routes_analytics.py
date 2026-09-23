"""
Analytics routes — dashboard summary data (today/7d/30d).
Top items, daily breakdown.

CRITICAL FIX: Summary and daily now query directly from Orders table
as the primary source of truth, with messaging/LLM data enriched from
daily_usage when available. This ensures analytics work for both
WhatsApp bot orders AND manual dashboard orders.

Commercial V1 tiering (migration 0047, docs/COMMERCIAL_FEATURE_MATRIX_2026-10.md):
`/summary`, `/top-items`, and `/daily` below are Core Analytics — available on
every plan including NextGen POS, ungated. `/average-order-value` and
`/payment-methods` are Advanced Analytics (NextGen Complete and above), gated
by `Business.advanced_analytics_enabled` via `require_advanced_analytics_enabled`
(returns 409 ADVANCED_ANALYTICS_NOT_ENABLED for a non-entitled tenant). The
delivery-channel breakdown, the other Advanced Analytics report, lives in
routes_external_orders.py's `/business/external-orders/channels` and is gated
the same way.
"""

from decimal import ROUND_HALF_UP, Decimal

from datetime import date, datetime, timedelta, timezone as tz

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.analytics.revenue import (
    IS_SALE,
    gross_cents_expr,
    local_sale_date_expr,
    refunds_cents_expr,
    require_advanced_analytics_enabled,
    sale_count_expr,
    sale_timestamp_expr,
    true_cancelled_count_expr,
    vat_cents_expr,
    voided_cents_expr,
    voided_count_expr,
)
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.analytics import DailyUsage
from shared.models.order import Order, OrderItem
from shared.utils.time import local_midnight_to_utc, today_date_for_business

router = APIRouter(prefix="/business/analytics", tags=["analytics"])


class AnalyticsSummary(BaseModel):
    period: str
    total_orders: int
    # BUG-0043 fix: `completed_orders` now means "counts as a sale" (see
    # backend/app/analytics/revenue.py) -- paid-ish OR fulfilled, not just
    # COLLECTED/DELIVERED. `cancelled_orders` now means a TRUE cancellation
    # (status CANCELLED and never paid/voided) -- POS voids are reported
    # separately via `voided_orders` instead of being silently lumped in.
    completed_orders: int
    cancelled_orders: int
    voided_orders: int
    # `revenue_cents` keeps its existing name/meaning for the dashboard but
    # now carries NET sales (gross - refunds) computed from the shared sale
    # definition, instead of a narrower COLLECTED/DELIVERED-only total.
    revenue_cents: int
    gross_cents: int
    refunds_cents: int
    net_cents: int
    vat_cents: int
    voided_cents: int
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
    # `revenue_cents` keeps its existing meaning for the dashboard chart but
    # now carries NET sales (gross - refunds) for that local calendar day.
    revenue_cents: int
    gross_cents: int
    refunds_cents: int
    vat_cents: int
    messages: int


@router.get("/summary", response_model=AnalyticsSummary)
async def get_summary(
    period: str = Query(default="7d", pattern=r"^(today|7d|30d)$"),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Get analytics summary for today, 7 days, or 30 days.

    Queries Orders table directly for order/revenue/customer metrics,
    then enriches with messaging/LLM data from daily_usage if available.
    This ensures analytics work for ALL order sources (WhatsApp + manual).
    """
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

    # BUG-0032: local midnight of start_date, converted to a real UTC
    # instant -- see local_midnight_to_utc()'s docstring for why the
    # naive "tag the local date's y/m/d with UTC tzinfo" approach this
    # replaced was wrong for any non-zero-offset business timezone.
    cutoff_dt = local_midnight_to_utc(start_date, tz_name)

    order_stats = await db.execute(
        select(
            func.count(Order.id).label("total_orders"),
            sale_count_expr().label("completed_orders"),
            true_cancelled_count_expr().label("cancelled_orders"),
            voided_count_expr().label("voided_orders"),
            gross_cents_expr().label("gross_cents"),
            refunds_cents_expr().label("refunds_cents"),
            vat_cents_expr().label("vat_cents"),
            voided_cents_expr().label("voided_cents"),
            func.count(func.distinct(Order.phone_number)).label("unique_customers"),
        ).where(
            Order.business_id == user.business_id,
            sale_timestamp_expr() >= cutoff_dt,
        )
    )
    orow = order_stats.one()
    gross_cents = int(orow.gross_cents or 0)
    refunds_cents = int(orow.refunds_cents or 0)
    net_cents = gross_cents - refunds_cents

    msg_stats = await db.execute(
        select(
            func.coalesce(
                func.sum(DailyUsage.inbound_messages) + func.sum(DailyUsage.outbound_messages),
                0,
            ).label("total_messages"),
            func.coalesce(func.sum(DailyUsage.llm_calls), 0).label("llm_calls"),
            func.coalesce(func.sum(DailyUsage.llm_cost_cents), 0).label("llm_cost_cents"),
        ).where(
            DailyUsage.business_id == user.business_id,
            DailyUsage.day >= start_date,
        )
    )
    mrow = msg_stats.one()

    return AnalyticsSummary(
        period=period,
        total_orders=int(orow.total_orders or 0),
        completed_orders=int(orow.completed_orders or 0),
        cancelled_orders=int(orow.cancelled_orders or 0),
        voided_orders=int(orow.voided_orders or 0),
        revenue_cents=net_cents,
        gross_cents=gross_cents,
        refunds_cents=refunds_cents,
        net_cents=net_cents,
        vat_cents=int(orow.vat_cents or 0),
        voided_cents=int(orow.voided_cents or 0),
        total_messages=int(mrow.total_messages or 0),
        llm_calls=int(mrow.llm_calls or 0),
        llm_cost_cents=int(mrow.llm_cost_cents or 0),
        unique_customers=int(orow.unique_customers or 0),
    )


@router.get("/top-items", response_model=list[TopItem])
async def get_top_items(
    days: int = Query(default=30, ge=1, le=90),
    limit: int = Query(default=10, ge=1, le=50),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Get top-selling items by quantity over the given period.

    Uses the shared sale definition (IS_SALE, backend/app/analytics/revenue.py)
    so a line only counts once its order is a real sale — excludes voided
    orders and unpaid/unfulfilled funnel orders, not just CANCELLED ones.
    Refunds are recorded at the order level only (no per-line refund
    tracking exists), so a partially refunded order's lines still count at
    their full original amount here.
    """
    cutoff = datetime.now(tz.utc) - timedelta(days=days)

    result = await db.execute(
        select(
            OrderItem.name_snapshot,
            func.sum(OrderItem.quantity).label("total_qty"),
            func.sum(OrderItem.line_total_cents).label("total_revenue"),
        )
        .join(Order, OrderItem.order_id == Order.id)
        .where(
            OrderItem.business_id == user.business_id,
            sale_timestamp_expr() >= cutoff,
            IS_SALE,
        )
        .group_by(OrderItem.name_snapshot)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(limit)
    )

    return [
        TopItem(
            name=row.name_snapshot,
            quantity=int(row.total_qty or 0),
            revenue_cents=int(row.total_revenue or 0),
        )
        for row in result.all()
    ]


@router.get("/daily", response_model=list[DailyBreakdown])
async def get_daily_breakdown(
    days: int = Query(default=30, ge=1, le=90),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Get daily breakdown of orders and revenue.

    Queries Orders table directly so manual orders are always included.
    Enriches with messaging data from daily_usage when available.
    """
    from shared.models.business import Business

    biz = await db.get(Business, user.business_id)
    tz_name = biz.timezone if biz else "Africa/Johannesburg"
    start_date = today_date_for_business(tz_name) - timedelta(days=days - 1)
    cutoff_dt = local_midnight_to_utc(start_date, tz_name)

    # BUG-0043: previously `cast(Order.created_at, Date)` — a raw UTC-date
    # cast, which (like the range cutoff before BUG-0032's fix) puts an
    # order placed e.g. 00:30 SAST on the PREVIOUS UTC calendar day. Bucket
    # by the sale's own local date instead, same fix applied to every day
    # boundary in this module, not just the range start.
    order_day_expr = local_sale_date_expr(tz_name)

    order_daily = await db.execute(
        select(
            order_day_expr.label("day"),
            func.count(Order.id).label("orders"),
            gross_cents_expr().label("gross_cents"),
            refunds_cents_expr().label("refunds_cents"),
            vat_cents_expr().label("vat_cents"),
        )
        .where(
            Order.business_id == user.business_id,
            sale_timestamp_expr() >= cutoff_dt,
        )
        .group_by(order_day_expr)
        .order_by(order_day_expr)
    )
    order_rows = {row.day: row for row in order_daily.all()}

    msg_daily = await db.execute(
        select(DailyUsage).where(
            DailyUsage.business_id == user.business_id,
            DailyUsage.day >= start_date,
        )
    )
    msg_rows = {row.day: row for row in msg_daily.scalars().all()}

    # Always emit every day in the requested range — even days with zero activity —
    # so the chart always shows a full timeline rather than appearing blank.
    from datetime import timedelta as _td
    today_local = today_date_for_business(tz_name)
    all_days: list[date] = []
    cursor = start_date
    while cursor <= today_local:
        all_days.append(cursor)
        cursor += _td(days=1)

    result = []
    for d in all_days:
        odata = order_rows.get(d)
        mdata = msg_rows.get(d)
        gross_cents = int(odata.gross_cents or 0) if odata else 0
        refunds_cents = int(odata.refunds_cents or 0) if odata else 0
        result.append(
            DailyBreakdown(
                day=d,
                orders=int(odata.orders or 0) if odata else 0,
                revenue_cents=gross_cents - refunds_cents,
                gross_cents=gross_cents,
                refunds_cents=refunds_cents,
                vat_cents=int(odata.vat_cents or 0) if odata else 0,
                messages=int((mdata.inbound_messages or 0) + (mdata.outbound_messages or 0)) if mdata else 0,
            )
        )

    return result


# ── Advanced Analytics (NextGen Complete and above — migration 0047) ────────

class AverageOrderValue(BaseModel):
    period: str
    completed_orders: int
    net_cents: int
    # 0 when completed_orders is 0 — never a divide-by-zero. Computed as
    # net_cents / completed_orders (Decimal, ROUND_HALF_UP to the nearest
    # cent — same rounding convention as recipe costing in routes_recipes.py),
    # from the SAME gross/refunds numbers /summary reports for the same
    # period, so the two can never disagree.
    average_order_value_cents: int


@router.get("/average-order-value", response_model=AverageOrderValue)
async def get_average_order_value(
    period: str = Query(default="7d", pattern=r"^(today|7d|30d)$"),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Advanced Analytics. Average order value = net_cents / completed_orders,
    using the exact same IS_SALE-derived gross/refunds/sale-count expressions
    as /summary (backend/app/analytics/revenue.py) — never a parallel
    definition of what counts as a sale or a completed order.
    """
    business = await require_advanced_analytics_enabled(db, user.business_id)
    tz_name = business.timezone or "Africa/Johannesburg"
    today = today_date_for_business(tz_name)

    if period == "today":
        start_date = today
    elif period == "7d":
        start_date = today - timedelta(days=6)
    else:
        start_date = today - timedelta(days=29)

    cutoff_dt = local_midnight_to_utc(start_date, tz_name)

    row = (
        await db.execute(
            select(
                sale_count_expr().label("completed_orders"),
                gross_cents_expr().label("gross_cents"),
                refunds_cents_expr().label("refunds_cents"),
            ).where(
                Order.business_id == user.business_id,
                sale_timestamp_expr() >= cutoff_dt,
            )
        )
    ).one()

    completed_orders = int(row.completed_orders or 0)
    net_cents = int(row.gross_cents or 0) - int(row.refunds_cents or 0)
    if completed_orders:
        aov_cents = int(
            (Decimal(net_cents) / Decimal(completed_orders)).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    else:
        aov_cents = 0

    return AverageOrderValue(
        period=period,
        completed_orders=completed_orders,
        net_cents=net_cents,
        average_order_value_cents=aov_cents,
    )


class PaymentMethodBreakdown(BaseModel):
    payment_method: str
    order_count: int
    gross_cents: int
    vat_cents: int
    refunds_cents: int
    net_cents: int


class PaymentMethodsReport(BaseModel):
    days: int
    since: datetime
    methods: list[PaymentMethodBreakdown]
    total_orders: int
    total_gross_cents: int
    total_refunds_cents: int
    total_net_cents: int


@router.get("/payment-methods", response_model=PaymentMethodsReport)
async def get_payment_method_breakdown(
    days: int = Query(default=30, ge=1, le=90),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Advanced Analytics. Cash-vs-card (and every other recorded
    Order.payment_method) revenue split — reuses IS_SALE/refunds_cents_expr/
    sale_timestamp_expr exactly as routes_external_orders.py's channel
    report does, rather than writing a second "what counts as a sale" rule.
    A NULL payment_method (e.g. an order that never reached a recorded
    payment method) is bucketed as UNKNOWN rather than silently dropped.
    """
    await require_advanced_analytics_enabled(db, user.business_id)
    since = datetime.now(tz.utc) - timedelta(days=days)

    method_expr = func.coalesce(Order.payment_method, "UNKNOWN")
    rows = (
        await db.execute(
            select(
                method_expr,
                func.count(),
                func.coalesce(func.sum(Order.total_cents), 0),
                func.coalesce(func.sum(Order.tax_cents), 0),
                refunds_cents_expr(),
            )
            .where(
                Order.business_id == user.business_id,
                sale_timestamp_expr() >= since,
                IS_SALE,
            )
            .group_by(method_expr)
        )
    ).all()

    methods = [
        PaymentMethodBreakdown(
            payment_method=payment_method,
            order_count=int(count or 0),
            gross_cents=int(gross or 0),
            vat_cents=int(vat or 0),
            refunds_cents=int(refunds or 0),
            net_cents=int(gross or 0) - int(refunds or 0),
        )
        for payment_method, count, gross, vat, refunds in rows
    ]
    methods.sort(key=lambda m: -m.gross_cents)

    return PaymentMethodsReport(
        days=days,
        since=since,
        methods=methods,
        total_orders=sum(m.order_count for m in methods),
        total_gross_cents=sum(m.gross_cents for m in methods),
        total_refunds_cents=sum(m.refunds_cents for m in methods),
        total_net_cents=sum(m.net_cents for m in methods),
    )