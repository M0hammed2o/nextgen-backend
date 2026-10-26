"""
Order routes — list (live + history), detail, status update.
Staff, Manager, and Owner can access. Status transitions are validated.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, InvalidTransitionError, NotFoundError
from backend.app.core.ownership import require_owned
from backend.app.core.pagination import CursorParams, PaginatedResponse, PaginationMeta, encode_cursor
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.pos.pricing import POSAddOnSelection, POSItemInput, resolve_pos_items
from backend.app.receipts.whatsapp_receipt import (
    ReceiptDestinationUnavailable,
    resolve_receipt_customer_and_phone,
    send_digital_receipt,
)
from backend.app.utils.phone import normalize_sa_mobile
from shared.enums import (
    EXTERNAL_ORDER_SOURCES,
    ORDER_STATUS_TRANSITIONS,
    AuditScope,
    OrderSource,
    OrderStatus,
)
from backend.app.payments.registry import get_provider
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.order import Order, OrderEvent, OrderItem
from shared.pricing.engine import apply_vat_to_total
from shared.utils import format_order_number

settings = get_settings()

router = APIRouter(prefix="/business/orders", tags=["orders"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class OrderItemResponse(BaseModel):
    id: uuid.UUID
    menu_item_id: uuid.UUID | None = None
    name_snapshot: str
    unit_price_cents: int
    quantity: int
    line_total_cents: int
    options_snapshot: dict | None
    add_ons_snapshot: list | None = None
    selected_options_snapshot: list | None = None
    removed_ingredients_snapshot: list | None = None
    special_instructions: str | None
    # BUG-0016 fix — True for a manually-typed name+price_cents line with no
    # catalog backing (unverified, caller-asserted price); False for every
    # other line, which was resolved and priced server-side from the live
    # menu catalog (POS orders, and dashboard orders that set menu_item_id).
    is_ad_hoc_price: bool = False
    model_config = {"from_attributes": True}


class PaymentAllocationResponse(BaseModel):
    """
    One settled payment allocation (migration 0055) — only meaningfully
    populated for payment_method == 'SPLIT'; never exposes internal UUIDs
    (payment_link_intent_id) or a raw payment link URL.
    """
    payment_method: str
    amount_cents: int
    cash_tendered_cents: int | None = None
    change_due_cents: int | None = None
    payment_reference: str | None = None
    provider: str | None = None
    paid_at: datetime | None = None
    # Financial-safety closeout (migration 0056). PAY_BY_LINK only — NULL
    # unless this allocation's order has been refunded/voided. See
    # shared/models/pos_payment.py's own comment for the full reasoning:
    # NextGen has no verified-safe way to call a real provider refund API
    # from the till-native refund flow, so this is never set to CONFIRMED
    # automatically — only by a manager's explicit confirmation.
    provider_refund_status: str | None = None
    provider_refund_confirmed_at: datetime | None = None
    model_config = {"from_attributes": True}


class OrderResponse(BaseModel):
    id: uuid.UUID
    order_number: str
    status: str
    payment_status: str
    payment_method: str | None = None
    payment_reference: str | None = None
    payment_link_url: str | None = None
    payment_required: bool = False
    paid_at: datetime | None = None
    order_mode: str
    table_number: str | None = None
    source: str
    cash_tendered_cents: int | None = None
    change_due_cents: int | None = None
    till_session_id: uuid.UUID | None = None
    created_by_user_id: uuid.UUID | None = None
    subtotal_cents: int
    delivery_fee_cents: int
    discount_cents: int = 0
    tax_cents: int = 0
    discount_reason: str | None = None
    # Loyalty redemption snapshot (migration 0050). Declared nullable here
    # even though the DB columns are NOT NULL DEFAULT 0/nullable-with-no-
    # default — a test or other caller may hand this schema an Order
    # instance that was constructed directly in Python and never flushed
    # (SQLAlchemy only applies a column's Python-side default at INSERT
    # time), which reads back as None rather than 0 for an unset int column.
    # In production, `db.refresh(order)` always runs first, so these are
    # real values by the time a client sees them.
    loyalty_discount_cents: int | None = 0
    loyalty_reward_id: uuid.UUID | None = None
    loyalty_points_redeemed: int | None = 0
    loyalty_reward_name: str | None = None
    total_cents: int
    currency: str
    refund_reference: str | None = None
    refund_amount_cents: int | None = None
    refunded_at: datetime | None = None
    customer_name: str | None
    phone_number: str | None
    delivery_address: str | None
    estimated_ready_at: datetime | None
    confirmed_at: datetime | None
    accepted_at: datetime | None
    ready_at: datetime | None
    completed_at: datetime | None
    special_instructions: str | None = None
    cancelled_reason: str | None
    items: list[OrderItemResponse] = []
    payment_allocations: list[PaymentAllocationResponse] = []
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

class OrderItemAddOnSelection(BaseModel):
    add_on_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=99)


class OrderItemCreate(BaseModel):
    """
    Single line item in a manually created (dashboard) order.

    Two mutually exclusive ways to price a line (BUG-0016 fix):

      * Catalog (preferred) — set `menu_item_id` (+ optionally
        `selected_option_ids` / `add_on_selections` / `removed_ingredient_ids`).
        The item, its options and add-ons are resolved and priced
        server-side from this business's live menu catalog, via the exact
        same `backend/app/pos/pricing.py` path POS checkout uses — any
        `price_cents`/`name` the client also sends is ignored. A
        `menu_item_id` belonging to another business, or a disabled/deleted
        item, is rejected (404/422) — see `resolve_pos_items`.

      * Ad hoc (fallback, unchanged legacy behaviour) — omit `menu_item_id`
        and set `name` + `price_cents` for a genuinely one-off line with no
        catalog equivalent (e.g. a hand-typed "delivery surcharge"). This
        price is caller-asserted, not catalog-verified — the resulting
        OrderItem is flagged `is_ad_hoc_price=True` in the response so it
        stays visibly distinct from a catalog-priced line.

    `menu_item_id` is additive/optional specifically so the current
    manager-dashboard "Create Order" form (which only ever sends
    name+price_cents today — no menu picker) keeps working unchanged.
    """
    menu_item_id: uuid.UUID | None = None
    selected_option_ids: dict[str, str] = Field(default_factory=dict)
    add_on_selections: list[OrderItemAddOnSelection] = Field(default_factory=list)
    removed_ingredient_ids: list[str] = Field(default_factory=list)

    name: str | None = Field(default=None, max_length=255)
    price_cents: int | None = Field(
        default=None, ge=0,
        description="Unit price in cents — ad hoc lines only (no menu_item_id); "
                     "ignored when menu_item_id is set",
    )

    qty: int = Field(ge=1, le=999)
    options: dict | None = None
    special_instructions: str | None = None

    @model_validator(mode="after")
    def _validate_pricing_shape(self) -> "OrderItemCreate":
        if self.menu_item_id is None and (not self.name or self.price_cents is None):
            raise ValueError(
                "Each item needs either menu_item_id (catalog-priced) or "
                "both name and price_cents (ad hoc)"
            )
        return self


# Sources this dashboard endpoint may be asked to record an order under.
# Deliberately excludes OrderSource.POS — POS orders are always priced and
# created via POST /business/pos/orders, never claimed here.
_MANUAL_ORDER_SOURCES = (OrderSource.MANUAL, OrderSource.ADMIN, OrderSource.WHATSAPP)


class OrderCreate(BaseModel):
    """Create an order from the dashboard (manual order)."""
    customer_name: str | None = Field(default=None, max_length=255)
    customer_phone: str | None = Field(default=None, max_length=32)
    delivery_address: str | None = None
    items: list[OrderItemCreate] = Field(min_length=1)
    notes: str | None = None
    order_mode: str = Field(default="PICKUP", pattern=r"^(PICKUP|DELIVERY|DINE_IN)$")
    source: str = Field(
        default=OrderSource.MANUAL.value,
        pattern=r"^(" + "|".join(s.value for s in _MANUAL_ORDER_SOURCES) + r")$",
    )


# ── Routes ───────────────────────────────────────────────────────────────────

LIVE_STATUSES = [
    OrderStatus.PENDING_DELIVERY_FEE.value,
    OrderStatus.FEE_SENT.value,
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

    BUG-0016 fix: catalog-priced items (menu_item_id set) are resolved and
    priced server-side against the live menu, via the same
    `backend/app/pos/pricing.py::resolve_pos_items` POS checkout uses — the
    client's price_cents is never trusted for those lines. Ad hoc lines
    (no menu_item_id) keep the legacy caller-priced behaviour but are
    flagged `is_ad_hoc_price=True`. The order total is run through the same
    VAT engine as POS (`shared/pricing/engine.py::apply_vat_to_total`) so a
    VAT-registered business's manually-created orders carry real tax_cents
    instead of always 0.
    """
    from sqlalchemy import update as sa_update

    # ── Resolve every catalog-priced item against the live menu BEFORE any
    #    mutation — an invalid menu_item_id/option/add-on must never consume
    #    an order number or leave a partial order behind (same rule as POS
    #    checkout). Ad hoc items (no menu_item_id) skip this entirely, so an
    #    all-ad-hoc order (today's only real caller) triggers no extra query.
    catalog_entries = [
        (idx, item) for idx, item in enumerate(body.items) if item.menu_item_id is not None
    ]
    resolved_by_index: dict[int, object] = {}
    if catalog_entries:
        item_inputs = [
            POSItemInput(
                menu_item_id=item.menu_item_id,
                quantity=item.qty,
                selected_option_ids=item.selected_option_ids,
                add_on_selections=[
                    POSAddOnSelection(add_on_id=sel.add_on_id, quantity=sel.quantity)
                    for sel in item.add_on_selections
                ],
                removed_ingredient_ids=item.removed_ingredient_ids,
                special_instructions=item.special_instructions,
            )
            for _, item in catalog_entries
        ]
        resolved_items = await resolve_pos_items(db, user.business_id, item_inputs)
        resolved_by_index = {
            idx: resolved for (idx, _), resolved in zip(catalog_entries, resolved_items)
        }

    # Atomic increment of business order_number_sequence — also pulls the
    # VAT configuration needed below in the same round trip.
    result = await db.execute(
        sa_update(Business)
        .where(Business.id == user.business_id)
        .values(order_number_sequence=Business.order_number_sequence + 1)
        .returning(
            Business.order_number_sequence,
            Business.currency,
            Business.delivery_fee_cents,
            Business.vat_registered,
            Business.vat_rate_basis_points,
            Business.vat_inclusive_pricing,
        )
    )
    row = result.one_or_none()
    if not row:
        raise NotFoundError("Business")

    (
        seq, currency, biz_delivery_fee,
        vat_registered, vat_rate_basis_points, vat_inclusive_pricing,
    ) = row

    # ── Build order item rows (preserving submitted order) and totals ─────
    subtotal_cents = 0
    item_rows: list[dict] = []
    for idx, item in enumerate(body.items):
        resolved = resolved_by_index.get(idx)
        if resolved is not None:
            subtotal_cents += resolved.breakdown.line_total_cents
            item_rows.append(dict(
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
                removed_ingredients_snapshot=resolved.removed_ingredients_snapshot or None,
                special_instructions=resolved.special_instructions or body.notes,
                is_ad_hoc_price=False,
            ))
        else:
            line_total = item.qty * item.price_cents
            subtotal_cents += line_total
            item_rows.append(dict(
                menu_item_id=None,
                name_snapshot=item.name,
                base_price_cents=item.price_cents,
                option_adjustment_cents=0,
                add_on_total_cents=0,
                unit_price_cents=item.price_cents,
                quantity=item.qty,
                line_total_cents=line_total,
                options_snapshot=item.options,
                special_instructions=item.special_instructions or body.notes,
                is_ad_hoc_price=True,
            ))

    delivery_fee = biz_delivery_fee if body.order_mode == "DELIVERY" else 0
    net_total_cents = max(0, subtotal_cents + delivery_fee)

    # ── VAT — same engine and same inclusive/exclusive semantics as POS
    #    checkout (shared/pricing/engine.py::apply_vat_to_total). Previously
    #    this endpoint hardcoded tax_cents=0 regardless of VAT registration.
    if vat_registered:
        total_cents, tax_cents = apply_vat_to_total(
            net_total_cents, vat_rate_basis_points, vat_inclusive_pricing,
        )
    else:
        total_cents, tax_cents = net_total_cents, 0

    order = Order(
        business_id=user.business_id,
        order_number=format_order_number(seq),
        status=OrderStatus.NEW.value,
        order_mode=body.order_mode,
        source=body.source,
        subtotal_cents=subtotal_cents,
        delivery_fee_cents=delivery_fee,
        tax_cents=tax_cents,
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
    for row_kwargs in item_rows:
        db.add(OrderItem(
            order_id=order.id,
            business_id=user.business_id,
            **row_kwargs,
        ))

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

    # Record usage (fire and forget) — feeds the monthly order-quota/pack
    # tracking (backend/app/billing/usage.py). Every order-creation path
    # (WhatsApp, manual, POS) must call this — previously only the bot did.
    # `increment_usage` only flushes, it never commits — this call used to
    # run after the handler's only commit, so the increment was flushed
    # into a session that then closed without ever committing it, silently
    # discarding it (BUG-0050). Committing here, inside this same
    # try/except, means a usage-tracking failure still can never fail the
    # order itself (the order already committed above).
    try:
        from backend.app.bot.usage_tracker import increment_usage
        biz_tz_row = await db.execute(
            select(Business.timezone).where(Business.id == user.business_id)
        )
        biz_timezone = biz_tz_row.scalar_one_or_none() or "Africa/Johannesburg"
        await increment_usage(
            db, user.business_id, biz_timezone,
            orders_created=1, revenue_cents=order.total_cents,
        )
        await db.commit()
    except Exception:
        import logging
        logging.getLogger("nextgen").warning("Failed to record usage for manual order %s", order.id)

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
        # Support comma-separated values e.g. ?status=COLLECTED,DELIVERED
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            query = query.where(Order.status == statuses[0])
        else:
            query = query.where(Order.status.in_(statuses))

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

    # ── Payment gate: block IN_PROGRESS if online payment required ───────
    if requested == OrderStatus.IN_PROGRESS and order.payment_required:
        if order.payment_status != "PAID":
            raise InvalidTransitionError(
                current.value, requested.value,
                detail="Payment must be confirmed before the order can move to IN_PROGRESS."
            )

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

        # Reverse any stock consumption posted at sale time — but only if the
        # order never reached the kitchen (see backend/app/inventory/cancellation.py
        # for the full rule and the exact pre-preparation status set).
        from backend.app.inventory.cancellation import reverse_consumption_on_cancel
        inv_row = await db.execute(
            select(Business.inventory_enabled).where(Business.id == user.business_id)
        )
        inventory_enabled = bool(inv_row.scalar_one_or_none())
        await reverse_consumption_on_cancel(
            db,
            business_id=user.business_id,
            inventory_enabled=inventory_enabled,
            order_id=order.id,
            previous_status=old_status,
            created_by_user_id=user.user_id,
        )

    # ── When accepted with online payment required → set PENDING ────────
    if requested == OrderStatus.ACCEPTED and order.payment_required:
        order.payment_status = "PENDING"

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

    # ── Payment link creation (ACCEPTED + payment_required + provider set) ─
    if requested == OrderStatus.ACCEPTED and order.payment_required:
        try:
            from backend.app.payments.registry import get_provider
            biz_result = await db.execute(select(Business).where(Business.id == order.business_id))
            biz_for_payment = biz_result.scalar_one_or_none()
            if biz_for_payment and biz_for_payment.payment_provider:
                provider = get_provider(biz_for_payment.payment_provider)
                if provider:
                    link_url = await provider.create_payment_link(order, biz_for_payment)
                    if link_url:
                        order.payment_link_url = link_url
                        await db.commit()
                        await db.refresh(order)
        except Exception:
            import logging as _log
            _log.getLogger("nextgen").warning(
                "Payment link creation failed for order %s — continuing without link",
                order.order_number,
            )

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

    # ── Notify customer via WhatsApp (fire and forget) ────────────────────
    try:
        if order.customer_id:
            from shared.models.customer import Customer
            from backend.app.bot.whatsapp_sender import send_notification_message
            from backend.app.payments.messages import build_payment_message

            cust_row = await db.execute(
                select(Customer.wa_id).where(Customer.id == order.customer_id)
            )
            wa_id = cust_row.scalar_one_or_none()

            biz_row = await db.execute(
                select(Business).where(Business.id == order.business_id)
            )
            business_obj = biz_row.scalar_one_or_none()
            phone_number_id = business_obj.whatsapp_phone_number_id if business_obj else None

            if wa_id and phone_number_id:
                # For online-payment-required orders accepted: send payment instructions
                # instead of (or in addition to) the generic accepted message.
                if requested == OrderStatus.ACCEPTED and order.payment_required and business_obj:
                    payment_msg = build_payment_message(order, business_obj)
                    if payment_msg:
                        await send_notification_message(
                            phone_number_id=phone_number_id,
                            recipient_wa_id=wa_id,
                            text=payment_msg,
                            db=db, business=business_obj,
                            order_id=order.id, business_id=order.business_id,
                            order_status=order.status,
                        )
                else:
                    msg = _customer_status_message(
                        status=requested.value,
                        order_number=order.order_number,
                        estimated_ready_minutes=body.estimated_ready_minutes,
                        reason=body.reason,
                    )
                    if msg:
                        await send_notification_message(
                            phone_number_id=phone_number_id,
                            recipient_wa_id=wa_id,
                            text=msg,
                            db=db, business=business_obj,
                            order_id=order.id, business_id=order.business_id,
                            order_status=order.status,
                        )
    except Exception:
        import logging
        logging.getLogger("nextgen").warning(
            "Failed to send customer WhatsApp notification for order %s", order.id
        )

    # ── Digital receipt for a cash-on-collection WhatsApp order (fire and
    #    forget) ────────────────────────────────────────────────────────
    #
    # A CASH_ON_COLLECTION order's payment_status never becomes PAID (see
    # backend/app/api/v1/routes_payments.py — that path is exclusively for
    # online-payment-provider orders); the genuine "this sale is now final"
    # moment for a cash-on-collection order IS this exact status transition
    # into COLLECTED/DELIVERED. Triggered only on the specific transition
    # INTO one of those two states (never on a repeat/duplicate update) —
    # `old_status not in (...)` here is defence in depth: ORDER_STATUS_
    # TRANSITIONS above already makes COLLECTED/DELIVERED terminal (no
    # outbound transitions at all), so a genuine duplicate call would have
    # already raised InvalidTransitionError before this line is ever
    # reached; this check guards against that invariant changing later
    # without this trigger being re-audited.
    #
    # Idempotency beyond that: send_digital_receipt() itself is idempotent
    # per order_id (get-or-create + already-SENT short-circuit), so even a
    # hypothetical second call here could never double-send.
    if (
        requested in (OrderStatus.COLLECTED, OrderStatus.DELIVERED)
        and old_status not in (OrderStatus.COLLECTED.value, OrderStatus.DELIVERED.value)
        and order.payment_status == "CASH_ON_COLLECTION"
    ):
        try:
            from shared.models.customer import Customer
            from backend.app.receipts.whatsapp_receipt import send_digital_receipt

            biz_row = await db.execute(select(Business).where(Business.id == order.business_id))
            business_obj = biz_row.scalar_one_or_none()

            customer_obj = None
            if order.customer_id:
                cust_row = await db.execute(
                    select(Customer).where(
                        Customer.id == order.customer_id, Customer.business_id == order.business_id,
                    )
                )
                customer_obj = cust_row.scalar_one_or_none()

            if business_obj is not None:
                # No phone re-entry needed — this is a WhatsApp-originated
                # order, so the identity is already known via the linked
                # Customer's wa_id (send_digital_receipt resolves it).
                await send_digital_receipt(
                    db, order, business_obj, channels={"WHATSAPP"}, customer=customer_obj,
                )
        except Exception:
            import logging
            logging.getLogger("nextgen").warning(
                "Failed to send digital receipt for cash-on-collection order %s", order.id
            )

    return OrderResponse.model_validate(order)


def _customer_status_message(
    status: str,
    order_number: str,
    estimated_ready_minutes: int | None,
    reason: str | None,
) -> str | None:
    """Return a customer-facing WhatsApp notification text, or None to skip."""
    if status == "ACCEPTED":
        msg = f"✅ Your order *{order_number}* has been accepted!"
        if estimated_ready_minutes:
            msg += f" Estimated ready time: *{estimated_ready_minutes} minutes*."
        return msg
    if status == "IN_PROGRESS":
        return f"👨‍🍳 Your order *{order_number}* is being prepared."
    if status == "READY":
        return f"🎉 Your order *{order_number}* is ready for collection!"
    if status == "DELIVERED":
        return f"🛵 Your order *{order_number}* has been delivered. Enjoy! 🙏"
    if status == "COLLECTED":
        return f"Thank you! Your order *{order_number}* has been collected. Enjoy! 🙏"
    if status == "CANCELLED":
        msg = f"❌ Sorry, your order *{order_number}* has been cancelled."
        if reason:
            msg += f"\nReason: {reason}"
        return msg
    return None


class DeliveryFeeRequest(BaseModel):
    delivery_fee_cents: int = Field(ge=0, description="Delivery fee in cents")


@router.post("/{order_id}/delivery-fee", response_model=OrderResponse)
async def set_delivery_fee(
    order_id: uuid.UUID,
    body: DeliveryFeeRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Staff sets the delivery fee on a PENDING_DELIVERY_FEE order.
    Stores the fee in the order AND sends a WhatsApp message to the customer
    asking them to approve it. Order status moves to FEE_SENT.
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

    if order.status != OrderStatus.PENDING_DELIVERY_FEE.value:
        from backend.app.core.errors import InvalidTransitionError
        raise InvalidTransitionError(order.status, "FEE_SENT")

    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    business = biz_result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    fee = body.delivery_fee_cents
    order.delivery_fee_cents = fee

    # Recompute total through the same VAT/discount path used at order
    # creation (shared/pricing/engine.py::apply_vat_to_total) instead of
    # `subtotal + fee`, which silently dropped discount_cents and never
    # applied VAT at all — for an exclusive-VAT, VAT-registered business
    # that meant total_cents came out short of what the customer actually
    # owes once a delivery fee is added.
    #
    # Also nets out loyalty_discount_cents (migration 0050) — this endpoint
    # is reached only via the WhatsApp PENDING_DELIVERY_FEE flow, which a POS
    # loyalty redemption never goes through today, but the formula must stay
    # consistent with document.py::_vat_rate_display's reconstruction of the
    # same net total, or a future order that somehow carries both would
    # silently drop its VAT rate label on reprint.
    net_total_cents = max(
        0, order.subtotal_cents - order.discount_cents - (order.loyalty_discount_cents or 0) + fee
    )
    if business.vat_registered:
        total_cents, tax_cents = apply_vat_to_total(
            net_total_cents, business.vat_rate_basis_points, business.vat_inclusive_pricing,
        )
    else:
        total_cents, tax_cents = net_total_cents, 0
    order.total_cents = total_cents
    order.tax_cents = tax_cents
    order.status = "FEE_SENT"

    event = OrderEvent(
        order_id=order.id,
        business_id=user.business_id,
        old_status=OrderStatus.PENDING_DELIVERY_FEE.value,
        new_status="FEE_SENT",
        changed_by_user_id=user.user_id,
        reason=f"Delivery fee set: {fee} cents",
    )
    db.add(event)
    await db.flush()

    # Send WhatsApp message to customer asking them to approve the fee
    try:
        if order.customer_id:
            from shared.models.customer import Customer
            from backend.app.bot.whatsapp_sender import send_notification_message
            from shared.utils.money import format_currency

            cust_row = await db.execute(
                select(Customer.wa_id).where(Customer.id == order.customer_id)
            )
            wa_id = cust_row.scalar_one_or_none()

            if wa_id and business.whatsapp_phone_number_id:
                fee_str = format_currency(fee, business.currency)
                msg = (
                    f"🚗 *Delivery Fee for Order {order.order_number}*\n\n"
                    f"Your delivery fee is *{fee_str}*.\n\n"
                    f"Reply *yes* to confirm and place your order, or *no* to cancel."
                )
                await send_notification_message(
                    phone_number_id=business.whatsapp_phone_number_id,
                    recipient_wa_id=wa_id,
                    text=msg,
                    db=db, business=business,
                    order_id=order.id, business_id=order.business_id,
                    order_status=order.status,
                )

            # Store fee in customer's conversation session so pipeline can finalise
            from shared.models.customer import ConversationSession
            from sqlalchemy.orm.attributes import flag_modified
            sess_row = await db.execute(
                select(ConversationSession).where(
                    ConversationSession.business_id == order.business_id,
                    ConversationSession.customer_id == order.customer_id,
                )
            )
            conv = sess_row.scalar_one_or_none()
            if conv:
                ctx = dict(conv.context_json or {})
                ctx["delivery_fee_cents"] = fee
                ctx["delivery_fee_status"] = "FEE_SENT"
                ctx["pending_order_id"] = str(order.id)
                conv.context_json = ctx
                flag_modified(conv, "context_json")
                from shared.enums import ConversationState
                conv.state = ConversationState.WAITING_DELIVERY_FEE_APPROVAL.value
    except Exception:
        import logging as _log
        _log.getLogger("nextgen").warning(
            "Failed to send delivery fee WhatsApp message for order %s", order.id
        )

    await db.commit()
    await db.refresh(order)

    # Publish SSE so dashboard refreshes
    try:
        from backend.app.db.session import get_redis
        import json
        redis = await get_redis()
        await redis.publish(
            f"orders:{user.business_id}",
            json.dumps({
                "type": "order_status_changed",
                "order_id": str(order.id),
                "order_number": order.order_number,
                "old_status": OrderStatus.PENDING_DELIVERY_FEE.value,
                "new_status": "FEE_SENT",
                "total_cents": order.total_cents,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }),
        )
    except Exception:
        pass

    return OrderResponse.model_validate(order)


class PaymentUpdateRequest(BaseModel):
    # REFUNDED/PARTIALLY_REFUNDED/VOIDED are accepted here only so the
    # handler can reject them with a clear, actionable message pointing at
    # the refund/void endpoints — see update_payment_status.
    payment_status: str = Field(
        pattern=r"^(UNPAID|PENDING|PAID|FAILED|REFUNDED|PARTIALLY_REFUNDED|VOIDED|CASH_ON_COLLECTION)$"
    )
    payment_method: str | None = Field(
        default=None,
        # MANUAL: the staff app's "mark paid" button on an order awaiting
        # ONLINE payment (payment_required=True, e.g. YOCO/PAYFAST/STITCH) —
        # staff confirming, outside the online flow, that payment was
        # received some other way (cash on collection, EFT proof shown in
        # person, etc.). Distinct from the specific provider values, which
        # record how an online payment actually cleared.
        pattern=r"^(CASH|CARD|PAY_ON_COLLECTION|DIRECT_EFT|PAYSHAP|YOCO|PAYFAST|STITCH|MANUAL)$",
        description="Payment method used",
    )
    payment_reference: str | None = Field(
        default=None, max_length=255,
        description="Transaction reference, EFT pop number, etc.",
    )


@router.patch("/{order_id}/payment", response_model=OrderResponse)
async def update_payment_status(
    order_id: uuid.UUID,
    body: PaymentUpdateRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Record HOW and WHEN payment was taken on an order — not a general-purpose
    payment_status setter.

    Staff/Manager/Owner may record UNPAID/PENDING -> PAID (the "customer paid
    at collection" flow the staff app actually uses). REFUNDED/
    PARTIALLY_REFUNDED/VOIDED are rejected here — those carry their own
    money-movement bookkeeping (refund amount, stock-reversal decision) that
    only `POST /{order_id}/refund` and the POS refund/void endpoints collect.
    Moving an already-PAID order back to any non-PAID status (e.g. a
    correction) requires an owner/manager and is audited — a cashier
    shouldn't be able to reverse a completed sale outside that audited path.
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

    if order.source in EXTERNAL_ORDER_SOURCES:
        raise AppError(
            "EXTERNAL_ORDER_PAYMENT_LOCKED",
            "This order's payment was collected by the delivery platform, not this "
            "restaurant — its payment status/method can't be edited here. Contact "
            "the platform (Uber Eats/Mr D) for payment disputes on marketplace orders.",
            422,
        )

    new_status = body.payment_status
    old_payment_status = order.payment_status

    if new_status in ("REFUNDED", "PARTIALLY_REFUNDED", "VOIDED"):
        raise AppError(
            "USE_REFUND_ENDPOINT",
            "Use POST /orders/{id}/refund (or the POS refund/void endpoints) "
            "to record a refund or void — this endpoint only records how and "
            "when payment was taken.",
            422,
        )

    if old_payment_status == "PAID" and new_status != "PAID" and user.role not in ("OWNER", "MANAGER"):
        raise AppError(
            "OWNER_OR_MANAGER_REQUIRED",
            "Moving a PAID order back to an unpaid status requires an owner or manager.",
            403,
        )

    was_unpaid = old_payment_status in ("UNPAID", "PENDING")
    payment_required = order.payment_required

    order.payment_status = new_status
    if body.payment_method is not None:
        order.payment_method = body.payment_method
    if body.payment_reference is not None:
        order.payment_reference = body.payment_reference
    if new_status == "PAID" and order.paid_at is None:
        order.paid_at = datetime.now(timezone.utc)

    # A MANUAL confirmation overrides the online-payment flow the order was
    # actually placed under (payment_required=True) — worth its own event
    # so staff/owner can see, on the order timeline, that this PAID status
    # came from a staff override rather than the payment provider clearing.
    if body.payment_method == "MANUAL":
        db.add(OrderEvent(
            order_id=order.id,
            business_id=user.business_id,
            old_status=order.status,
            new_status=order.status,
            changed_by_user_id=user.user_id,
            reason=(
                f"Payment marked {new_status} manually by staff "
                f"(payment_reference={body.payment_reference!r})"
                if body.payment_reference else
                f"Payment marked {new_status} manually by staff"
            ),
        ))

    # A PAID -> non-PAID correction is rare and money-affecting enough to
    # leave its own audit trail, same as a refund would.
    if old_payment_status == "PAID" and new_status != "PAID":
        db.add(AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="order.payment.reverted_from_paid",
            target_type="order",
            target_id=order.id,
            diff_json={
                "old_payment_status": old_payment_status,
                "new_payment_status": new_status,
                "actor_role": user.role,
            },
        ))

    await db.commit()
    await db.refresh(order)

    # ── Award loyalty points if this business has loyalty enabled ────────
    if body.payment_status == "PAID" and was_unpaid:
        from backend.app.loyalty.engine import award_loyalty_points_if_enabled
        await award_loyalty_points_if_enabled(db, user.business_id, order)

    # ── Send WhatsApp payment confirmation if online payment was required ─
    if body.payment_status == "PAID" and was_unpaid and payment_required:
        try:
            from shared.models.customer import Customer
            from backend.app.bot.whatsapp_sender import send_notification_message
            from backend.app.payments.messages import build_payment_confirmed_message

            cust_row = await db.execute(
                select(Customer.wa_id).where(Customer.id == order.customer_id)
            )
            wa_id = cust_row.scalar_one_or_none()

            biz_row = await db.execute(
                select(Business).where(Business.id == order.business_id)
            )
            business_obj = biz_row.scalar_one_or_none()
            phone_number_id = business_obj.whatsapp_phone_number_id if business_obj else None

            if wa_id and phone_number_id:
                await send_notification_message(
                    phone_number_id=phone_number_id,
                    recipient_wa_id=wa_id,
                    text=build_payment_confirmed_message(order),
                    db=db, business=business_obj,
                    order_id=order.id, business_id=order.business_id,
                    order_status=order.status,
                )
        except Exception:
            import logging
            logging.getLogger("nextgen").warning(
                "Failed to send payment confirmation WhatsApp for order %s", order.id
            )

    return OrderResponse.model_validate(order)


# ── Refund (Payment Provider Framework) ──────────────────────────────────────

class RefundRequest(BaseModel):
    amount_cents: int | None = Field(default=None, ge=1, description="Defaults to a full refund of total_cents")
    reason: str | None = Field(default=None, max_length=255)

    # ── Manual refund confirmation (F3 fix, Wave 0A verifier) ─────────────
    # For providers whose refund is MANUAL_REQUIRED (Direct EFT, Stitch),
    # this endpoint's provider path above never sets REFUNDED — there is no
    # provider call to confirm it. OWNER/MANAGER set confirm_manual=true
    # (with a reference for the manual transfer they already completed
    # outside NextGen) to record that fact directly. Never calls a payment
    # provider. Mutually the ONLY way left to record such a refund — the
    # PATCH .../payment endpoint intentionally rejects REFUNDED/
    # PARTIALLY_REFUNDED (see update_payment_status's USE_REFUND_ENDPOINT).
    confirm_manual: bool = Field(
        default=False,
        description="Set true to record a refund already completed manually "
                     "outside NextGen (e.g. a MANUAL_REQUIRED provider like "
                     "Direct EFT/Stitch) — requires `reference`. Never calls "
                     "the payment provider.",
    )
    reference: str | None = Field(
        default=None, max_length=255,
        description="Required when confirm_manual=true — proof/reference for "
                     "the manual transfer (EFT proof-of-payment number, etc).",
    )
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _confirm_manual_requires_reference(self) -> "RefundRequest":
        if self.confirm_manual and not self.reference:
            raise ValueError("reference is required when confirm_manual is true")
        return self


class RefundResponse(BaseModel):
    success: bool
    status: str
    provider_reference: str | None = None
    error: str | None = None
    order: OrderResponse


@router.post("/{order_id}/refund", response_model=RefundResponse)
async def refund_order_payment(
    order_id: uuid.UUID,
    body: RefundRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Refund a paid order through its business's configured payment provider
    (the Payment Provider Framework, `backend/app/payments/base.py`) — the
    route never contains provider-specific logic, it only asks the provider
    to "process this refund."

    OWNER/MANAGER only, same as the discount-limit gate on POS orders —
    refunds are a financial action a cashier shouldn't authorize solo.

    A MANUAL_REQUIRED or FAILED result does NOT change payment_status —
    only a provider-confirmed COMPLETED refund does. For MANUAL_REQUIRED
    providers (Direct EFT, Stitch), where there is no provider call left to
    confirm, staff record the refund by calling this same endpoint again
    with `confirm_manual=true` and a `reference` — see RefundRequest.
    (`PATCH /{order_id}/payment` intentionally rejects REFUNDED/
    PARTIALLY_REFUNDED and always has — it only records HOW and WHEN a
    payment was taken, not a refund.)
    """
    # Locked FOR UPDATE before any provider call — two concurrent refund
    # requests on the same order must not both read payment_status=PAID and
    # both ask the provider to refund it (which would send money back
    # twice). The second call blocks until the first commits, then sees the
    # already-REFUNDED status and is correctly rejected below.
    result = await db.execute(
        select(Order)
        .where(Order.id == order_id, Order.business_id == user.business_id)
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError("Order", str(order_id))

    if order.source in EXTERNAL_ORDER_SOURCES:
        raise AppError(
            "EXTERNAL_ORDER_PAYMENT_LOCKED",
            "This order's payment was collected by the delivery platform, not this "
            "restaurant's own payment provider — it can't be refunded here. Refunds "
            "on marketplace orders go through the platform (Uber Eats/Mr D) directly.",
            422,
        )

    if body.confirm_manual:
        if order.payment_status in ("REFUNDED", "PARTIALLY_REFUNDED"):
            raise AppError(
                "REFUND_ALREADY_RECORDED",
                f"This order's payment is already {order.payment_status} — "
                "a manual refund confirmation cannot be recorded twice.",
                409,
            )
        if order.payment_status != "PAID":
            raise AppError(
                "ORDER_NOT_PAID",
                f"Cannot refund an order with payment_status={order.payment_status!r} — only PAID orders can be refunded",
                422,
            )

        amount_cents = body.amount_cents or order.total_cents
        if amount_cents > order.total_cents:
            raise AppError("REFUND_EXCEEDS_TOTAL", "Refund amount cannot exceed the order total", 422)

        order.payment_status = "REFUNDED" if amount_cents == order.total_cents else "PARTIALLY_REFUNDED"
        order.refund_amount_cents = amount_cents
        order.refund_reference = body.reference
        order.refunded_at = datetime.now(timezone.utc)

        db.add(OrderEvent(
            order_id=order.id,
            business_id=user.business_id,
            old_status=order.status,
            new_status=order.status,
            changed_by_user_id=user.user_id,
            reason=(
                f"Manual refund confirmed by staff — R{amount_cents / 100:.2f} "
                f"(reference={body.reference!r})"
                + (f" — {body.note}" if body.note else "")
            ),
        ))
        db.add(AuditEvent(
            scope=AuditScope.BUSINESS.value,
            business_id=user.business_id,
            actor_user_id=user.user_id,
            action="order.payment.manual_refund_confirmed",
            target_type="order",
            target_id=order.id,
            diff_json={
                "amount_cents": amount_cents,
                "reference": body.reference,
                "note": body.note,
                "resulting_status": order.payment_status,
            },
        ))

        await db.commit()
        await db.refresh(order)

        return RefundResponse(
            success=True,
            status=order.payment_status,
            provider_reference=body.reference,
            error=None,
            order=OrderResponse.model_validate(order),
        )

    if order.payment_status != "PAID":
        raise AppError(
            "ORDER_NOT_PAID",
            f"Cannot refund an order with payment_status={order.payment_status!r} — only PAID orders can be refunded",
            422,
        )

    amount_cents = body.amount_cents or order.total_cents
    if amount_cents > order.total_cents:
        raise AppError("REFUND_EXCEEDS_TOTAL", "Refund amount cannot exceed the order total", 422)

    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    business = biz_result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    provider = get_provider(business.payment_provider)
    if not provider:
        raise AppError(
            "NO_PAYMENT_PROVIDER",
            "This business has no payment provider configured — cannot process a refund",
            422,
        )

    refund_result = await provider.refund_payment(order, business, amount_cents)

    if refund_result.success and refund_result.status == "COMPLETED":
        order.payment_status = "REFUNDED"
        order.refund_reference = refund_result.provider_reference
        order.refund_amount_cents = amount_cents
        order.refunded_at = datetime.now(timezone.utc)

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=user.business_id,
        actor_user_id=user.user_id,
        action="order.payment.refund_attempted",
        target_type="order",
        target_id=order.id,
        diff_json={
            "amount_cents": amount_cents,
            "reason": body.reason,
            "provider": business.payment_provider,
            "result_status": refund_result.status,
            "success": refund_result.success,
        },
    ))

    await db.commit()
    await db.refresh(order)

    return RefundResponse(
        success=refund_result.success,
        status=refund_result.status,
        provider_reference=refund_result.provider_reference,
        error=refund_result.error,
        order=OrderResponse.model_validate(order),
    )


# ── Digital receipt resend (manager dashboard) ──────────────────────────────

class ReceiptResendRequest(BaseModel):
    phone_number: str | None = Field(default=None, max_length=32)


class ReceiptResendResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    receipt_url: str
    whatsapp_status: str
    whatsapp_failure_reason: str | None = None


@router.post("/{order_id}/receipt/resend", response_model=ReceiptResendResponse)
async def resend_order_receipt(
    order_id: uuid.UUID,
    body: ReceiptResendRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Resend an order's digital receipt over WhatsApp — any order type/source
    (not just POS), for after-the-fact recovery (customer says they never
    got it, staff wants to re-trigger after fixing a phone number, etc.).
    OWNER/MANAGER only, since this always forces a resend even if the
    receipt already shows SENT/DELIVERED — see
    backend/app/receipts/whatsapp_receipt.py::FORCE_RESEND_ROLES, enforced
    here at the route layer (STAFF cannot reach this endpoint at all).

    Reuses the exact same underlying function every other trigger point
    uses — no second implementation of "build and send a digital receipt".
    """
    order = await require_owned(db, Order, order_id, user.business_id, label="Order")

    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    business = biz_result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    explicit_phone = None
    if body.phone_number:
        explicit_phone = normalize_sa_mobile(body.phone_number)
        if explicit_phone is None:
            raise AppError(
                "INVALID_PHONE_NUMBER",
                "Enter a valid South African mobile number",
                422,
            )

    customer, phone_number_e164 = await resolve_receipt_customer_and_phone(
        db, order, user.business_id, explicit_phone,
    )

    try:
        receipt = await send_digital_receipt(
            db, order, business,
            channels={"WHATSAPP"},
            phone_number_e164=phone_number_e164,
            customer=customer,
            requested_by_user_id=user.user_id,
            force_resend=True,
        )
    except ReceiptDestinationUnavailable:
        # BUG-0073: the already-successful delivery record was left exactly
        # as it was rather than being downgraded to FAILED. Tell the caller
        # to supply a number instead of returning a misleading 200.
        raise AppError(
            "RECEIPT_NO_DESTINATION",
            "No phone number available to resend this receipt to — enter one to resend.",
            422,
        )

    receipt_url = f"{settings.BACKEND_PUBLIC_URL}{settings.API_V1_PREFIX}/r/{receipt.token}"
    return ReceiptResendResponse(
        id=receipt.id,
        order_id=receipt.order_id,
        receipt_url=receipt_url,
        whatsapp_status=receipt.whatsapp_status,
        whatsapp_failure_reason=receipt.whatsapp_failure_reason,
    )
