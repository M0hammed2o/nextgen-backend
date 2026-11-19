"""
Sales Wallets: sales per channel group (Mr D, Uber Eats, General) plus their
total, for one tenant and one business-local date range.

These are REPORTING views over `orders`, not stored money. Nothing here holds,
moves or pays out funds, and no balance is kept anywhere; every figure is
recomputed from the orders table on each request.

Correctness rests on two things this module deliberately does not redefine:

  - "What counts as a sale", gross/refunds/net/VAT/voids, and "when a sale
    happened" all come from `backend/app/analytics/revenue.py`, the same
    definition the summary, daily, top-items and channel reports use, so the
    wallets can never disagree with them.
  - Every order has exactly one `source`, and `wallet_key_expr()` maps each
    source to exactly one wallet. An order therefore lands in one wallet only,
    and Total is aggregated from the same rows rather than added up a second
    time from separately computed figures. No order is counted twice.

Platform commission and settlement are not modelled anywhere in the database
yet (FEATURE_MATRIX "Commission / settlement: Not built"; Uber settlement data
needs the separate `eats.report` scope). The API therefore reports them as
unavailable with a reason. It never derives, estimates or invents them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.analytics.revenue import (
    IS_SALE,
    gross_cents_expr,
    refunds_cents_expr,
    sale_count_expr,
    sale_timestamp_expr,
    vat_cents_expr,
    voided_cents_expr,
    voided_count_expr,
)
from backend.app.core.errors import AppError
from shared.enums import OrderSource
from shared.models.order import Order
from shared.utils.time import local_midnight_to_utc, today_date_for_business

MR_D = "MR_D"
UBER_EATS = "UBER_EATS"
GENERAL = "GENERAL"
TOTAL = "TOTAL"
WALLET_KEYS: tuple[str, ...] = (MR_D, UBER_EATS, GENERAL)

WALLET_LABELS = {MR_D: "Mr D Food", UBER_EATS: "Uber Eats", GENERAL: "General Sales", TOTAL: "Total Sales"}

#: General Sales is "everything that is not Mr D or Uber Eats", so a new
#: source can never fall outside every wallet. OTHER_EXTERNAL (a generic
#: marketplace CSV import) is listed here for that reason; its own line in
#: the breakdown keeps it visible.
WALLET_SOURCES = {
    MR_D: (OrderSource.MR_D.value,),
    UBER_EATS: (OrderSource.UBER_EATS.value,),
    GENERAL: tuple(s.value for s in OrderSource if s.value not in (OrderSource.MR_D.value, OrderSource.UBER_EATS.value)),
}

SOURCE_LABELS = {
    "POS": "In store (till)", "WHATSAPP": "WhatsApp", "MANUAL": "Manual", "ADMIN": "Admin",
    "UBER_EATS": "Uber Eats", "MR_D": "Mr D", "OTHER_EXTERNAL": "Other platform",
}

RANGES = ("today", "yesterday", "7d", "month", "custom")
MAX_CUSTOM_DAYS = 366

_FEES_UNAVAILABLE = {
    "available": False,
    "reason": "Platform commission is not recorded yet. It is never estimated.",
}
_SETTLEMENT_UNAVAILABLE = {
    "available": False,
    "reason": "Platform settlement (payout) data is not recorded yet. It is never estimated.",
}


def wallet_key_expr():
    """SQL: the one wallet an order belongs to, from its source."""
    return case(
        (Order.source == OrderSource.MR_D.value, MR_D),
        (Order.source == OrderSource.UBER_EATS.value, UBER_EATS),
        else_=GENERAL,
    )


def wallet_for_source(source: str) -> str:
    """Pure-Python mirror of `wallet_key_expr()`, kept in lockstep for tests."""
    if source == OrderSource.MR_D.value:
        return MR_D
    if source == OrderSource.UBER_EATS.value:
        return UBER_EATS
    return GENERAL


@dataclass(frozen=True)
class DateRange:
    range: str
    start_date: date
    end_date: date
    timezone: str

    @property
    def start_utc(self) -> datetime:
        return local_midnight_to_utc(self.start_date, self.timezone)

    @property
    def end_utc_exclusive(self) -> datetime:
        return local_midnight_to_utc(self.end_date + timedelta(days=1), self.timezone)


def resolve_range(
    range_name: str, start: date | None, end: date | None, tz_name: str, *, today: date | None = None
) -> DateRange:
    """
    Turn a named range into inclusive business-local dates.

    today / yesterday / 7d (today and the 6 days before) / month (the 1st of
    this month to today) / custom (start..end, inclusive, at most 366 days).
    """
    today = today or today_date_for_business(tz_name)
    if range_name == "today":
        s = e = today
    elif range_name == "yesterday":
        s = e = today - timedelta(days=1)
    elif range_name == "7d":
        s, e = today - timedelta(days=6), today
    elif range_name == "month":
        s, e = today.replace(day=1), today
    elif range_name == "custom":
        if start is None or end is None:
            raise AppError("INVALID_DATE_RANGE", "A custom range needs both a start and an end date.", 422)
        if end < start:
            raise AppError("INVALID_DATE_RANGE", "The end date is before the start date.", 422)
        if (end - start).days + 1 > MAX_CUSTOM_DAYS:
            raise AppError("INVALID_DATE_RANGE", f"A custom range can cover at most {MAX_CUSTOM_DAYS} days.", 422)
        s, e = start, end
    else:
        raise AppError("INVALID_DATE_RANGE", f"Unknown range {range_name!r}.", 422)
    return DateRange(range=range_name, start_date=s, end_date=e, timezone=tz_name)


def _in_range(business_id: uuid.UUID, rng: DateRange):
    ts = sale_timestamp_expr()
    return (Order.business_id == business_id) & (ts >= rng.start_utc) & (ts < rng.end_utc_exclusive)


def _empty(key: str) -> dict:
    return {
        "key": key, "label": WALLET_LABELS[key], "order_count": 0, "gross_cents": 0, "refunds_cents": 0,
        "net_cents": 0, "vat_cents": 0, "voided_count": 0, "voided_cents": 0, "payment_status_counts": {},
    }


def _add(target: dict, *, count: int, gross: int, refunds: int, vat: int, voided: int, voided_cents: int,
         payment_status: str) -> None:
    target["order_count"] += count
    target["gross_cents"] += gross
    target["refunds_cents"] += refunds
    target["net_cents"] += gross - refunds
    target["vat_cents"] += vat
    target["voided_count"] += voided
    target["voided_cents"] += voided_cents
    if count:
        counts = target["payment_status_counts"]
        counts[payment_status] = counts.get(payment_status, 0) + count


async def wallet_totals(db: AsyncSession, business_id: uuid.UUID, rng: DateRange) -> tuple[list[dict], dict]:
    """The three wallets and their total, from ONE grouped query over the tenant's orders.

    Grouped by source (one source -> one wallet via `wallet_for_source`), so
    every row is added to exactly one wallet and once to the total."""
    rows = (
        await db.execute(
            select(
                Order.source,
                Order.payment_status,
                sale_count_expr(), gross_cents_expr(), refunds_cents_expr(), vat_cents_expr(),
                voided_count_expr(), voided_cents_expr(),
            )
            .where(_in_range(business_id, rng))
            .group_by(Order.source, Order.payment_status)
        )
    ).all()
    wallets = {key: {**_empty(key), "sources": list(WALLET_SOURCES[key])} for key in WALLET_KEYS}
    total = _empty(TOTAL)
    for source, payment_status, count, gross, refunds, vat, voided, voided_cents in rows:
        wallet = wallet_for_source(source)
        figures = dict(count=int(count), gross=int(gross), refunds=int(refunds), vat=int(vat),
                       voided=int(voided), voided_cents=int(voided_cents), payment_status=payment_status)
        _add(wallets[wallet], **figures)
        _add(total, **figures)
    for key in (MR_D, UBER_EATS):
        wallets[key]["platform_fees"] = dict(_FEES_UNAVAILABLE)
        wallets[key]["settlement"] = dict(_SETTLEMENT_UNAVAILABLE)
    wallets[GENERAL]["platform_fees"] = None   # not applicable: the restaurant took this money itself
    wallets[GENERAL]["settlement"] = None
    return [wallets[key] for key in WALLET_KEYS], total


async def wallet_orders(
    db: AsyncSession, business_id: uuid.UUID, rng: DateRange, wallet: str, *, page: int, page_size: int
) -> dict:
    """One wallet's (or Total's) sale orders, newest first, with a per-source subtotal."""
    scope = _in_range(business_id, rng) & IS_SALE
    if wallet != TOTAL:
        scope = scope & (wallet_key_expr() == wallet)
    by_source = (
        await db.execute(
            select(Order.source, sale_count_expr(), gross_cents_expr(), refunds_cents_expr())
            .where(scope).group_by(Order.source)
        )
    ).all()
    total_count = sum(int(r[1]) for r in by_source)
    ts = sale_timestamp_expr()
    rows = (
        await db.execute(
            select(Order, ts.label("sale_at"))
            .where(scope)
            .order_by(ts.desc(), Order.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    orders = []
    for order, sale_at in rows:
        refund = int(order.refund_amount_cents or 0)
        orders.append({
            "id": str(order.id), "order_number": order.order_number, "source": order.source,
            "source_label": SOURCE_LABELS.get(order.source, order.source), "sale_at": sale_at.isoformat(),
            "status": order.status, "payment_status": order.payment_status, "payment_method": order.payment_method,
            "gross_cents": int(order.total_cents), "refund_cents": refund, "net_cents": int(order.total_cents) - refund,
            "external_order_id": order.external_order_id,
        })
    return {
        "page": page, "page_size": page_size, "total_count": total_count,
        "by_source": sorted(
            (
                {"source": s, "label": SOURCE_LABELS.get(s, s), "order_count": int(c),
                 "gross_cents": int(g), "refunds_cents": int(r), "net_cents": int(g) - int(r)}
                for s, c, g, r in by_source
            ),
            key=lambda x: -x["gross_cents"],
        ),
        "orders": orders,
    }
