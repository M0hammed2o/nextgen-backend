"""
Order routes — list (live + history), detail, status update.
Staff, Manager, and Owner can access. Status transitions are validated.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import InvalidTransitionError, NotFoundError
from backend.app.core.pagination import CursorParams, PaginatedResponse, PaginationMeta, encode_cursor
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from shared.enums import ORDER_STATUS_TRANSITIONS, OrderStatus
from shared.models.business import Business
from shared.models.order import Order, OrderEvent, OrderItem
from shared.utils import format_order_number

router = APIRouter(prefix="/business/orders", tags=["orders"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class OrderItemResponse(BaseModel):
    id: uuid.UUID
    name_snapshot: str
    unit_price_cents: int
    quantity: int
    line_total_cents: int
    options_snapshot: dict | None
    special_instructions: str | None
    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: uuid.UUID
    order_number: str
    status: str
    payment_status: str
    order_mode: str
    source: str
    subtotal_cents: int
    delivery_fee_cents: int
    total_cents: int
    currency: str
    customer_name: str | None
    phone_number: str | None
    delivery_address: str | None
    estimated_ready_at: datetime | None
    confirmed_at: datetime | None
    accepted_at: datetime | None
    ready_at: datetime | None
    completed_at: datetime | None
    cancelled_reason: str | None
    items: list[OrderItemResponse] = []
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class StatusUpdateRequest(BaseModel):
    status: str
    reason: str | None = None
    estimated_ready_minutes: int | None = Field(default=None, ge=1, le=180)


class OrderListParams(BaseModel):
    status: str | None = None
    live: bool = False  # Only active statuses (NEW, ACCEPTED, IN_PROGRESS, READY)
    cursor: str | None = None
    limit: int = Field(default=25, ge=1, le=100)


# ── Create Order (manual / dashboard) ────────────────────────────────────────

class OrderItemCreate(BaseModel):
    """Single line item in a manually created order."""
    name: str = Field(max_length=255)
    qty: int = Field(ge=1, le=999)
    price_cents: int = Field(ge=0, description="Unit price in cents")
    options: dict | None = None
    special_instructions: str | None = None


class OrderCreate(BaseModel):
    """Create an order from the dashboard (manual order)."""
    customer_name: str | None = Field(default=None, max_length=255)
    customer_phone: str | None = Field(default=None, max_length=32)
    delivery_address: str | None = None
    items: list[OrderItemCreate] = Field(min_length=1)
    notes: str | None = None
    order_mode: str = Field(default="PICKUP", pattern=r"^(PICKUP|DELIVERY|DINE_IN)$")
    source: str = Field(default="MANUAL", pattern=r"^(MANUAL|ADMIN|WHATSAPP)$")


# ── Routes ───────────────────────────────────────────────────────────────────

LIVE_STATUSES = [
    OrderStatus.NEW.value,
    OrderStatus.ACCEPTED.value,
    OrderStatus.IN_PROGRESS.value,
    OrderStatus.READY.value,
]


# ═════════════════════════════════════════════════════════════════════════════
# NEW: Feature 4 — Create order from dashboard
# ═════════════════════════════════════════════════════════════════════════════

@router.post("", response_model=OrderResponse, status_code=201)
async def create_order(
    body: OrderCreate,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a manual order from the dashboard.
    Calculates totals, assigns order number, creates items.
    """
    from sqlalchemy import update as sa_update

    # Atomic increment of business order_number_sequence
    result = await db.execute(
        sa_update(Business)
        .where(Business.id == user.business_id)
        .values(order_number_sequence=Business.order_number_sequence + 1)
        .returning(Business.order_number_sequence, Business.currency, Business.delivery_fee_cents)
    )
    row = result.one_or_none()
    if not row:
        raise NotFoundError("Business")

    seq, currency, biz_delivery_fee = row

    # Calculate totals
    subtotal_cents = sum(item.qty * item.price_cents for item in body.items)
    delivery_fee = biz_delivery_fee if body.order_mode == "DELIVERY" else 0
    total_cents = subtotal_cents + delivery_fee

    order = Order(
        business_id=user.business_id,
        order_number=format_order_number(seq),
        status=OrderStatus.NEW.value,
        order_mode=body.order_mode,
        source=body.source,
        subtotal_cents=subtotal_cents,
        delivery_fee_cents=delivery_fee,
        total_cents=total_cents,
        currency=currency,
        customer_name=body.customer_name,
        phone_number=body.customer_phone,
        delivery_address=body.delivery_address,
        confirmed_at=datetime.now(timezone.utc),
    )
    db.add(order)
    await db.flush()

    # Create order items
    for item in body.items:
        oi = OrderItem(
            order_id=order.id,
            business_id=user.business_id,
            name_snapshot=item.name,
            unit_price_cents=item.price_cents,
            quantity=item.qty,
            line_total_cents=item.qty * item.price_cents,
            options_snapshot=item.options,
            special_instructions=item.special_instructions or body.notes,
        )
        db.add(oi)

    # Create initial event
    event = OrderEvent(
        order_id=order.id,
        business_id=user.business_id,
        old_status=None,
        new_status=OrderStatus.NEW.value,
        changed_by_user_id=user.user_id,
        reason="Manual order created from dashboard",
    )
    db.add(event)

    await db.commit()
    await db.refresh(order)

    # Publish SSE (fire and forget)
    try:
        from backend.app.db.session import get_redis
        import json
        redis = await get_redis()
        await redis.publish(
            f"orders:{user.business_id}",
            json.dumps({
                "type": "new_order",
                "order_id": str(order.id),
                "order_number": order.order_number,
                "status": order.status,
                "total_cents": order.total_cents,
                "created_at": order.created_at.isoformat(),
            }),
        )
    except Exception:
        pass

    return OrderResponse.model_validate(order)


