"""
Workflow 1 (full customer lifecycle), driven against a REAL Postgres + Redis
instance — the first real integration test in this codebase's history.

Simulates the end-to-end commercial workflow named in the platform's own
launch requirements:

  configure business -> connect WhatsApp (Model 1: set phone_number_id)
  -> create menu -> create staff -> place first order (POS, walk-in
  customer by phone) -> process payment -> loyalty points awarded
  -> analytics reflects the order -> billing invoice recorded on a
  simulated provider webhook -> subscription/account remains active
  (get_current_user still grants access)

Every step calls the REAL route-handler function with a REAL AsyncSession
against the REAL local dev Postgres — not a mock, not a fake. This is
deliberately a much stronger form of verification than this repo's existing
~2,200 unit tests (all mocked, per TESTING.md) and directly answers "does
the whole workflow actually work," not just "does each function's logic
look right in isolation."

New-tenant public self-signup and the free-trial flow itself are not
exercised here (neither exists yet — see PRODUCT_ROADMAP.md/TODO.md); this
test starts from an already-onboarded business, which matches how every
real tenant is onboarded today (via the admin suite).
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.api.v1.routes_analytics import get_summary
from backend.app.api.v1.routes_business import BusinessSettingsUpdate, update_business_settings
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_orders import StatusUpdateRequest
from backend.app.api.v1.routes_orders import update_order_status
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest, POSOrderItemRequest, TillOpenRequest, create_pos_order, open_till,
)
from backend.app.api.v1.routes_staff import StaffCreate, create_staff
from backend.app.api.v1.routes_loyalty import LoyaltyProgramUpdate, update_loyalty_program
from backend.app.billing.stripe_webhooks import _handle_invoice_paid
from backend.app.core.rbac import AuthUser
from backend.app.utils.phone import normalize_sa_mobile
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.loyalty import LoyaltyTransaction
from shared.models.billing_invoice import BillingInvoice


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


def _staff(business_id: uuid.UUID, staff_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=staff_id, business_id=business_id, role="STAFF", token_type="access")


async def test_full_workflow_signup_to_active_subscription(db, test_business: Business):
    owner = _owner(test_business.id)

    # ── Step: Configure business ─────────────────────────────────────────
    settings_result = await update_business_settings(
        body=BusinessSettingsUpdate(
            name="Barn Owl Integration Test",
            payment_provider="MOCK",
            delivery_enabled=False,
        ),
        user=owner, db=db,
    )
    assert settings_result.payment_provider == "MOCK"
    assert settings_result.name == "Barn Owl Integration Test"

    # ── Step: Connect WhatsApp (Model 1 — set phone_number_id) ──────────
    result = await db.execute(select(Business).where(Business.id == test_business.id))
    biz = result.scalar_one()
    biz.whatsapp_phone_number_id = f"test-{uuid.uuid4().hex[:10]}"
    await db.commit()
    assert biz.whatsapp_phone_number_id is not None

    # ── Step: Enable Loyalty (so the walk-in POS customer earns points) ──
    await update_loyalty_program(
        body=LoyaltyProgramUpdate(is_enabled=True, points_per_rand=1, points_per_order=10),
        user=owner, db=db,
    )

    # ── Step: Create menu (category + item with a required size option) ─
    category = await create_category(
        body=CategoryCreate(name="Drinks", sort_order=0), user=owner, db=db,
    )
    item = await create_item(
        body=ItemCreate(
            category_id=category.id,
            name="Iced Coffee",
            price_cents=5000,
            options_json={
                "option_groups": [{
                    "id": "size", "name": "Size", "required": True,
                    "min_selections": 1, "max_selections": 1,
                    "options": [
                        {"id": "medium", "name": "Medium", "price_delta_cents": -1000},
                        {"id": "mkhulu", "name": "MKhulu", "price_delta_cents": 1500},
                    ],
                }],
            },
        ),
        user=owner, db=db,
    )
    assert item.price_cents == 5000

    # ── Step: Create staff ────────────────────────────────────────────────
    staff_response = await create_staff(
        body=StaffCreate(staff_name="Test Cashier", role="STAFF"),
        user=owner, db=db,
    )
    assert staff_response.initial_pin is not None
    staff_user = _staff(test_business.id, staff_response.id)

    # ── Step: Place first order (POS, walk-in customer by phone) ────────
    # POS checkout now pays at the till atomically (BUG-0035, 2026-07-28) —
    # requires an open till session, and the order is created already PAID.
    await open_till(body=TillOpenRequest(opening_float_cents=0), user=staff_user, db=db)
    order = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(
                menu_item_id=item.id, quantity=2,
                selected_option_ids={"size": "mkhulu"},
            )],
            order_mode="PICKUP",
            customer_name="Walk-in Test Customer",
            customer_phone="0821234567",
            payment_method="CASH",
            cash_tendered_cents=13000,
        ),
        user=staff_user, db=db,
    )
    # base 5000 + option +1500 = 6500/unit * 2 = 13000
    assert order.subtotal_cents == 13000
    assert order.source == "POS"
    assert order.total_cents == 13000
    assert order.payment_status == "PAID"
    # This item's options_json carries no requires_preparation override, so
    # it defaults True -> the order starts at ACCEPTED, not NEW (ringing it
    # up at the till already is the acceptance).
    assert order.status == "ACCEPTED"

    # Confirm a Customer was created and linked (loyalty walk-in identity).
    # BUG-0098/BUG-0100 (KNOWN_BUGS.md): the loyalty customer-lookup path now
    # normalises the phone number (normalize_sa_mobile) before storing it —
    # a stale raw-string lookup would no longer find the row the checkout
    # above just created.
    cust_result = await db.execute(
        select(Customer).where(Customer.phone_number == normalize_sa_mobile("0821234567"))
    )
    customer = cust_result.scalar_one()

    # ── Step: Loyalty points awarded (at checkout, not at a later payment
    # step — the order was already PAID at creation) ────────────────────
    await db.refresh(customer)
    # 130 Rand spent * 1 point/Rand + 10 flat per order = 140
    assert customer.loyalty_points_balance == 140
    txn_result = await db.execute(select(LoyaltyTransaction).where(LoyaltyTransaction.customer_id == customer.id))
    assert txn_result.scalar_one_or_none() is not None

    # ── Step: Kitchen/counter fulfils the order ──────────────────────────
    # Revenue is recognised on fulfilment (status COLLECTED/DELIVERED), not
    # on payment alone — see routes_analytics.py's revenue_cents CASE.
    for next_status in ("IN_PROGRESS", "READY", "COLLECTED"):
        order = await update_order_status(
            order_id=order.id,
            body=StatusUpdateRequest(status=next_status),
            user=staff_user, db=db,
        )
    assert order.status == "COLLECTED"

    # ── Step: Analytics reflects the order ───────────────────────────────
    summary = await get_summary(period="30d", user=owner, db=db)
    assert summary.revenue_cents >= 13000
    assert summary.total_orders >= 1

    # ── Step: Receive invoice (simulated billing-provider webhook) ───────
    biz.stripe_customer_id = f"cus_test_{uuid.uuid4().hex[:10]}"
    await db.commit()
    await _handle_invoice_paid(db, {"customer": biz.stripe_customer_id, "amount_paid": 120000, "id": "in_test_123"})

    invoice_result = await db.execute(select(BillingInvoice).where(BillingInvoice.business_id == test_business.id))
    invoice = invoice_result.scalar_one_or_none()
    assert invoice is not None
    assert invoice.status == "PAID"
    assert invoice.amount_cents == 120000

    # ── Step: Subscription/account remains active ────────────────────────
    # get_current_user() (backend/app/core/rbac.py) checks exactly these two
    # fields on every request (unit-tested directly in
    # test_rbac_business_suspension.py) — this integration test confirms the
    # real business row, after a real end-to-end workflow, is in the state
    # that check actually requires for continued access.
    await db.refresh(biz)
    assert biz.is_active is True
    assert biz.billing_status == "ACTIVE"
