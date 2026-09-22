"""
POS routes — order creation, checkout, till (cash-up), and till-native
refund/void, for staff ringing up in-person orders at a physical till.

Unlike the existing dashboard manual-order endpoint (POST /business/orders),
every price here is computed server-side from the live menu catalog via
shared/pricing/engine.py — the client never submits a price_cents value.

Restaurant POS redesign (Phase 2): checkout is now atomic AND pays the order
in full at the till — a POS order is created already PAID, with the correct
initial fulfilment status (ACCEPTED for items needing kitchen prep, COMPLETED
for a pure grab-and-go sale), instead of being created UNPAID/NEW and forced
through the same NEW→ACCEPTED→…→payment-prompt lifecycle a remote WhatsApp
order uses. See KNOWN_BUGS.md BUG-0035 and DECISIONS.md for the full
rationale.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError, MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.v1.routes_orders import OrderResponse
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.ownership import require_owned
from backend.app.core.rbac import AuthUser, require_owner_or_manager, require_staff_or_above
from backend.app.db.session import get_db
from backend.app.inventory.ledger import post_sale_consumption, reverse_sale_consumption
from backend.app.pos.pricing import POSAddOnSelection, POSItemInput, resolve_pos_items
from backend.app.pos.till import compute_expected_cash_cents, compute_variance_cents
from backend.app.receipts.whatsapp_receipt import resolve_receipt_customer_and_phone, send_digital_receipt
from backend.app.utils.phone import normalize_sa_mobile
from shared.enums import AuditScope, OrderSource, OrderStatus, POSPaymentMethod
from shared.pricing.engine import apply_vat_to_total
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.order import Order, OrderEvent, OrderItem
from shared.models.till import TillSession
from shared.models.user import BusinessUser
from shared.utils import format_order_number

router = APIRouter(prefix="/business/pos", tags=["pos"])
settings = get_settings()


# ── Schemas ──────────────────────────────────────────────────────────────────

class POSAddOnSelectionRequest(BaseModel):
    add_on_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=99)


class POSOrderItemRequest(BaseModel):
    menu_item_id: uuid.UUID
    quantity: int = Field(ge=1, le=999)
    selected_option_ids: dict[str, str] = Field(default_factory=dict)
    add_on_selections: list[POSAddOnSelectionRequest] = Field(default_factory=list)
    removed_ingredient_ids: list[str] = Field(default_factory=list)
    special_instructions: str | None = None


class POSOrderCreateRequest(BaseModel):
    items: list[POSOrderItemRequest] = Field(min_length=1)
    order_mode: str = Field(default="PICKUP", pattern=r"^(PICKUP|DELIVERY|DINE_IN)$")
    table_number: str | None = Field(default=None, max_length=32)
    customer_name: str | None = Field(default=None, max_length=255)
    customer_phone: str | None = Field(default=None, max_length=32)
    discount_cents: int = Field(default=0, ge=0)
    discount_reason: str | None = Field(default=None, max_length=255)

    # ── Payment (Phase 2 — paid at checkout, not after fulfilment) ────────
    payment_method: str = Field(pattern=r"^(CASH|CARD|OTHER)$")
    cash_tendered_cents: int | None = Field(default=None, ge=0)
    payment_reference: str | None = Field(default=None, max_length=255)

    # A client-generated key; retrying the same checkout with the same key
    # returns the original order instead of ringing up a second sale.
    idempotency_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_cash_tender(self) -> "POSOrderCreateRequest":
        if self.payment_method == POSPaymentMethod.CASH.value:
            if self.cash_tendered_cents is None:
                raise ValueError("cash_tendered_cents is required for a CASH payment")
        elif self.cash_tendered_cents is not None:
            raise ValueError("cash_tendered_cents is only valid for a CASH payment")
        return self


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
    # Populated only in the response returned directly by POST /close (this
    # session's own object never stores them — they're derived at close time
    # from the linked orders, same as expected_cash_cents itself).
    cash_sales_cents: int | None = None
    cash_refunds_cents: int | None = None
    opened_at: datetime
    closed_at: datetime | None
    model_config = {"from_attributes": True}


class TillSessionHistoryEntry(BaseModel):
    """
    One past cash-up, for the owner/manager history view. Carries the staff
    names resolved server-side so the client never has to hold a user
    directory to render an audit trail.
    """
    id: uuid.UUID
    status: str
    opening_float_cents: int
    expected_cash_cents: int | None
    counted_cash_cents: int | None
    variance_cents: int | None
    opened_at: datetime
    closed_at: datetime | None
    opened_by_name: str | None = None
    closed_by_name: str | None = None


class POSRefundRequest(BaseModel):
    amount_cents: int | None = Field(default=None, ge=1, description="Defaults to a full refund of total_cents")
    reason: str | None = Field(default=None, max_length=255)

    # Money moving back to the customer does NOT imply food coming back to
    # the shelf. A refunded pizza that was already made and handed over is
    # gone; restoring its ingredients would invent stock that does not
    # physically exist. So this defaults to False and the manager must say
    # so explicitly — see void() for the case where nothing was ever made.
    restore_stock: bool = Field(
        default=False,
        description="Only true when the food physically came back and is still usable",
    )


class POSVoidRequest(BaseModel):
    reason: str = Field(max_length=255)


_RECEIPT_CHANNELS = {"PRINT", "WHATSAPP"}


class POSReceiptRequest(BaseModel):
    channels: list[str] = Field(min_length=1, max_length=2)
    phone_number: str | None = Field(default=None, max_length=32)
    force_resend: bool = False

    @model_validator(mode="after")
    def _validate_channels(self) -> "POSReceiptRequest":
        cleaned = [c.upper() for c in self.channels]
        unknown = sorted(set(cleaned) - _RECEIPT_CHANNELS)
        if unknown:
            raise ValueError(f"Unknown receipt channel(s): {unknown}")
        self.channels = cleaned
        return self


class POSReceiptResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    receipt_url: str
    print_requested: bool
    whatsapp_requested: bool
    whatsapp_status: str
    whatsapp_failure_reason: str | None = None


class POSSettingsResponse(BaseModel):
    """
    Staff-readable subset of Business's POS settings — GET /business/settings
    itself is owner/manager-only, but a cashier needs to know which payment
    buttons to show and whether a table number is required. Never exposes
    anything beyond these till-facing toggles.
    """
    cash_enabled: bool
    card_enabled: bool
    other_payment_enabled: bool
    table_number_required: bool
    delivery_fee_cents: int
    max_staff_discount_cents: int

    # VAT — the till needs these to show a correct tax line before checkout
    vat_registered: bool = False
    vat_rate_basis_points: int = 0
    vat_inclusive_pricing: bool = True

    # Receipt/printing — drives auto-print behaviour and paper width in the UI
    receipt_paper_width_mm: int = 80
    receipt_auto_print: bool = True
    receipt_copies: int = 1
    print_kitchen_receipt: bool = False

    # Inventory — the till shows the quick-wastage action only when enabled
    inventory_enabled: bool = False

    # Staff navigation — which tabs this tenant's bottom bar should show.
    # Display configuration only: a hidden tab never disables the underlying
    # feature, and the API for each still enforces its own permissions.
    staff_live_orders_tab_enabled: bool = True
    staff_wastage_tab_enabled: bool = True
    staff_stocktake_tab_enabled: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _get_open_till(db: AsyncSession, business_id: uuid.UUID) -> TillSession:
    """
    Load (and lock) this business's OPEN till session.

    Locked FOR UPDATE for the rest of the caller's transaction so a checkout
    cannot attach to a session that a concurrent `close_till` is in the
    middle of totalling (see `close_till`, which locks the same row) — one
    of the two commits will simply wait for the other instead of both
    reading a stale/half-computed state.

    `uq_till_sessions_one_open_per_business` (migration 0032) should make
    more than one OPEN row impossible, but this is belt-and-braces: if it
    ever happens anyway (e.g. a row inserted before that migration ran),
    `scalar_one_or_none()` raises `MultipleResultsFound` rather than 500ing
    every checkout for this business — fall back to the most recently
    opened session and log it as the real bug it would be.
    """
    query = (
        select(TillSession)
        .where(
            TillSession.business_id == business_id,
            TillSession.status == "OPEN",
        )
        .with_for_update()
    )
    try:
        session = (await db.execute(query)).scalar_one_or_none()
    except MultipleResultsFound:
        import logging
        logging.getLogger("nextgen").error(
            "Multiple OPEN till sessions found for business %s — this should "
            "be impossible after uq_till_sessions_one_open_per_business; "
            "falling back to the most recently opened session",
            business_id,
        )
        fallback = await db.execute(
            query.order_by(TillSession.opened_at.desc()).limit(1)
        )
        session = fallback.scalars().first()

    if not session:
        raise AppError(
            "TILL_NOT_OPEN",
            "Open the till before ringing up a sale",
            409,
        )
    return session


def _is_unique_violation(exc: IntegrityError, constraint_name: str) -> bool:
    """
    True if this IntegrityError is a violation of the named unique
    constraint/index — checked against the driver's own error text (what
    Postgres actually reports for a unique_violation) rather than an
    asyncpg-specific attribute, so this stays driver-agnostic.
    """
    return constraint_name in str(getattr(exc, "orig", exc))


async def _replay_idempotent_checkout(
    db: AsyncSession,
    business_id: uuid.UUID,
    idempotency_key: str | None,
    exc: IntegrityError,
    attempted_total_cents: int,
    attempted_item_count: int,
) -> Order | None:
    """
    A double-tapped checkout with the SAME idempotency key can both pass the
    initial "does this key already exist?" lookup before either has
    committed — `uq_orders_business_idempotency_key` (migration 0023) is
    what actually decides the winner, and the loser must replay the
    winner's order instead of 500ing.

    Returns the winning order, or None if this IntegrityError was NOT an
    idempotency-key collision — the caller must re-raise in that case, since
    any other constraint violation is a real bug this must never mask.
    """
    if not idempotency_key or not _is_unique_violation(exc, "uq_orders_business_idempotency_key"):
        return None

    existing = await db.execute(
        select(Order).where(
            Order.business_id == business_id,
            Order.idempotency_key == idempotency_key,
        )
    )
    existing_order = existing.scalar_one_or_none()
    if not existing_order:
        # The row that won the race committed strictly before this insert
        # could fail against it, so it should be visible by now. If it
        # somehow isn't, this wasn't a clean replay — let the original
        # error propagate rather than silently returning nothing useful.
        return None

    if existing_order.total_cents != attempted_total_cents or len(existing_order.items) != attempted_item_count:
        import logging
        logging.getLogger("nextgen").warning(
            "Idempotency replay for key=%s returned a different order than "
            "this request would have created: existing total_cents=%s "
            "items=%s vs this request's total_cents=%s items=%s",
            idempotency_key, existing_order.total_cents, len(existing_order.items),
            attempted_total_cents, attempted_item_count,
        )
    return existing_order


_POS_METHOD_SETTING = {
    POSPaymentMethod.CASH.value: "pos_cash_enabled",
    POSPaymentMethod.CARD.value: "pos_card_enabled",
    # OTHER is a catch-all for whichever specific "other" provider a business
    # has switched on (PayFast/Yoco/iKhokha/Direct EFT) — any one of them
    # being enabled is enough to show/accept the generic "Other" button.
}


def _payment_method_enabled(business: Business, payment_method: str) -> bool:
    if payment_method == POSPaymentMethod.CASH.value:
        return business.pos_cash_enabled
    if payment_method == POSPaymentMethod.CARD.value:
        return business.pos_card_enabled
    return (
        business.pos_payfast_enabled
        or business.pos_yoco_enabled
        or business.pos_ikhoka_enabled
        or business.pos_direct_eft_enabled
        or business.pos_other_payment_enabled
    )


@router.get("/settings", response_model=POSSettingsResponse)
async def get_pos_settings(
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """Which payment buttons the till should show, and whether it must collect a table number."""
    result = await db.execute(select(Business).where(Business.id == user.business_id))
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")
    return POSSettingsResponse(
        cash_enabled=business.pos_cash_enabled,
        card_enabled=business.pos_card_enabled,
        other_payment_enabled=(
            business.pos_payfast_enabled
            or business.pos_yoco_enabled
            or business.pos_ikhoka_enabled
            or business.pos_direct_eft_enabled
            or business.pos_other_payment_enabled
        ),
        table_number_required=business.pos_table_number_required,
        delivery_fee_cents=business.delivery_fee_cents,
        max_staff_discount_cents=business.max_staff_discount_cents,
        vat_registered=business.vat_registered,
        vat_rate_basis_points=business.vat_rate_basis_points,
        vat_inclusive_pricing=business.vat_inclusive_pricing,
        receipt_paper_width_mm=business.receipt_paper_width_mm,
        receipt_auto_print=business.receipt_auto_print,
        receipt_copies=business.receipt_copies,
        print_kitchen_receipt=business.print_kitchen_receipt,
        inventory_enabled=business.inventory_enabled,
        staff_live_orders_tab_enabled=business.staff_live_orders_tab_enabled,
        staff_wastage_tab_enabled=business.staff_wastage_tab_enabled,
        # A stocktake tab is only useful if there is stock to count. Gating the
        # display on both flags means enabling the tab before the inventory
        # module cannot strand staff on a tab whose API will refuse them.
        staff_stocktake_tab_enabled=(
            business.staff_stocktake_tab_enabled and business.inventory_enabled
        ),
    )


# ── Order creation / checkout ─────────────────────────────────────────────────

@router.post("/orders", response_model=OrderResponse, status_code=201)
async def create_pos_order(
    body: POSOrderCreateRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Ring up and PAY for an order at the till in one atomic operation. Every
    menu item / option / add-on ID is re-validated against this business's
    live catalog and priced via shared/pricing/engine.py — no client-
    submitted price is ever trusted. Unlike a WhatsApp/manual order, a POS
    order is created already payment_status=PAID; its fulfilment status
    starts at ACCEPTED (if any item needs kitchen prep) or COMPLETED
    (grab-and-go) — never NEW, since ringing it up at the till already IS
    the acceptance moment.
    """
    # ── Idempotency: a retried/double-tapped checkout must never create a
    #    second sale — return the original order unchanged instead. ───────
    if body.idempotency_key:
        existing = await db.execute(
            select(Order).where(
                Order.business_id == user.business_id,
                Order.idempotency_key == body.idempotency_key,
            )
        )
        existing_order = existing.scalar_one_or_none()
        if existing_order:
            return OrderResponse.model_validate(existing_order)

    # ── Till must be open before any sale can be rung up ──────────────────
    till_session = await _get_open_till(db, user.business_id)

    biz_check_result = await db.execute(
        select(Business).where(Business.id == user.business_id)
    )
    business = biz_check_result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    if not _payment_method_enabled(business, body.payment_method):
        raise AppError(
            "PAYMENT_METHOD_NOT_ACCEPTED",
            f"This business does not accept {body.payment_method} at the till",
            422,
        )

    if business.pos_table_number_required and body.order_mode == "DINE_IN" and not body.table_number:
        raise AppError("TABLE_NUMBER_REQUIRED", "A table number is required for dine-in orders", 422)

    item_inputs = [
        POSItemInput(
            menu_item_id=item.menu_item_id,
            quantity=item.quantity,
            selected_option_ids=item.selected_option_ids,
            add_on_selections=[
                POSAddOnSelection(add_on_id=sel.add_on_id, quantity=sel.quantity)
                for sel in item.add_on_selections
            ],
            removed_ingredient_ids=item.removed_ingredient_ids,
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

    max_staff_discount = business.max_staff_discount_cents or 0
    if body.discount_cents > 0 and user.role != "OWNER" and body.discount_cents > max_staff_discount:
        raise AppError(
            "DISCOUNT_LIMIT_EXCEEDED",
            f"This discount requires owner approval (staff limit is {max_staff_discount} cents)",
            403,
        )

    delivery_fee_cents = business.delivery_fee_cents if body.order_mode == "DELIVERY" else 0
    net_total_cents = max(0, subtotal_cents - body.discount_cents + delivery_fee_cents)

    # ── VAT ───────────────────────────────────────────────────────────────
    # Computed BEFORE cash validation because exclusive VAT increases what
    # the customer actually owes. Inclusive (the SA norm) leaves the total
    # untouched and only reports the tax portion inside it, so switching VAT
    # on never silently reprices a tenant's menu.
    if business.vat_registered:
        total_cents, tax_cents = apply_vat_to_total(
            net_total_cents,
            business.vat_rate_basis_points,
            business.vat_inclusive_pricing,
        )
    else:
        total_cents, tax_cents = net_total_cents, 0

    # ── Payment validation — computed here, never trusted from the client ─
    if body.payment_method == POSPaymentMethod.CASH.value:
        if body.cash_tendered_cents < total_cents:
            raise AppError(
                "INSUFFICIENT_CASH_TENDERED",
                f"Cash received ({body.cash_tendered_cents} cents) is less than the total ({total_cents} cents)",
                422,
            )
        change_due_cents = body.cash_tendered_cents - total_cents
    else:
        change_due_cents = None

    # A sale containing ANY item that needs kitchen prep goes to the kitchen
    # (starting at ACCEPTED — no separate "accept" click, since paying for
    # it at the till already is the acceptance). A pure grab-and-go sale
    # (drinks, packaged snacks) completes immediately with no kitchen ticket.
    needs_kitchen = any(r.requires_preparation for r in resolved_items)
    initial_status = OrderStatus.ACCEPTED if needs_kitchen else OrderStatus.COMPLETED

    # ── Atomic order-number increment (same pattern as the manual-order route) ─
    result = await db.execute(
        sa_update(Business)
        .where(Business.id == user.business_id)
        .values(order_number_sequence=Business.order_number_sequence + 1)
        .returning(Business.order_number_sequence, Business.currency)
    )
    row = result.one_or_none()
    if not row:
        raise NotFoundError("Business")
    seq, currency = row

    # Link a Customer record by phone number when loyalty is enabled, so
    # points can be awarded once this order is created (already PAID).
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

    now = datetime.now(timezone.utc)

    order = Order(
        business_id=user.business_id,
        customer_id=customer_id,
        order_number=format_order_number(seq),
        status=initial_status.value,
        payment_status="PAID",
        payment_required=False,
        payment_method=body.payment_method,
        payment_reference=body.payment_reference,
        paid_at=now,
        cash_tendered_cents=body.cash_tendered_cents,
        change_due_cents=change_due_cents,
        till_session_id=till_session.id,
        idempotency_key=body.idempotency_key,
        created_by_user_id=user.user_id,
        order_mode=body.order_mode,
        table_number=body.table_number if body.order_mode == "DINE_IN" else None,
        source=OrderSource.POS.value,
        subtotal_cents=subtotal_cents,
        delivery_fee_cents=delivery_fee_cents,
        discount_cents=body.discount_cents,
        tax_cents=tax_cents,
        discount_reason=body.discount_reason,
        discount_approved_by_user_id=user.user_id if body.discount_cents > 0 else None,
        total_cents=total_cents,
        currency=currency,
        customer_name=body.customer_name,
        phone_number=body.customer_phone,
        confirmed_at=now,
        accepted_at=now if initial_status == OrderStatus.ACCEPTED else None,
        completed_at=now if initial_status == OrderStatus.COMPLETED else None,
    )
    db.add(order)

    # ── Everything from here to the final commit is one all-or-nothing
    #    attempt. A double-tapped checkout with the SAME idempotency_key can
    #    race past the early replay-lookup above before either side has
    #    committed — the partial unique index on (business_id,
    #    idempotency_key) is what actually decides the winner. The loser's
    #    flush/commit raises IntegrityError here (Postgres enforces a
    #    non-deferred unique index immediately, so this most often surfaces
    #    at the `db.flush()` right after `db.add(order)`, not at the final
    #    commit) — caught below and replayed into the winner's order instead
    #    of propagating as an unhandled 500. ───────────────────────────────
    try:
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
                removed_ingredients_snapshot=resolved.removed_ingredients_snapshot or None,
                special_instructions=resolved.special_instructions,
            ))

        db.add(OrderEvent(
            order_id=order.id,
            business_id=user.business_id,
            old_status=None,
            new_status=initial_status.value,
            changed_by_user_id=user.user_id,
            reason=f"POS checkout — paid by {body.payment_method}",
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

        # ── Theoretical stock consumption ───────────────────────────────
        # Staged inside this same transaction so a sale and its stock
        # deduction commit together or not at all — a sale that silently
        # failed to deduct would show up later as unexplained variance,
        # which is exactly what this subsystem exists to eliminate.
        #
        # Deliberately atomic rather than best-effort: resolution reads the
        # recipes and costs BEFORE staging any movement, so the only way it
        # can fail is a database problem that would have failed the order
        # insert too. A menu item with no recipe is NOT an error — it
        # resolves to nothing and is reported back, because blocking a sale
        # over missing reference data would be far worse than a known
        # reporting gap.
        #
        # Skipped entirely for tenants without the inventory module, so
        # every existing restaurant's checkout behaves exactly as before.
        consumption = None
        if business.inventory_enabled:
            consumption = await post_sale_consumption(
                db,
                business_id=user.business_id,
                order_id=order.id,
                resolved_items=resolved_items,
                created_by_user_id=user.user_id,
                occurred_at=now,
            )
            if consumption.menu_items_without_recipe:
                db.add(AuditEvent(
                    scope=AuditScope.BUSINESS.value,
                    business_id=user.business_id,
                    actor_user_id=user.user_id,
                    action="inventory.sale.missing_recipe",
                    target_type="order",
                    target_id=order.id,
                    diff_json={
                        "menu_item_ids": [
                            str(i) for i in consumption.menu_items_without_recipe
                        ],
                        "note": "Sold with no active recipe — no stock was deducted "
                                "for these items, so variance will understate usage",
                    },
                ))

        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        replayed = await _replay_idempotent_checkout(
            db, user.business_id, body.idempotency_key, exc,
            attempted_total_cents=total_cents, attempted_item_count=len(resolved_items),
        )
        if replayed is not None:
            return OrderResponse.model_validate(replayed)
        raise

    await db.refresh(order)

    # ── Award loyalty points — POS orders are already PAID at creation, so
    #    this fires once, right here, instead of waiting for a later
    #    payment-confirmation event (award_loyalty_points_if_enabled is
    #    itself idempotent-safe against re-invocation since it only ever
    #    runs once per order creation call, never on the idempotent-replay
    #    branch above which returns before reaching this line). ───────────
    if customer_id:
        from backend.app.loyalty.engine import award_loyalty_points_if_enabled
        await award_loyalty_points_if_enabled(db, user.business_id, order)
        await db.refresh(order)

    # Record usage (fire and forget) — see routes_orders.py::create_order
    # for why every order-creation path must call this, not just the bot.
    # `increment_usage` only flushes, it never commits (by design, so a
    # caller mid-transaction isn't forced to commit early) — this was the
    # entire bug (BUG-0050): every previous caller committed BEFORE calling
    # it, so the increment was flushed into a session that then closed
    # without ever committing it, silently discarding it. Committing here,
    # inside this same try/except, means a usage-tracking failure still can
    # never fail the sale itself (the sale already committed above).
    try:
        from backend.app.bot.usage_tracker import increment_usage
        await increment_usage(
            db, user.business_id, business.timezone or "Africa/Johannesburg",
            orders_created=1, revenue_cents=order.total_cents,
        )
        await db.commit()
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


# ── Till-native refund / void (POS payment methods only — a business with
#    no online payment provider configured still needs to be able to
#    reverse a cash/card till sale; routes_orders.py's /refund endpoint is
#    provider-dependent and is not appropriate for a physical till sale) ──

async def _load_pos_order_for_reversal(db: AsyncSession, business_id: uuid.UUID, order_id: uuid.UUID) -> Order:
    # Locked FOR UPDATE for the rest of the caller's transaction so two
    # concurrent refund/void calls on the same order can't both read it as
    # still PAID and both reverse it — the second call blocks until the
    # first commits, then sees the already-reversed payment_status and is
    # correctly rejected by the check below instead of double-reversing.
    result = await db.execute(
        select(Order)
        .where(Order.id == order_id, Order.business_id == business_id)
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError("Order", str(order_id))
    if order.source != OrderSource.POS.value:
        raise AppError(
            "NOT_A_POS_ORDER",
            "This endpoint only reverses POS-sourced orders — use the standard refund endpoint for other sources",
            422,
        )
    if order.payment_status != "PAID":
        raise AppError(
            "ORDER_NOT_PAID",
            f"Cannot reverse an order with payment_status={order.payment_status!r} — only PAID orders can be refunded or voided",
            422,
        )
    return order


async def _require_open_till_for_cash_reversal(db: AsyncSession, business_id: uuid.UUID, order: Order) -> None:
    """
    A CASH refund/void hands physical cash back out of a drawer — if no
    till is open, that cash-out has nowhere to be reconciled against at the
    next cash-up (`close_till`'s expected-cash calculation only nets
    refunds against whichever session happens to be OPEN when it closes).
    Non-cash reversals (card/other) never touch a physical drawer, so they
    are unaffected.
    """
    if order.payment_method != POSPaymentMethod.CASH.value:
        return
    # Locked FOR UPDATE — same row/lock close_till() takes on this session.
    # Without this, a cash refund committing just after close_till has
    # already computed its expected-cash totals (but before this refund
    # existed) would land in no till session at all: this SELECT would
    # still see status="OPEN" (read before close_till's commit) and pass,
    # then the refund's own commit races close_till's — whichever commits
    # last silently wins, and the loser's cash movement is reconciled
    # against nothing (F4 fix, Wave 0A verifier).
    result = await db.execute(
        select(TillSession)
        .where(
            TillSession.business_id == business_id,
            TillSession.status == "OPEN",
        )
        .with_for_update()
    )
    if not result.scalar_one_or_none():
        raise AppError(
            "TILL_NOT_OPEN",
            "A cash refund or void requires an open till session so the "
            "cash paid out is reconciled at the next cash-up — open the till first.",
            409,
        )


@router.post("/orders/{order_id}/refund", response_model=OrderResponse)
async def refund_pos_order(
    order_id: uuid.UUID,
    body: POSRefundRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Refund a paid POS sale (full or partial). No external payment provider
    is involved — cash is handed back physically, a card refund is
    processed on the same terminal the sale used — this endpoint only
    records that it happened, same philosophy as Phase 7's manually
    confirmed card payments. OWNER/MANAGER only, same as a POS discount
    above the staff limit.
    """
    order = await _load_pos_order_for_reversal(db, user.business_id, order_id)
    await _require_open_till_for_cash_reversal(db, user.business_id, order)

    amount_cents = body.amount_cents or order.total_cents
    if amount_cents > order.total_cents:
        raise AppError("REFUND_EXCEEDS_TOTAL", "Refund amount cannot exceed the order total", 422)

    order.payment_status = "REFUNDED" if amount_cents == order.total_cents else "PARTIALLY_REFUNDED"
    order.refund_amount_cents = amount_cents
    order.refund_reference = body.reason
    order.refunded_at = datetime.now(timezone.utc)

    # Money going back does not mean food came back. A refunded pizza that
    # was made and handed over is gone, and restoring its ingredients would
    # invent stock that does not physically exist — so this only happens
    # when the manager explicitly says the food returned usable.
    reversed_lines = 0
    if body.restore_stock:
        business_result = await db.execute(
            select(Business).where(Business.id == user.business_id)
        )
        business = business_result.scalar_one_or_none()
        if business and business.inventory_enabled:
            reversed_lines = await reverse_sale_consumption(
                db,
                business_id=user.business_id,
                order_id=order.id,
                created_by_user_id=user.user_id,
                notes=f"Refund with stock returned: {body.reason}",
            )

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=user.business_id,
        actor_user_id=user.user_id,
        action="order.pos.refunded",
        target_type="order",
        target_id=order.id,
        diff_json={
            "amount_cents": amount_cents,
            "reason": body.reason,
            "restore_stock": body.restore_stock,
            "stock_lines_reversed": reversed_lines,
        },
    ))

    await db.commit()
    await db.refresh(order)
    return OrderResponse.model_validate(order)


@router.post("/orders/{order_id}/void", response_model=OrderResponse)
async def void_pos_order(
    order_id: uuid.UUID,
    body: POSVoidRequest,
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Void a mis-rung POS sale — a full reversal distinct from a refund in
    intent (nothing should have happened at all) though identical in effect
    on till accounting. Cancels fulfilment and marks payment VOIDED rather
    than REFUNDED so reporting can tell the two apart. OWNER/MANAGER only.
    """
    order = await _load_pos_order_for_reversal(db, user.business_id, order_id)
    await _require_open_till_for_cash_reversal(db, user.business_id, order)

    order.payment_status = "VOIDED"
    order.refund_amount_cents = order.total_cents
    order.refund_reference = body.reason
    order.refunded_at = datetime.now(timezone.utc)
    order.status = OrderStatus.CANCELLED.value
    order.cancelled_reason = body.reason
    order.cancelled_by_user_id = user.user_id

    # A void means the sale should never have happened, so the ingredients
    # go back. Reversal mirrors the ORIGINAL ledger rows rather than
    # re-resolving the recipe — if the recipe changed between the sale and
    # the void, re-resolving would put back a different quantity and invent
    # stock. It is also idempotent: a second void writes nothing.
    reversed_lines = 0
    business_result = await db.execute(
        select(Business).where(Business.id == user.business_id)
    )
    business = business_result.scalar_one_or_none()
    if business and business.inventory_enabled:
        reversed_lines = await reverse_sale_consumption(
            db,
            business_id=user.business_id,
            order_id=order.id,
            created_by_user_id=user.user_id,
            notes=f"Void: {body.reason}",
        )

    db.add(AuditEvent(
        scope=AuditScope.BUSINESS.value,
        business_id=user.business_id,
        actor_user_id=user.user_id,
        action="order.pos.voided",
        target_type="order",
        target_id=order.id,
        diff_json={"reason": body.reason, "stock_lines_reversed": reversed_lines},
    ))

    await db.commit()
    await db.refresh(order)
    return OrderResponse.model_validate(order)


# ── Digital receipt ──────────────────────────────────────────────────────────

@router.post("/orders/{order_id}/receipt", response_model=POSReceiptResponse)
async def request_pos_receipt(
    order_id: uuid.UUID,
    body: POSReceiptRequest,
    user: AuthUser = Depends(require_staff_or_above),
    db: AsyncSession = Depends(get_db),
):
    """
    Request a receipt for a POS sale — print (audit only, till printing
    itself is unchanged/client-side) and/or WhatsApp (template-based, see
    backend/app/receipts/whatsapp_receipt.py::send_digital_receipt).

    STAFF may request a receipt for the first time; only OWNER/MANAGER may
    set force_resend=True to re-trigger a WhatsApp send that already shows
    SENT/DELIVERED (checked here at the route layer — send_digital_receipt
    itself does not know or enforce roles).
    """
    order = await require_owned(db, Order, order_id, user.business_id, label="Order")

    biz_result = await db.execute(select(Business).where(Business.id == user.business_id))
    business = biz_result.scalar_one_or_none()
    if not business:
        raise NotFoundError("Business")

    if body.force_resend and user.role not in ("OWNER", "MANAGER"):
        raise AppError(
            "FORBIDDEN",
            "Only an owner or manager can force a receipt resend",
            403,
        )

    explicit_phone = None
    if body.phone_number:
        explicit_phone = normalize_sa_mobile(body.phone_number)
        if explicit_phone is None:
            raise AppError(
                "INVALID_PHONE_NUMBER",
                "Enter a valid South African mobile number",
                422,
            )

    customer = None
    phone_number_e164 = explicit_phone
    if "WHATSAPP" in body.channels:
        customer, phone_number_e164 = await resolve_receipt_customer_and_phone(
            db, order, user.business_id, explicit_phone,
        )

    receipt = await send_digital_receipt(
        db, order, business,
        channels=set(body.channels),
        phone_number_e164=phone_number_e164,
        customer=customer,
        requested_by_user_id=user.user_id,
        force_resend=body.force_resend,
    )

    receipt_url = f"{settings.BACKEND_PUBLIC_URL}{settings.API_V1_PREFIX}/r/{receipt.token}"
    return POSReceiptResponse(
        id=receipt.id,
        order_id=receipt.order_id,
        receipt_url=receipt_url,
        print_requested=receipt.print_requested,
        whatsapp_requested=receipt.whatsapp_requested,
        whatsapp_status=receipt.whatsapp_status,
        whatsapp_failure_reason=receipt.whatsapp_failure_reason,
    )


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
    # The SELECT above is a friendly pre-check for the common (non-racing)
    # case — the actual guarantee is `uq_till_sessions_one_open_per_business`
    # (migration 0032). Two simultaneous opens can both pass that pre-check;
    # the loser's commit then raises IntegrityError here instead of a 500.
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if _is_unique_violation(exc, "uq_till_sessions_one_open_per_business"):
            raise AppError("TILL_ALREADY_OPEN", "A till is already open.", 409)
        raise
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
    # Locked FOR UPDATE so a checkout that's mid-transaction against this
    # same till row (`_get_open_till` also locks it) can't attach a sale to
    # this session after its totals have already been computed below — one
    # of the two commits simply waits for the other's lock to release.
    result = await db.execute(
        select(TillSession)
        .where(
            TillSession.id == session_id,
            TillSession.business_id == user.business_id,
        )
        .with_for_update()
    )
    session = result.scalar_one_or_none()
    if not session:
        raise NotFoundError("TillSession", str(session_id))
    if session.status != "OPEN":
        raise AppError("TILL_ALREADY_CLOSED", "This till session has already been closed", 422)

    # Linked directly via Order.till_session_id (set at checkout) rather than
    # the old opened_at/closed_at timestamp-window heuristic — exact, and
    # unaffected by clock skew or a sale that's paid slightly before/after
    # the session boundary. Deliberately includes REFUNDED/PARTIALLY_REFUNDED/
    # VOIDED orders too, not just PAID ones: the cash physically went into
    # this drawer at sale time regardless of what happened to it later — the
    # separate cash_refunds query below is what nets that back out, scoped to
    # whichever session the refund itself happened in (see its own comment;
    # excluding non-PAID orders here would silently under-count sales and
    # then also subtract the refund, double-reversing the same transaction).
    cash_result = await db.execute(
        select(Order.total_cents).where(
            Order.till_session_id == session.id,
            Order.payment_method == "CASH",
            Order.payment_status.in_(["PAID", "REFUNDED", "PARTIALLY_REFUNDED", "VOIDED"]),
        )
    )
    cash_sales_cents = sum(cash_result.scalars().all())

    # NOT filtered by till_session_id: that column records which session rang
    # the ORIGINAL sale up, but the cash physically leaves THIS drawer at
    # refund time, which may be a later session than the one that took the
    # original sale (e.g. a next-day refund). Only one till session can ever
    # be OPEN at once, so "refunded after this session opened" unambiguously
    # means "refunded during this session" for the currently-open session
    # being closed.
    cash_refund_result = await db.execute(
        select(Order.refund_amount_cents).where(
            Order.business_id == user.business_id,
            Order.payment_method == "CASH",
            Order.payment_status.in_(["REFUNDED", "PARTIALLY_REFUNDED", "VOIDED"]),
            Order.refund_amount_cents.is_not(None),
            Order.refunded_at >= session.opened_at,
        )
    )
    cash_refunds_cents = sum(cash_refund_result.scalars().all())

    expected = compute_expected_cash_cents(
        session.opening_float_cents, cash_sales_cents, cash_refunds_cents,
    )
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
            "cash_sales_cents": cash_sales_cents,
            "cash_refunds_cents": cash_refunds_cents,
        },
    ))

    await db.commit()
    await db.refresh(session)
    response = TillSessionResponse.model_validate(session)
    response.cash_sales_cents = cash_sales_cents
    response.cash_refunds_cents = cash_refunds_cents
    return response


@router.get("/till/sessions", response_model=list[TillSessionHistoryEntry])
async def list_till_sessions(
    limit: int = Query(50, ge=1, le=200),
    user: AuthUser = Depends(require_owner_or_manager),
    db: AsyncSession = Depends(get_db),
):
    """
    Cash-up history, newest first — owner/manager only, because a variance is
    a statement about a named cashier's drawer and is not every cashier's
    business to browse.

    Read-only by construction. A closed session is never mutated (close is a
    one-way transition enforced in close_till), so this is the durable record
    of who opened the drawer, what was expected, what was counted, and by how
    much the two differed.
    """
    rows = (
        (
            await db.execute(
                select(TillSession)
                .where(TillSession.business_id == user.business_id)
                .order_by(TillSession.opened_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    # Resolve staff names in one query rather than per row.
    user_ids = {s.opened_by_user_id for s in rows if s.opened_by_user_id}
    user_ids |= {s.closed_by_user_id for s in rows if s.closed_by_user_id}
    names: dict[uuid.UUID, str] = {}
    if user_ids:
        for bu in (
            (
                await db.execute(
                    select(BusinessUser).where(
                        BusinessUser.business_id == user.business_id,
                        BusinessUser.id.in_(user_ids),
                    )
                )
            )
            .scalars()
            .all()
        ):
            names[bu.id] = bu.staff_name or bu.email or "Unknown"

    return [
        TillSessionHistoryEntry(
            id=s.id,
            status=s.status,
            opening_float_cents=s.opening_float_cents,
            expected_cash_cents=s.expected_cash_cents,
            counted_cash_cents=s.counted_cash_cents,
            variance_cents=s.variance_cents,
            opened_at=s.opened_at,
            closed_at=s.closed_at,
            opened_by_name=names.get(s.opened_by_user_id) if s.opened_by_user_id else None,
            closed_by_name=names.get(s.closed_by_user_id) if s.closed_by_user_id else None,
        )
        for s in rows
    ]
