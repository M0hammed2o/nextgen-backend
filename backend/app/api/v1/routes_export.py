"""
Order export — CSV download for business managers.
"""

import csv
import io
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from shared.models.order import Order, OrderItem
from shared.utils.money import format_currency

router = APIRouter(prefix="/business/orders", tags=["orders-export"])


@router.get("/export")
async def export_orders_csv(
    days: int = Query(default=30, ge=1, le=365),
    status: str | None = None,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """Export orders as a CSV file for the specified period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    query = (
        select(Order)
        .where(
            Order.business_id == user.business_id,
            Order.created_at >= cutoff,
        )
        .order_by(Order.created_at.desc())
    )
    if status:
        query = query.where(Order.status == status)

    result = await db.execute(query)
    orders = result.scalars().all()

    # Build CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Order Number", "Status", "Mode", "Source",
        "Customer Name", "Phone", "Delivery Address",
        "Items", "Subtotal", "Delivery Fee", "Total",
        "Created At", "Accepted At", "Completed At",
    ])

    for order in orders:
        # Load items for each order
        items_result = await db.execute(
            select(OrderItem).where(OrderItem.order_id == order.id)
        )
        items = items_result.scalars().all()
        items_text = "; ".join(
            f"{i.quantity}x {i.name_snapshot}" for i in items
        )

        writer.writerow([
            order.order_number,
            order.status,
            order.order_mode,
            order.source,
            order.customer_name or "",
            order.phone_number or "",
            order.delivery_address or "",
            items_text,
            format_currency(order.subtotal_cents, order.currency),
            format_currency(order.delivery_fee_cents, order.currency),
            format_currency(order.total_cents, order.currency),
            order.created_at.isoformat(),
            order.accepted_at.isoformat() if order.accepted_at else "",
            order.completed_at.isoformat() if order.completed_at else "",
        ])

    output.seek(0)
    filename = f"orders-{datetime.now().strftime('%Y%m%d')}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
