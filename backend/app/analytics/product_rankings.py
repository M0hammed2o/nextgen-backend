"""
Best-selling products per channel (Uber Eats, Mr D, General, All) and a
single product's performance across those channels.

Read-only reporting over `order_items` joined to `orders`. Nothing is
written, and every figure is recomputed on each request.

What this module deliberately reuses rather than redefines:

  - "What counts as a sale" and "when it happened": `IS_SALE` and
    `sale_timestamp_expr()` from revenue.py, the same as Sales Wallets and
    the summary report. Cancelled, voided and unpaid funnel orders never
    rank.
  - The channel of an order: `sales_wallets.wallet_for_source`. Each order
    has one source, so each line lands in exactly one channel, and "All" is
    aggregated from the same rows rather than added up a second time.
  - Business-local date ranges: `sales_wallets.resolve_range`.

Grouping is by `order_items.menu_item_id`, the restaurant's own product id,
never by the line's name. Sizes and other options are variants of the same
product; add-ons are part of the line's price (`line_total_cents` already
includes them). A line with no `menu_item_id` (a dashboard-typed ad hoc
line, or history from before a product existed) is reported separately as
unmapped and never assigned to a product by its name.

Marketplace orders are mapped BEFORE they become sales: both the live
delivery path and the report import refuse to create an Order while any
item is unmapped (delivery/order_service.py, external_orders/service.py).
Live orders held for mapping are counted here as `awaiting_mapping` so the
owner can see sales missing from the ranking. Duplicates are prevented at
write time (the delivery-order and orders partial unique indexes, and the
report import's live-API duplicate check), so a sale has exactly one Order
and is counted once.

Refunds are recorded per order, not per line. A full refund is attributed
to every line of its order. A partial refund cannot be attributed to a
product without inventing an allocation, so a product touched by one has
no refund-adjusted figure; the amount is disclosed as unattributed instead.

Shares are of the channel's ITEM sales (sum of line totals), which exclude
delivery fees and order-level discounts, so they will not add up to the
Sales Wallets' order totals. The response says so.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.analytics.revenue import IS_SALE, local_sale_date_expr, sale_timestamp_expr
from backend.app.analytics.sales_wallets import (
    GENERAL,
    MR_D,
    UBER_EATS,
    DateRange,
    wallet_for_source,
)
from backend.app.core.errors import AppError
from shared.models.delivery import DeliveryOrder
from shared.models.menu import MenuItem
from shared.models.order import Order, OrderItem

ALL = "ALL"
CHANNELS: tuple[str, ...] = (UBER_EATS, MR_D, GENERAL, ALL)
CHANNEL_LABELS = {UBER_EATS: "Uber Eats", MR_D: "Mr D Food", GENERAL: "General Sales", ALL: "All Sales"}
#: Live delivery providers whose orders can be held for mapping, by channel.
_LIVE_PROVIDER = {UBER_EATS: "UBER_EATS", MR_D: "MR_D"}
RANK_BY = ("quantity", "revenue")

SHARE_BASIS = (
    "Shares are of the channel's item sales (line totals including add-ons). Delivery fees and "
    "order-level discounts are not item sales, so these totals differ from the Sales Wallets."
)
REFUND_BASIS = (
    "Refunds are recorded per order. A full refund is attributed to every product in the order. "
    "A partial refund cannot be attributed to a product without guessing, so it is shown as "
    "unattributed and that product has no refund-adjusted figure."
)

_ORDER_REFUND = func.coalesce(Order.refund_amount_cents, 0)
_FULLY_REFUNDED = (_ORDER_REFUND > 0) & (_ORDER_REFUND >= Order.total_cents)
_PARTIALLY_REFUNDED = (_ORDER_REFUND > 0) & (_ORDER_REFUND < Order.total_cents)


def _pct(part: int, whole: int) -> float | None:
    return round(part * 100.0 / whole, 1) if whole else None


def _scope(business_id: uuid.UUID, rng: DateRange):
    ts = sale_timestamp_expr()
    # business_id on BOTH tables: an order item can never be read through
    # another tenant's order, or the other way round.
    return (
        (Order.business_id == business_id)
        & (OrderItem.business_id == business_id)
        & (ts >= rng.start_utc)
        & (ts < rng.end_utc_exclusive)
        & IS_SALE
    )


def _blank() -> dict:
    return {"quantity": 0, "gross_cents": 0, "fully_refunded_cents": 0, "partial_refund_lines": 0}


async def _menu_items(db: AsyncSession, business_id: uuid.UUID, ids: set[uuid.UUID]) -> dict:
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(MenuItem.id, MenuItem.name, MenuItem.image_url, MenuItem.is_deleted)
            .where(MenuItem.business_id == business_id, MenuItem.id.in_(ids))
        )
    ).all()
    return {r.id: r for r in rows}


async def _awaiting_mapping(db: AsyncSession, business_id: uuid.UUID, rng: DateRange) -> dict[str, int]:
    """Live marketplace orders in range held because an item is unmapped (no sale exists yet)."""
    ts = func.coalesce(DeliveryOrder.placed_at, DeliveryOrder.created_at)
    rows = (
        await db.execute(
            select(DeliveryOrder.provider, func.count())
            .where(
                DeliveryOrder.business_id == business_id,
                DeliveryOrder.status == "NEEDS_MAPPING",
                ts >= rng.start_utc,
                ts < rng.end_utc_exclusive,
            )
            .group_by(DeliveryOrder.provider)
        )
    ).all()
    by_provider = {p: int(c) for p, c in rows}
    out = {ch: by_provider.get(_LIVE_PROVIDER[ch], 0) for ch in _LIVE_PROVIDER}
    out[GENERAL] = 0
    out[ALL] = sum(out.values())
    return out


async def best_sellers(
    db: AsyncSession, business_id: uuid.UUID, rng: DateRange, *, rank_by: str = "quantity", limit: int = 3,
) -> dict:
    """Top `limit` products for each channel and for all sales, from ONE grouped query."""
    if rank_by not in RANK_BY:
        raise AppError("INVALID_RANK_BY", f"rank_by must be one of {', '.join(RANK_BY)}.", 422)
    rows = (
        await db.execute(
            select(
                Order.source,
                OrderItem.menu_item_id,
                func.sum(OrderItem.quantity),
                func.sum(OrderItem.line_total_cents),
                func.sum(case((_FULLY_REFUNDED, OrderItem.line_total_cents), else_=0)),
                func.sum(case((_PARTIALLY_REFUNDED, 1), else_=0)),
                func.max(OrderItem.name_snapshot),
            )
            .join(Order, OrderItem.order_id == Order.id)
            .where(_scope(business_id, rng))
            .group_by(Order.source, OrderItem.menu_item_id)
        )
    ).all()

    products: dict[str, dict[uuid.UUID, dict]] = {ch: defaultdict(_blank) for ch in CHANNELS}
    unmapped = {ch: {"quantity": 0, "gross_cents": 0, "names": set()} for ch in CHANNELS}
    snapshot_names: dict[uuid.UUID, str] = {}
    for source, menu_item_id, qty, gross, fully, partial_lines, name in rows:
        for ch in (wallet_for_source(source), ALL):
            if menu_item_id is None:
                unmapped[ch]["quantity"] += int(qty)
                unmapped[ch]["gross_cents"] += int(gross)
                unmapped[ch]["names"].add(name)
                continue
            p = products[ch][menu_item_id]
            p["quantity"] += int(qty)
            p["gross_cents"] += int(gross)
            p["fully_refunded_cents"] += int(fully)
            p["partial_refund_lines"] += int(partial_lines)
        if menu_item_id is not None:
            snapshot_names.setdefault(menu_item_id, name)

    # Partially refunded orders' refund amounts, per channel, disclosed unallocated.
    partial = (
        await db.execute(
            select(Order.source, func.count(), func.sum(_ORDER_REFUND))
            .where(
                Order.business_id == business_id,
                (sale_timestamp_expr() >= rng.start_utc) & (sale_timestamp_expr() < rng.end_utc_exclusive),
                IS_SALE,
                _PARTIALLY_REFUNDED,
            )
            .group_by(Order.source)
        )
    ).all()
    unattributed = {ch: {"order_count": 0, "refunds_cents": 0} for ch in CHANNELS}
    for source, count, cents in partial:
        for ch in (wallet_for_source(source), ALL):
            unattributed[ch]["order_count"] += int(count)
            unattributed[ch]["refunds_cents"] += int(cents or 0)

    all_ids = {mid for ch in CHANNELS for mid in products[ch]}
    menu = await _menu_items(db, business_id, all_ids)
    awaiting = await _awaiting_mapping(db, business_id, rng)

    channels = []
    for ch in CHANNELS:
        items = products[ch]
        total_qty = sum(p["quantity"] for p in items.values()) + unmapped[ch]["quantity"]
        total_gross = sum(p["gross_cents"] for p in items.values()) + unmapped[ch]["gross_cents"]

        def name_of(mid: uuid.UUID) -> str:
            m = menu.get(mid)
            return m.name if m else snapshot_names.get(mid, "")

        primary = "quantity" if rank_by == "quantity" else "gross_cents"
        secondary = "gross_cents" if rank_by == "quantity" else "quantity"
        ordered = sorted(
            items.items(),
            key=lambda kv: (-kv[1][primary], -kv[1][secondary], name_of(kv[0]).lower(), str(kv[0])),
        )
        ranking = []
        for position, (mid, p) in enumerate(ordered[:limit], start=1):
            m = menu.get(mid)
            attributable = p["partial_refund_lines"] == 0
            ranking.append({
                "rank": position,
                "menu_item_id": str(mid),
                "name": name_of(mid),
                "image_url": m.image_url if m else None,
                "is_deleted": bool(m.is_deleted) if m else True,
                "quantity": p["quantity"],
                "gross_cents": p["gross_cents"],
                "refund_adjusted_cents": p["gross_cents"] - p["fully_refunded_cents"] if attributable else None,
                "has_unattributed_refund": not attributable,
                "quantity_share_pct": _pct(p["quantity"], total_qty),
                "revenue_share_pct": _pct(p["gross_cents"], total_gross),
            })
        channels.append({
            "key": ch,
            "label": CHANNEL_LABELS[ch],
            "ranking": ranking,
            "product_count": len(items),
            "total_quantity": total_qty,
            "total_item_gross_cents": total_gross,
            "unmapped": {
                "quantity": unmapped[ch]["quantity"],
                "gross_cents": unmapped[ch]["gross_cents"],
                "names": sorted(n for n in unmapped[ch]["names"] if n)[:10],
            },
            "unattributed_refunds": unattributed[ch],
            "awaiting_mapping_orders": awaiting.get(ch, 0),
        })
    return {"rank_by": rank_by, "share_basis": SHARE_BASIS, "refund_basis": REFUND_BASIS, "channels": channels}


def _variant_label(selected_options: list | None) -> str:
    names = [str(o.get("option_name")) for o in (selected_options or []) if isinstance(o, dict) and o.get("option_name")]
    return ", ".join(names) if names else "Standard"


async def product_performance(
    db: AsyncSession, business_id: uuid.UUID, rng: DateRange, menu_item_id: uuid.UUID,
) -> dict:
    """One product across Uber Eats, Mr D and General: units, revenue, share and a daily trend."""
    menu = await _menu_items(db, business_id, {menu_item_id})
    scope = _scope(business_id, rng) & (OrderItem.menu_item_id == menu_item_id)
    day = local_sale_date_expr(rng.timezone)
    rows = (
        await db.execute(
            select(Order.source, day.label("day"), func.sum(OrderItem.quantity), func.sum(OrderItem.line_total_cents))
            .join(Order, OrderItem.order_id == Order.id)
            .where(scope)
            .group_by(Order.source, day)
        )
    ).all()
    if menu_item_id not in menu and not rows:
        # Unknown to this tenant: same answer whether it exists elsewhere or not.
        raise AppError("NOT_FOUND", "Product not found.", 404)

    channel_keys = (UBER_EATS, MR_D, GENERAL)
    by_channel = {ch: {"quantity": 0, "gross_cents": 0} for ch in channel_keys}
    daily: dict = defaultdict(lambda: {ch: {"quantity": 0, "gross_cents": 0} for ch in channel_keys})
    for source, d, qty, gross in rows:
        ch = wallet_for_source(source)
        by_channel[ch]["quantity"] += int(qty)
        by_channel[ch]["gross_cents"] += int(gross)
        daily[d][ch]["quantity"] += int(qty)
        daily[d][ch]["gross_cents"] += int(gross)

    variant_rows = (
        await db.execute(
            select(OrderItem.selected_options_snapshot, func.sum(OrderItem.quantity), func.sum(OrderItem.line_total_cents))
            .join(Order, OrderItem.order_id == Order.id)
            .where(scope)
            .group_by(OrderItem.selected_options_snapshot)
        )
    ).all()
    variants: dict[str, dict] = defaultdict(lambda: {"quantity": 0, "gross_cents": 0})
    for options, qty, gross in variant_rows:
        v = variants[_variant_label(options)]
        v["quantity"] += int(qty)
        v["gross_cents"] += int(gross)

    total_qty = sum(c["quantity"] for c in by_channel.values())
    total_gross = sum(c["gross_cents"] for c in by_channel.values())
    trend = []
    cursor = rng.start_date
    while cursor <= rng.end_date:
        entry = daily.get(cursor)
        trend.append({
            "day": cursor.isoformat(),
            **{ch.lower(): (entry[ch] if entry else {"quantity": 0, "gross_cents": 0}) for ch in channel_keys},
        })
        cursor += timedelta(days=1)

    m = menu.get(menu_item_id)
    snapshot = None
    if m is None:
        snapshot = (
            await db.execute(
                select(func.max(OrderItem.name_snapshot)).where(
                    OrderItem.business_id == business_id, OrderItem.menu_item_id == menu_item_id
                )
            )
        ).scalar()
    return {
        "menu_item_id": str(menu_item_id),
        "name": m.name if m else (snapshot or ""),
        "image_url": m.image_url if m else None,
        "is_deleted": bool(m.is_deleted) if m else True,
        "total_quantity": total_qty,
        "total_gross_cents": total_gross,
        "channels": [
            {
                "key": ch, "label": CHANNEL_LABELS[ch], **by_channel[ch],
                "quantity_share_pct": _pct(by_channel[ch]["quantity"], total_qty),
                "revenue_share_pct": _pct(by_channel[ch]["gross_cents"], total_gross),
            }
            for ch in channel_keys
        ],
        "variants": sorted(
            ({"label": k, **v} for k, v in variants.items()), key=lambda x: (-x["quantity"], x["label"])
        ),
        "daily": trend,
    }
