"""
Stock consumption reversal on order cancellation.

One rule, used by every path that can move an order to CANCELLED (the
staff/manager status-change route, the two ARQ auto-cancel cron jobs, and
the WhatsApp bot's own cancel/supersede/reject-delivery-fee paths):

    Stock consumed for a sale is only put back if the order never reached
    the kitchen. Once an order is IN_PROGRESS or READY, cancelling it no
    longer un-cooks the food, so the deduction stays and we record why
    instead of silently reappearing stock that was actually used.

The pre-preparation set is derived from shared.enums.ORDER_STATUS_TRANSITIONS:
every status that only ever leads to IN_PROGRESS (or another pre-prep
status), never the other way around.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inventory.ledger import reverse_sale_consumption
from shared.enums import AuditScope, OrderStatus
from shared.models.audit import AuditEvent

# Statuses from which a cancellation fully reverses stock consumption because
# the order never reached preparation. IN_PROGRESS and every status after it
# are deliberately excluded — the food was already made.
PRE_PREPARATION_STATUSES = frozenset(
    {
        OrderStatus.PENDING_DELIVERY_FEE.value,
        OrderStatus.FEE_SENT.value,
        OrderStatus.NEW.value,
        OrderStatus.ACCEPTED.value,
    }
)


async def reverse_consumption_on_cancel(
    db: AsyncSession,
    *,
    business_id: uuid.UUID,
    inventory_enabled: bool,
    order_id: uuid.UUID,
    previous_status: str,
    created_by_user_id: uuid.UUID | None = None,
) -> None:
    """
    Call inside the same transaction that writes an order's CANCELLED status.

    - `previous_status` still pre-preparation → reverse any posted sale
      consumption (idempotent: a second call for the same order writes
      nothing, via reverse_sale_consumption's own already-reversed check).
    - `previous_status` already IN_PROGRESS/READY/etc. (food was made) →
      leave the deduction in place and record why.
    - Inventory not enabled for this tenant → no-op; nothing was ever posted.
    """
    if not inventory_enabled:
        return

    if previous_status in PRE_PREPARATION_STATUSES:
        await reverse_sale_consumption(
            db,
            business_id=business_id,
            order_id=order_id,
            created_by_user_id=created_by_user_id,
            notes="Order cancelled before preparation started",
        )
        return

    db.add(
        AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=business_id,
            actor_user_id=created_by_user_id,
            action="inventory.sale.stock_kept_consumed",
            target_type="order",
            target_id=order_id,
            diff_json={
                "previous_status": previous_status,
                "note": "Order cancelled after preparation started — stock "
                        "deduction was not reversed because the food was "
                        "already made",
            },
        )
    )
