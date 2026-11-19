"""
Sales by product variant (size/colour/SKU) over a business-local date range.

Read-only over ``order_items`` joined to ``orders``; reuses the same sale
predicate (``IS_SALE``), timestamp and date-range helpers as every other
report, so cancelled/voided/unpaid orders never count and a sale is counted
exactly once (one row per order line, grouped by variant_id - no join that
could fan out).

Returns: a line is returned either through a line-level POS refund
(``order_items.returned_quantity``) or because its whole order was fully
refunded without line detail (then every unit counts as returned).
Returned value is at the line's unit price, BEFORE any order-level discount;
the order-level money truth stays in the existing revenue/refund reports.
"""
from __future__ import annotations

import uuid

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.analytics.product_rankings import _FULLY_REFUNDED, _scope
from backend.app.analytics.sales_wallets import DateRange
from shared.models.order import Order, OrderItem

BASIS = (
    "Units and gross are what was rung up. Returned counts line-level returns plus every unit of a fully "
    "refunded order, valued at the line's unit price before order-level discounts. Net = gross - returned."
)


async def variant_sales(
    db: AsyncSession, business_id: uuid.UUID, rng: DateRange, *, menu_item_id: uuid.UUID | None = None,
) -> dict:
    returned_units = case((_FULLY_REFUNDED, OrderItem.quantity), else_=OrderItem.returned_quantity)
    scope = _scope(business_id, rng) & OrderItem.variant_id.is_not(None)
    if menu_item_id is not None:
        scope = scope & (OrderItem.menu_item_id == menu_item_id)
    rows = (
        await db.execute(
            select(
                OrderItem.variant_id,
                OrderItem.menu_item_id,
                func.max(OrderItem.name_snapshot),
                func.max(OrderItem.sku_snapshot),
                func.max(OrderItem.variant_label_snapshot),
                func.sum(OrderItem.quantity),
                func.sum(OrderItem.line_total_cents),
                func.sum(returned_units),
                func.sum(returned_units * OrderItem.unit_price_cents),
            )
            .join(Order, OrderItem.order_id == Order.id)
            .where(scope)
            .group_by(OrderItem.variant_id, OrderItem.menu_item_id)
        )
    ).all()
    variants = []
    for vid, mid, name, sku, label, qty, gross, ret, ret_cents in rows:
        qty, gross, ret, ret_cents = int(qty), int(gross), int(ret or 0), int(ret_cents or 0)
        variants.append({
            "variant_id": str(vid), "menu_item_id": str(mid) if mid else None, "name": name, "sku": sku,
            "label": label, "units_sold": qty, "gross_cents": gross, "units_returned": ret,
            "returned_cents": ret_cents, "net_units": qty - ret, "net_cents": gross - ret_cents,
        })
    variants.sort(key=lambda v: (-v["net_units"], v["sku"] or ""))
    totals = {k: sum(v[k] for v in variants) for k in
              ("units_sold", "gross_cents", "units_returned", "returned_cents", "net_units", "net_cents")}
    return {"variants": variants, "totals": totals, "basis": BASIS}
