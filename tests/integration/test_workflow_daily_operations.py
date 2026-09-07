"""
Workflow 2 (Restaurant Daily Operations), driven against a REAL Postgres +
Redis instance — the second real-DB integration test in this codebase's
history (see test_workflow_full_lifecycle.py for the first).

Covers: order creation -> kitchen workflow -> payment -> refund -> cash-up
(till open/close) -> loyalty redemption -> reports (CSV export) ->
analytics. Every step calls the REAL route-handler function with a REAL
AsyncSession against the REAL local dev Postgres.

"Modify orders" (from the workflow spec) has no dedicated endpoint in this
codebase today — an order's line items cannot be edited after creation, only
its status/payment can change. That is exercised here as-is; it is not a gap
introduced by this test.

"Inventory deduction" (from the workflow spec) is explicitly NOT exercised:
no inventory system exists anywhere in this codebase. `PRODUCT_ROADMAP.md`
already documents this as "Placeholder only" (`MenuAddOn.inventory_item_id`
is a nullable FK with no backing table) and places a real inventory module
in Phase 2, deliberately deferred. The commercial pricing copy driving this
autonomous session's mega-prompts lists "Inventory" as an included Phase-1
feature — that conflict is a business-packaging decision, not something to
resolve by building a speculative inventory module here. See TODO.md.

"Receipt" is a frontend rendering concern (CustomerReceipt.tsx) with no
backend endpoint of its own to integration-test here.
"""
import uuid

import pytest
from sqlalchemy import select

from backend.app.api.v1.routes_business import BusinessSettingsUpdate, update_business_settings
from backend.app.api.v1.routes_export import export_orders_csv
from backend.app.api.v1.routes_loyalty import (
    LoyaltyProgramUpdate,
    LoyaltyRewardCreate,
    RedeemRequest,
    create_reward,
    redeem_loyalty_reward,
    update_loyalty_program,
)
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_orders import (
    StatusUpdateRequest,
    update_order_status,
)
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSRefundRequest,
    TillCloseRequest,
    TillOpenRequest,
    close_till,
    create_pos_order,
    open_till,
    refund_pos_order,
)
from backend.app.api.v1.routes_staff import StaffCreate, create_staff
from backend.app.api.v1.routes_analytics import get_summary
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.customer import Customer


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


def _staff(business_id: uuid.UUID, staff_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=staff_id, business_id=business_id, role="STAFF", token_type="access")


async def test_daily_operations_workflow(db, test_business: Business):
    owner = _owner(test_business.id)

    # ── Setup: payment provider, loyalty, menu, staff ────────────────────
    await update_business_settings(
        body=BusinessSettingsUpdate(payment_provider="MOCK"), user=owner, db=db,
    )
    await update_loyalty_program(
        body=LoyaltyProgramUpdate(is_enabled=True, points_per_rand=1, points_per_order=0, min_redemption_points=0),
        user=owner, db=db,
    )
    category = await create_category(body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=db)
    item = await create_item(
        body=ItemCreate(category_id=category.id, name="Burger", price_cents=8000),
        user=owner, db=db,
    )
    reward = await create_reward(
        body=LoyaltyRewardCreate(name="Free Drink", points_cost=50), user=owner, db=db,
    )
    staff_response = await create_staff(body=StaffCreate(staff_name="Cashier", role="STAFF"), user=owner, db=db)
    staff_user = _staff(test_business.id, staff_response.id)

    # ── Till open ──────────────────────────────────────────────────────────
    till = await open_till(body=TillOpenRequest(opening_float_cents=10000), user=staff_user, db=db)
    assert till.status == "OPEN"

    # ── Order A: walk-in, paid cash AT CHECKOUT (POS redesign, 2026-07-28) ──
    # then walked through the kitchen workflow. Burger defaults to
    # requires_preparation=True, so the order starts at ACCEPTED (not NEW) —
    # ringing it up already is the acceptance, per KNOWN_BUGS.md BUG-0035.
    order_a = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
            order_mode="PICKUP",
            customer_name="Regular Customer",
            customer_phone="0827654321",
            payment_method="CASH",
            cash_tendered_cents=8000,
        ),
        user=staff_user, db=db,
    )
    assert order_a.total_cents == 8000
    assert order_a.payment_status == "PAID"
    assert order_a.status == "ACCEPTED"
    assert order_a.change_due_cents == 0

    for next_status in ("IN_PROGRESS", "READY", "COLLECTED"):
        order_a = await update_order_status(
            order_id=order_a.id, body=StatusUpdateRequest(status=next_status), user=staff_user, db=db,
        )
    assert order_a.status == "COLLECTED"

    cust_result = await db.execute(select(Customer).where(Customer.phone_number == "0827654321"))
    customer = cust_result.scalar_one()
    await db.refresh(customer)
    assert customer.loyalty_points_balance == 80  # 8000 cents -> 80 Rand -> 80 points at 1/Rand

    # ── Order B: same customer, paid cash AT CHECKOUT, then REFUNDED via the
    # new till-native POS refund endpoint (still in the same till session) —
    # this is the exact scenario that exposed the cash-up reconciliation bug
    # (routes_pos.py's close_till previously didn't exclude refunded cash
    # orders from expected_cash_cents, and the old provider-based refund
    # endpoint had no way to reverse a till sale with no online provider).
    order_b = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
            order_mode="PICKUP",
            customer_name="Regular Customer",
            customer_phone="0827654321",
            payment_method="CASH",
            cash_tendered_cents=8000,
        ),
        user=staff_user, db=db,
    )
    assert order_b.payment_status == "PAID"

    refund_response = await refund_pos_order(
        order_id=order_b.id, body=POSRefundRequest(reason="Customer changed their mind"),
        user=owner, db=db,
    )
    assert refund_response.payment_status == "REFUNDED"

    # ── Loyalty redemption ────────────────────────────────────────────────
    await db.refresh(customer)
    assert customer.loyalty_points_balance == 160  # order B also earned 80 before being refunded
    redeem_response = await redeem_loyalty_reward(
        body=RedeemRequest(customer_id=customer.id, reward_id=reward.id), user=staff_user, db=db,
    )
    assert redeem_response.success is True
    assert redeem_response.balance_after == 110  # 160 - 50

    # ── Till close: only order A's cash (order B was refunded) should count ─
    closed_till = await close_till(
        session_id=till.id, body=TillCloseRequest(counted_cash_cents=18000), user=owner, db=db,
    )
    assert closed_till.status == "CLOSED"
    # expected = 10000 opening float + 8000 (order A only, order B refunded and excluded) = 18000
    assert closed_till.expected_cash_cents == 18000
    assert closed_till.variance_cents == 0

    # ── Reports (CSV export) ──────────────────────────────────────────────
    export_response = await export_orders_csv(days=30, status=None, user=owner, db=db)
    body_chunks = [chunk async for chunk in export_response.body_iterator]
    csv_text = b"".join(
        chunk if isinstance(chunk, bytes) else chunk.encode() for chunk in body_chunks
    ).decode()
    assert order_a.order_number in csv_text
    assert order_b.order_number in csv_text

    # ── Analytics ──────────────────────────────────────────────────────────
    summary = await get_summary(period="30d", user=owner, db=db)
    assert summary.total_orders >= 2
    assert summary.revenue_cents >= 8000  # only order A counts (COLLECTED); order B was never fulfilled
