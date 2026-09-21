"""
Order export — CSV download for business managers.
"""

import csv
import io
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.order import Order, OrderItem
from shared.utils.money import format_currency

router = APIRouter(prefix="/business/orders", tags=["orders-export"])

# A CSV opened in Excel/Sheets treats a cell starting with one of these as a
# formula to evaluate, not literal text — the classic CSV/formula-injection
# vector. customer_name, phone_number, delivery_address, and item names all
# come from a WhatsApp customer's free-text input and are never validated
# as "not a formula", so every one of them is a real injection surface.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# A hard cap on how many orders one export can return. Before this fix, a
# 365-day export ran one OrderItem query PER order with no limit at all —
# unbounded query count and response size. Rather than silently truncating
# (which would produce an export that LOOKS complete but isn't — worse for
# an accountant than an explicit error), a business over the cap gets a
# clear error asking it to narrow the date range instead.
MAX_EXPORT_ORDERS = 10_000


def _csv_safe(value) -> str:
    """
    Neutralise formula injection on a free-text, customer-controlled cell
    by prefixing a leading =, +, -, @, TAB, or CR with a single quote,
    which forces spreadsheet software to render it as literal text.

    Only ever applied to customer-controlled text fields — NEVER to the
    numeric currency columns this module formats itself. format_currency
    always leads with a currency symbol (e.g. "R-15.50" for a refund, not
    "-15.50"), so a legitimate negative amount is unaffected either way,
    but this function must not be called on it regardless.
    """
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in _FORMULA_PREFIXES:
        return "'" + text
    return text


@router.get("/export")
async def export_orders_csv(
    days: int = Query(default=30, ge=1, le=365),
    status: str | None = None,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Export orders as a CSV file for the specified period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    base_filters = [Order.business_id == user.business_id, Order.created_at >= cutoff]
    if status:
        base_filters.append(Order.status == status)

    count_result = await db.execute(select(func.count()).select_from(Order).where(*base_filters))
    total_orders = count_result.scalar_one()
    if total_orders > MAX_EXPORT_ORDERS:
        raise AppError(
            "EXPORT_TOO_LARGE",
            f"This export would include {total_orders} orders, over the "
            f"{MAX_EXPORT_ORDERS}-order limit for a single download. "
            "Narrow the date range (or filter by status) and try again.",
            422,
        )

    query = select(Order).where(*base_filters).order_by(Order.created_at.desc())
    result = await db.execute(query)
    orders = result.scalars().all()

    # One batched item query for every order in this export, instead of one
    # query per order — still scoped to business_id, same tenant-isolation
    # guarantee as the original per-order query.
    order_ids = [order.id for order in orders]
    items_by_order: dict[uuid.UUID, list[OrderItem]] = defaultdict(list)
    if order_ids:
        items_result = await db.execute(
            select(OrderItem).where(
                OrderItem.order_id.in_(order_ids),
                OrderItem.business_id == user.business_id,
            )
        )
        for item in items_result.scalars().all():
            items_by_order[item.order_id].append(item)

    # Build CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Order Number", "Status", "Mode", "Source",
        "Customer Name", "Phone", "Delivery Address",
        "Items", "Subtotal", "Delivery Fee", "Total",
        "Created At", "Accepted At", "Completed At",
        # Accountant columns — appended at the end so existing column
        # positions/indexes are unaffected for anything already parsing
        # this export.
        "Discount", "VAT", "Payment Method", "Payment Status", "Refund Amount",
    ])

    for order in orders:
        items = items_by_order.get(order.id, [])
        items_text = "; ".join(
            _csv_safe(f"{i.quantity}x {i.name_snapshot}") for i in items
        )

        writer.writerow([
            order.order_number,
            order.status,
            order.order_mode,
            order.source,
            _csv_safe(order.customer_name) if order.customer_name else "",
            _csv_safe(order.phone_number) if order.phone_number else "",
            _csv_safe(order.delivery_address) if order.delivery_address else "",
            items_text,
            format_currency(order.subtotal_cents, order.currency),
            format_currency(order.delivery_fee_cents, order.currency),
            format_currency(order.total_cents, order.currency),
            order.created_at.isoformat(),
            order.accepted_at.isoformat() if order.accepted_at else "",
            order.completed_at.isoformat() if order.completed_at else "",
            format_currency(order.discount_cents, order.currency),
            format_currency(order.tax_cents, order.currency),
            order.payment_method or "",
            order.payment_status or "",
            format_currency(order.refund_amount_cents, order.currency) if order.refund_amount_cents else "",
        ])

    output.seek(0)
    filename = f"orders-{datetime.now().strftime('%Y%m%d')}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
