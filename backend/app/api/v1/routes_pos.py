"""
POS routes — order creation and till (cash-up) management for staff ringing
up in-person orders.

Unlike the existing dashboard manual-order endpoint (POST /business/orders),
every price here is computed server-side from the live menu catalog via
shared/pricing/engine.py — the client never submits a price_cents value.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_orders import OrderResponse
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.pos.pricing import POSAddOnSelection, POSItemInput, resolve_pos_items
from backend.app.pos.till import compute_expected_cash_cents, compute_variance_cents
from shared.enums import AuditScope, OrderSource, OrderStatus
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.order import Order, OrderEvent, OrderItem
from shared.models.till import TillSession
from shared.utils import format_order_number

router = APIRouter(prefix="/business/pos", tags=["pos"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class POSAddOnSelectionRequest(BaseModel):
    add_on_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=99)


class POSOrderItemRequest(BaseModel):
    menu_item_id: uuid.UUID
    quantity: int = Field(ge=1, le=999)
    selected_option_ids: dict[str, str] = Field(default_factory=dict)
    add_on_selections: list[POSAddOnSelectionRequest] = Field(default_factory=list)
    special_instructions: str | None = None


class POSOrderCreateRequest(BaseModel):
    items: list[POSOrderItemRequest] = Field(min_length=1)
    order_mode: str = Field(default="PICKUP", pattern=r"^(PICKUP|DELIVERY|DINE_IN)$")
    customer_name: str | None = Field(default=None, max_length=255)
    customer_phone: str | None = Field(default=None, max_length=32)
    discount_cents: int = Field(default=0, ge=0)
    discount_reason: str | None = Field(default=None, max_length=255)


class TillOpenRequest(BaseModel):
    opening_float_cents: int = Field(ge=0)


class TillCloseRequest(BaseModel):
    counted_cash_cents: int = Field(ge=0)


class TillSessionResponse(BaseModel):
    id: uuid.UUID
    status: str
    opening_float_cents: int
    expected_cash_cents: int | None
    counted_cash_cents: int | None
    variance_cents: int | None
    opened_at: datetime
    closed_at: datetime | None
    model_config = {"from_attributes": True}


# ── Order creation ───────────────────────────────────────────────────────────

@router.post("/orders", response_model=OrderResponse, status_code=201)
async def create_pos_order(
    body: POSOrderCreateRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Ring up an order at the till. Every menu item / option / add-on ID is
    re-validated against this business's live catalog and priced via
    shared/pricing/engine.py — no client-submitted price is ever trusted.
    """
    item_inputs = [
        POSItemInput(
            menu_item_id=item.menu_item_id,
            quantity=item.quantity,
            selected_option_ids=item.selected_option_ids,
            add_on_selections=[
                POSAddOnSelection(add_on_id=sel.add_on_id, quantity=sel.quantity)
                for sel in item.add_on_selections
            ],
            special_instructions=item.special_instructions,
        )
        for item in body.items
    ]

    # Resolve and price every line item against the live menu BEFORE any
    # mutation — an invalid item/option/add-on must never consume an order
    # number or leave a partial order behind.
    resolved_items = await resolve_pos_items(db, user.business_id, item_inputs)
    subtotal_cents = sum(r.breakdown.line_total_cents for r in resolved_items)

    if body.discount_cents > subtotal_cents:
        raise AppError(
            "DISCOUNT_EXCEEDS_SUBTOTAL",
            "Discount cannot exceed the order subtotal",
            422,
        )

    biz_result = await db.execute(
        select(Business.max_staff_discount_cents).where(Business.id == user.business_id)
    )
    max_staff_discount = biz_result.scalar_one_or_none() or 0
    if body.discount_cents > 0 and user.role != "OWNER" and body.discount_cents > max_staff_discount:
        raise AppError(
            "DISCOUNT_LIMIT_EXCEEDED",
            f"This discount requires owner approval (staff limit is {max_staff_discount} cents)",
            403,
        )

    # ── Atomic order-number increment (same pattern as the manual-order route) ─
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

    delivery_fee_cents = biz_delivery_fee if body.order_mode == "DELIVERY" else 0
    total_cents = subtotal_cents - body.discount_cents + delivery_fee_cents

    # Link a Customer record by phone number when loyalty is enabled, so
    # points can be awarded once this order is later marked PAID. Never
    # required — a POS order with no phone number just earns no points.
    customer_id = None
    if body.customer_phone:
        from backend.app.loyalty.engine import get_or_create_customer_by_phone
        from shared.models.loyalty import LoyaltyProgram
        program_result = await db.execute(
            select(LoyaltyProgram).where(LoyaltyProgram.business_id == user.business_id)
        )
        program = program_result.scalar_one_or_none()
        if program and program.is_enabled:
            customer = await get_or_create_customer_by_phone(
                db, user.business_id, body.customer_phone, body.customer_name,
            )
            customer_id = customer.id

    order = Order(
        business_id=user.business_id,
        customer_id=customer_id,
        order_number=format_order_number(seq),
        status=OrderStatus.NEW.value,
        payment_status="PENDING",
        payment_required=False,
        order_mode=body.order_mode,
        source=OrderSource.POS.value,
        subtotal_cents=subtotal_cents,
        delivery_fee_cents=delivery_fee_cents,
        discount_cents=body.discount_cents,
        tax_cents=0,  # No tax-rate configuration exists yet (Phase 1 scope) — always 0 for now.
        discount_reason=body.discount_reason,
        discount_approved_by_user_id=user.user_id if body.discount_cents > 0 else None,
        total_cents=max(0, total_cents),
        currency=currency,
        customer_name=body.customer_name,
        phone_number=body.customer_phone,
        confirmed_at=datetime.now(timezone.utc),
    )
    db.add(order)
    await db.flush()

    for resolved in resolved_items:
        db.add(OrderItem(
            order_id=order.id,
            business_id=user.business_id,
            menu_item_id=resolved.menu_item_id,
            name_snapshot=resolved.name,
            base_price_cents=resolved.breakdown.base_price_cents,
            option_adjustment_cents=resolved.breakdown.option_adjustment_cents,
            add_on_total_cents=resolved.breakdown.add_on_total_cents,
            unit_price_cents=resolved.breakdown.unit_price_cents,
            quantity=resolved.breakdown.quantity,
            line_total_cents=resolved.breakdown.line_total_cents,
            selected_options_snapshot=resolved.selected_options_snapshot or None,
            add_ons_snapshot=resolved.add_ons_snapshot or None,
            special_instructions=resolved.special_instructions,
        ))

    db.add(OrderEvent(
        order_id=order.id,
        business_id=user.business_id,
        old_status=None,
        new_status=OrderStatus.NEW.value,
        changed_by_user_id=user.user_id,
        reason="POS order created",
    ))

    if body.discount_cents > 0:
        db.add(AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="order.pos.discount_applied",
            target_type="order",
            target_id=order.id,
            diff_json={
                "discount_cents": body.discount_cents,
                "discount_reason": body.discount_reason,
                "actor_role": user.role,
            },
        ))

    await db.commit()
    await db.refresh(order)

    # Record usage (fire and forget) — see routes_orders.py::create_order
    # for why every order-creation path must call this, not just the bot.
    try:
        from backend.app.bot.usage_tracker import increment_usage
        await increment_usage(
            db, user.business_id, "Africa/Johannesburg",
            orders_created=1, revenue_cents=order.total_cents,
        )
    except Exception:
        import logging
        logging.getLogger("nextgen").warning("Failed to record usage for POS order %s", order.id)

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


