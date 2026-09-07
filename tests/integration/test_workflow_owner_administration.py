"""
Workflow 3 (Restaurant Owner Administration), driven against a REAL Postgres
+ Redis instance — the third real-DB integration test in this codebase's
history (see test_workflow_full_lifecycle.py / test_workflow_daily_operations.py
for the first two).

Covers: business settings (hours/profile/payment config) -> menu management
(categories, items, option groups, add-ons, attach/detach) -> specials CRUD
-> staff (manager creation, PIN rotation, deactivation) -> loyalty (reward
catalogue CRUD + redemption) -> reports (CSV export) -> analytics -> billing
(status + invoice history).

Explicitly NOT exercised here (documented, not silently skipped):
- `POST /business/billing/checkout-session`, `/portal-session`, `/cancel` —
  every registered `BillingProvider` (STRIPE, PAYFAST) makes a real external
  HTTP call to a real payment platform using real credentials. There is no
  MOCK billing provider (unlike the customer-payment Payment Provider
  Framework, which has one specifically for this reason). Exercising these
  would require live Stripe/PayFast test credentials, which this environment
  does not have — a genuine external dependency, not an engineering gap.
  `GET /business/billing/status` and `GET /business/billing/invoices` (both
  pure DB reads, no external call) ARE exercised below.
"""
import uuid

import pytest
from sqlalchemy import select

from backend.app.api.v1.routes_billing import get_billing_status, list_invoices
from backend.app.api.v1.routes_business import BusinessSettingsUpdate, update_business_settings
from backend.app.api.v1.routes_export import export_orders_csv
from backend.app.api.v1.routes_loyalty import (
    LoyaltyRewardCreate,
    LoyaltyRewardUpdate,
    RedeemRequest,
    create_reward,
    list_rewards,
    redeem_loyalty_reward,
    update_loyalty_program,
    update_reward,
    LoyaltyProgramUpdate,
)
from backend.app.api.v1.routes_menu import (
    AddOnCreate,
    CategoryCreate,
    CategoryUpdate,
    ItemCreate,
    ItemUpdate,
    attach_add_on,
    create_add_on,
    create_category,
    create_item,
    delete_category,
    update_category,
    update_item,
)
from backend.app.api.v1.routes_orders import StatusUpdateRequest, update_order_status
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest, POSOrderItemRequest, TillOpenRequest, create_pos_order, open_till,
)
from backend.app.api.v1.routes_specials import SpecialCreate, SpecialUpdate, create_special, delete_special, update_special
from backend.app.api.v1.routes_staff import PinRotation, StaffCreate, StaffUpdate, auto_rotate_pin, create_staff, rotate_pin, update_staff
from backend.app.api.v1.routes_analytics import get_summary
from backend.app.core.rbac import AuthUser
from shared.models.business import Business
from shared.models.billing_invoice import BillingInvoice
from shared.models.customer import Customer


def _owner(business_id: uuid.UUID) -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="OWNER", token_type="access")