@router.get("", response_model=PaginatedResponse)
async def list_orders(
    status: str | None = None,
    live: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    List orders for the business.
    - ?live=true: only active orders (for staff dashboard)
    - ?status=NEW: filter by specific status
    - Cursor-based pagination
    """
    query = select(Order).where(Order.business_id == user.business_id)

    if live:
        query = query.where(Order.status.in_(LIVE_STATUSES))
    elif status:
        query = query.where(Order.status == status)

    if cursor:
        from backend.app.core.pagination import decode_cursor
        cursor_ts, cursor_id = decode_cursor(cursor)
        query = query.where(
            (Order.created_at < cursor_ts) |
            (and_(Order.created_at == cursor_ts, Order.id < cursor_id))
        )

    query = query.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit + 1)
    result = await db.execute(query)
    orders = list(result.scalars().all())

    has_more = len(orders) > limit
    if has_more:
        orders = orders[:limit]

    next_cursor = None
    if has_more and orders:
        last = orders[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return PaginatedResponse(
        data=[OrderResponse.model_validate(o) for o in orders],
        pagination=PaginationMeta(
            per_page=limit,
            next_cursor=next_cursor,
            has_more=has_more,
        ),
    )


@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: uuid.UUID,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Get order details including items."""
    result = await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.business_id == user.business_id,
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError("Order", str(order_id))
    return OrderResponse.model_validate(order)


@router.post("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: uuid.UUID,
    body: StatusUpdateRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Update order status with transition validation.
    Creates an OrderEvent audit record.
    Publishes to Redis for SSE live updates.
    """
    result = await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.business_id == user.business_id,
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError("Order", str(order_id))

    # ── Validate transition ──────────────────────────────────────────────
    current = OrderStatus(order.status)
    requested = OrderStatus(body.status)

    allowed = ORDER_STATUS_TRANSITIONS.get(current, [])
    if requested not in allowed:
        raise InvalidTransitionError(current.value, requested.value)

    old_status = order.status
    order.status = requested.value
    now = datetime.now(timezone.utc)

    # Set milestone timestamps
    if requested == OrderStatus.ACCEPTED:
        order.accepted_at = now
        if body.estimated_ready_minutes:
            from datetime import timedelta
            order.estimated_ready_at = now + timedelta(minutes=body.estimated_ready_minutes)
    elif requested == OrderStatus.READY:
        order.ready_at = now
    elif requested in (OrderStatus.COLLECTED, OrderStatus.DELIVERED):
        order.completed_at = now
    elif requested == OrderStatus.CANCELLED:
        order.cancelled_reason = body.reason
        order.cancelled_by_user_id = user.user_id

    # ── Create audit event ───────────────────────────────────────────────
    event = OrderEvent(
        order_id=order.id,
        business_id=user.business_id,
        old_status=old_status,
        new_status=requested.value,
        changed_by_user_id=user.user_id,
        reason=body.reason,
    )
    db.add(event)

    await db.commit()
    await db.refresh(order)

    # ── Publish SSE event (fire and forget) ──────────────────────────────
    try:
        from backend.app.db.session import get_redis
        redis = await get_redis()
        import json
        await redis.publish(
            f"orders:{user.business_id}",
            json.dumps({
                "type": "order_status_changed",
                "order_id": str(order.id),
                "order_number": order.order_number,
                "old_status": old_status,
                "new_status": requested.value,
                "total_cents": order.total_cents,
                "updated_at": now.isoformat(),
            }),
        )
    except Exception:
        import logging
        logging.getLogger("nextgen").warning("Failed to publish SSE event for order %s", order.id)

    return OrderResponse.model_validate(order)


class PaymentUpdateRequest(BaseModel):
    payment_status: str = Field(pattern=r"^(PENDING|PAID|CASH_ON_COLLECTION)$")


@router.patch("/{order_id}/payment", response_model=OrderResponse)
async def update_payment_status(
    order_id: uuid.UUID,
    body: PaymentUpdateRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Update payment status on an order.
    Staff / Manager / Owner can mark PENDING → PAID or CASH_ON_COLLECTION.
    """
    result = await db.execute(
        select(Order).where(
            Order.id == order_id,
            Order.business_id == user.business_id,
        )
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError("Order", str(order_id))

    order.payment_status = body.payment_status
    await db.commit()
    await db.refresh(order)
    return OrderResponse.model_validate(order)