# ── Till (cash-up) ───────────────────────────────────────────────────────────

@router.get("/till/current", response_model=TillSessionResponse | None)
async def get_current_till(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    The business's currently OPEN till session, or null if none — lets the
    cash-up UI know whether to show "Open Till" or "Close Till" without the
    client having to remember a session ID across page reloads.
    """
    result = await db.execute(
        select(TillSession).where(
            TillSession.business_id == user.business_id,
            TillSession.status == "OPEN",
        )
    )
    session = result.scalar_one_or_none()
    return TillSessionResponse.model_validate(session) if session else None


@router.post("/till/open", response_model=TillSessionResponse, status_code=201)
async def open_till(
    body: TillOpenRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(TillSession).where(
            TillSession.business_id == user.business_id,
            TillSession.status == "OPEN",
        )
    )
    if existing.scalar_one_or_none():
        raise AppError("TILL_ALREADY_OPEN", "A till session is already open for this business", 409)

    session = TillSession(
        business_id=user.business_id,
        opened_by_user_id=user.user_id,
        opening_float_cents=body.opening_float_cents,
        status="OPEN",
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return TillSessionResponse.model_validate(session)


@router.post("/till/{session_id}/close", response_model=TillSessionResponse)
async def close_till(
    session_id: uuid.UUID,
    body: TillCloseRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Manager/owner sign-off required to close — a cashier cannot approve
    their own cash-up. Closing is a one-way transition; an already-closed
    session is rejected rather than silently re-mutated.
    """
    result = await db.execute(
        select(TillSession).where(
            TillSession.id == session_id,
            TillSession.business_id == user.business_id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise NotFoundError("TillSession", str(session_id))
    if session.status != "OPEN":
        raise AppError("TILL_ALREADY_CLOSED", "This till session has already been closed", 422)

    cash_result = await db.execute(
        select(Order.total_cents).where(
            Order.business_id == user.business_id,
            Order.payment_method == "CASH",
            Order.payment_status == "PAID",
            Order.paid_at.is_not(None),
            Order.paid_at >= session.opened_at,
        )
    )
    cash_sales_cents = sum(cash_result.scalars().all())

    expected = compute_expected_cash_cents(session.opening_float_cents, cash_sales_cents)
    variance = compute_variance_cents(expected, body.counted_cash_cents)

    session.status = "CLOSED"
    session.closed_by_user_id = user.user_id
    session.approved_by_user_id = user.user_id
    session.closed_at = datetime.now(timezone.utc)
    session.expected_cash_cents = expected
    session.counted_cash_cents = body.counted_cash_cents
    session.variance_cents = variance

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=user.business_id,
        actor_user_id=user.user_id,
        action="pos.till.closed",
        target_type="till_session",
        target_id=session.id,
        diff_json={
            "expected_cash_cents": expected,
            "counted_cash_cents": body.counted_cash_cents,
            "variance_cents": variance,
        },
    ))

    await db.commit()
    await db.refresh(session)
    return TillSessionResponse.model_validate(session)