async def test_owner_administration_workflow(db, test_business: Business):
    owner = _owner(test_business.id)

    # ── Business settings: profile, hours, payment config ────────────────
    settings_result = await update_business_settings(
        body=BusinessSettingsUpdate(
            name="Barn Owl Cafe",
            address="123 Main Road, Cape Town",
            phone="0211234567",
            business_hours={"mon": {"open": "07:00", "close": "17:00"}, "sun": None},
            payment_provider="MOCK",
            eft_bank_name="Test Bank",
            eft_account_number="1234567890",
        ),
        user=owner, db=db,
    )
    assert settings_result.name == "Barn Owl Cafe"
    assert settings_result.payment_provider == "MOCK"

    # ── Menu management: category + item + option group + add-on ─────────
    category = await create_category(body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=db)
    category = await update_category(
        category_id=category.id, body=CategoryUpdate(name="Mains & Grills"), user=owner, db=db,
    )
    assert category.name == "Mains & Grills"

    item = await create_item(
        body=ItemCreate(
            category_id=category.id, name="Steak", price_cents=15000,
            options_json={
                "option_groups": [{
                    "id": "doneness", "name": "Doneness", "required": True,
                    "min_selections": 1, "max_selections": 1,
                    "options": [
                        {"id": "medium", "name": "Medium", "price_delta_cents": 0},
                        {"id": "well_done", "name": "Well Done", "price_delta_cents": 0},
                    ],
                }],
            },
        ),
        user=owner, db=db,
    )
    item = await update_item(item_id=item.id, body=ItemUpdate(price_cents=16000), user=owner, db=db)
    assert item.price_cents == 16000

    add_on = await create_add_on(
        body=AddOnCreate(name="Extra Sauce", price_cents=2000), user=owner, db=db,
    )
    await attach_add_on(item_id=item.id, add_on_id=add_on.id, user=owner, db=db)

    # Category delete is blocked while it still has an active item — confirms
    # the existing safety check actually holds against a real FK/DB state,
    # not just the mocked unit tests that already cover this in isolation.
    with pytest.raises(Exception):
        await delete_category(category_id=category.id, user=owner, db=db)

    # ── Specials CRUD ──────────────────────────────────────────────────────
    special = await create_special(
        body=SpecialCreate(title="Tuesday Steak Night", days_of_week=["tue"]), user=owner, db=db,
    )
    special = await update_special(
        special_id=special.id, body=SpecialUpdate(title="Tuesday Steak Special"), user=owner, db=db,
    )
    assert special.title == "Tuesday Steak Special"
    await delete_special(special_id=special.id, user=owner, db=db)

    # ── Staff: manager creation, PIN rotation, deactivation ────────────────
    manager_response = await create_staff(
        body=StaffCreate(staff_name="Assistant Manager", role="MANAGER", email="manager@barnowlcafe.co.za", password="correct-horse-battery"),
        user=owner, db=db,
    )
    assert manager_response.role == "MANAGER"

    staff_response = await create_staff(body=StaffCreate(staff_name="Cashier", role="STAFF"), user=owner, db=db)

    rotate_result = await rotate_pin(
        staff_id=staff_response.id, body=PinRotation(new_pin="9999"), user=owner, db=db,
    )
    assert rotate_result.pin == "9999"

    auto_rotate_result = await auto_rotate_pin(staff_id=staff_response.id, user=owner, db=db)
    assert len(auto_rotate_result.pin) == 4
    assert auto_rotate_result.pin != "9999"

    deactivated = await update_staff(
        staff_id=manager_response.id, body=StaffUpdate(is_active=False), user=owner, db=db,
    )
    assert deactivated.is_active is False

    # ── Loyalty: reward catalogue CRUD + redemption ────────────────────────
    await update_loyalty_program(
        body=LoyaltyProgramUpdate(is_enabled=True, points_per_rand=1, points_per_order=0, min_redemption_points=0),
        user=owner, db=db,
    )
    reward = await create_reward(body=LoyaltyRewardCreate(name="Free Coffee", points_cost=30), user=owner, db=db)
    rewards = await list_rewards(user=owner, db=db)
    assert any(r.id == reward.id for r in rewards)
    reward = await update_reward(reward_id=reward.id, body=LoyaltyRewardUpdate(points_cost=40), user=owner, db=db)
    assert reward.points_cost == 40

    staff_user = AuthUser(user_id=staff_response.id, business_id=test_business.id, role="STAFF", token_type="access")
    await open_till(body=TillOpenRequest(opening_float_cents=0), user=staff_user, db=db)
    # POS checkout now pays at the till (BUG-0035) — the Steak item defaults
    # to requires_preparation=True, so the order starts at ACCEPTED, not NEW.
    order = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1, selected_option_ids={"doneness": "medium"})],
            order_mode="PICKUP", customer_name="Regular", customer_phone="0839998888",
            payment_method="CASH", cash_tendered_cents=16000,
        ),
        user=staff_user, db=db,
    )
    assert order.payment_status == "PAID"
    assert order.status == "ACCEPTED"
    for next_status in ("IN_PROGRESS", "READY", "COLLECTED"):
        await update_order_status(order_id=order.id, body=StatusUpdateRequest(status=next_status), user=staff_user, db=db)

    cust_result = await db.execute(select(Customer).where(Customer.phone_number == "0839998888"))
    customer = cust_result.scalar_one()
    assert customer.loyalty_points_balance == 160  # 16000 cents item -> 160 Rand -> 160 points

    redeem_response = await redeem_loyalty_reward(
        body=RedeemRequest(customer_id=customer.id, reward_id=reward.id), user=staff_user, db=db,
    )
    assert redeem_response.success is True
    assert redeem_response.balance_after == 120  # 160 - 40

    # ── Reports (CSV export) ──────────────────────────────────────────────
    export_response = await export_orders_csv(days=30, status="COLLECTED", user=owner, db=db)
    body_chunks = [chunk async for chunk in export_response.body_iterator]
    csv_text = b"".join(c if isinstance(c, bytes) else c.encode() for c in body_chunks).decode()
    assert order.order_number in csv_text

    # ── Analytics ──────────────────────────────────────────────────────────
    summary = await get_summary(period="30d", user=owner, db=db)
    assert summary.total_orders >= 1
    assert summary.revenue_cents >= 16000

    # ── Billing (DB-read-only surface — see module docstring for what's ───
    # deliberately not exercised: checkout/portal/cancel need real provider
    # credentials this environment doesn't have).
    billing_status = await get_billing_status(user=owner, db=db)
    assert billing_status.billing_status == "TRIAL"  # fresh test_business, never subscribed

    db.add(BillingInvoice(
        business_id=test_business.id, provider="STRIPE", provider_reference="in_test_owner_wf",
        amount_cents=120000, status="PAID", plan="STARTER",
    ))
    await db.commit()
    invoices = await list_invoices(user=owner, db=db)
    assert len(invoices) == 1
    assert invoices[0].amount_cents == 120000
    assert invoices[0].status == "PAID"
