"""
Release-gate proof (Part 3 of the release-gate brief): one clean test
restaurant taken through the full commercial workflow -- onboarding through
paid conversion -- against a REAL Postgres, asserting real numbers at every
step rather than only HTTP-200-shaped success.

NEVER FAKES A THIRD-PARTY SUCCESS
----------------------------------
No real Meta (WhatsApp) or Uber Eats call is made or can be made in this
environment -- there are no credentials and there must be none. The digital-
receipt step below proves the SAFE-FAILURE path instead (the Meta HTTP
boundary is mocked to return a realistic "no credentials" failure, same
pattern as tests/integration/test_digital_receipts_realdb.py), and delivery
is exercised only via CSV import/reconciliation (a local file parse, no
third-party network call of any kind -- the Uber/Mr D DIRECT-API integration
is out of scope here, see docs/COMMERCIAL_FEATURE_MATRIX_2026-10.md).

Requires TEST_DATABASE_URL (see docker-compose.test.yml / TESTING.md).
"""
import io
import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select
from starlette.datastructures import UploadFile

from admin_api.app.api.v1.routes_businesses import (
    BusinessCreate,
    OwnerCreateRequest,
    BusinessUpdate,
    create_business,
    create_business_owner,
    start_business_trial,
    update_business,
)
from admin_api.app.api.v1.routes_commercial import (
    InvoiceDraftCreate,
    InvoiceLineIn,
    MarkPaidRequest,
    create_invoice_draft,
    issue_invoice_route,
    mark_invoice_paid_route,
)
from backend.app.api.v1.routes_analytics import (
    get_average_order_value,
    get_payment_method_breakdown,
    get_summary,
)
from backend.app.api.v1.routes_external_orders import channel_breakdown, commit_external_orders
from backend.app.api.v1.routes_inventory import (
    IngredientCreateRequest,
    OpeningStockRequest,
    SupplierCreateRequest,
    create_ingredient,
    create_supplier,
    get_stock_levels,
    set_opening_stock,
)
from backend.app.api.v1.routes_loyalty import LoyaltyProgramUpdate, LoyaltyRewardCreate, create_reward, update_loyalty_program
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest,
    POSOrderItemRequest,
    POSReceiptRequest,
    TillCloseRequest,
    TillOpenRequest,
    close_till,
    create_pos_order,
    open_till,
    request_pos_receipt,
)
from backend.app.api.v1.routes_recipes import RecipeItemRequest, RecipeUpsertRequest, upsert_recipe
from backend.app.api.v1.routes_staff import StaffCreate, create_staff
from backend.app.api.v1.routes_stocktake import (
    CountLineRequest,
    CountSubmitRequest,
    StocktakeOpenRequest,
    approve_stocktake,
    complete_stocktake,
    open_stocktake,
    submit_counts,
)
from backend.app.api.v1.routes_wastage import WastageCreateRequest, approve_wastage, record_wastage
from backend.app.bot.meta_errors import MetaErrorInfo
from backend.app.core.config import get_settings
from backend.app.core.rbac import AuthUser
from backend.app.core.security import hash_password
from shared.models.admin import AdminRole, AdminUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.order import Order


def _admin_auth(admin_id, role):
    return AuthUser(user_id=admin_id, business_id=None, role=role, token_type="admin_access")


def _business_auth(user_id, business_id, role):
    return AuthUser(user_id=user_id, business_id=business_id, role=role, token_type="access")


async def _make_admin(db, role, email):
    admin = AdminUser(
        email=email, password_hash=hash_password("Irrelevant123!"),
        display_name="Release Gate Admin", role=role, is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


@pytest.fixture
def _invoicing_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "NEXTGEN_LEGAL_NAME", "NextGen Intelligence (Pty) Ltd")
    monkeypatch.setattr(settings, "NEXTGEN_ADDRESS", "1 Test Street, Durban")
    monkeypatch.setattr(settings, "NEXTGEN_CONTACT_EMAIL", "billing@nextgen.test")
    monkeypatch.setattr(settings, "NEXTGEN_BANK_NAME", "Test Bank")
    monkeypatch.setattr(settings, "NEXTGEN_BANK_ACCOUNT_NAME", "NextGen Intelligence")
    monkeypatch.setattr(settings, "NEXTGEN_BANK_ACCOUNT_NUMBER", "123456789")
    monkeypatch.setattr(settings, "NEXTGEN_BANK_BRANCH_CODE", "123456")
    monkeypatch.setattr(settings, "NEXTGEN_VAT_REGISTERED", False)
    return settings


async def test_full_commercial_e2e_release_gate(db, _invoicing_configured):
    unique = uuid.uuid4().hex[:8]

    # ══ ONBOARDER creates restaurant ══════════════════════════════════════
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-e2e-{unique}@test.co.za")
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-e2e-{unique}@test.co.za")
    onb_auth = _admin_auth(onboarder.id, AdminRole.ONBOARDER)
    super_auth = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)

    created = await create_business(
        body=BusinessCreate(name=f"E2E Release Gate Bistro {unique}", start_trial=True),
        user=onb_auth, db=db,
    )
    business_id = created.id
    biz = await db.get(Business, business_id)
    assert biz.billing_status == "TRIAL"

    # ══ Trial starts ═══════════════════════════════════════════════════════
    trial_result = await start_business_trial(business_id=business_id, user=onb_auth, db=db)
    assert trial_result.trial_ends_at is not None

    # ══ Modules set — NextGen Complete (all modules) ══════════════════════
    # ONBOARDER may set inventory/whatsapp/digital-receipts; delivery and
    # advanced-analytics are deliberately SUPER_ADMIN-only (see
    # admin_api/app/api/v1/routes_businesses.py's ONBOARDER_PATCH_ALLOWED_FIELDS
    # docstring) -- a real Complete-tier sale genuinely needs both roles.
    whatsapp_phone_number_id = f"e2e-wa-{unique}"
    await update_business(
        business_id=business_id,
        body=BusinessUpdate(
            inventory_enabled=True, is_whatsapp_enabled=True, digital_receipts_enabled=True,
            whatsapp_phone_number_id=whatsapp_phone_number_id,
        ),
        user=onb_auth, db=db,
    )
    await update_business(
        business_id=business_id,
        body=BusinessUpdate(
            delivery_platforms_enabled=True, advanced_analytics_enabled=True,
            # migration 0051, KNOWN_BUGS.md BUG-0107 — Loyalty is a NextGen
            # Complete-tier module like the other two SUPER_ADMIN-only flags
            # above; this business exercises loyalty program config +
            # checkout redemption further down, so it must be entitled.
            loyalty_enabled=True,
        ),
        user=super_auth, db=db,
    )
    biz = await db.get(Business, business_id)
    assert biz.inventory_enabled is True
    assert biz.is_whatsapp_enabled is True
    assert biz.digital_receipts_enabled is True
    assert biz.delivery_platforms_enabled is True
    assert biz.advanced_analytics_enabled is True
    assert biz.loyalty_enabled is True

    # ══ Owner created ══════════════════════════════════════════════════════
    owner_login = await create_business_owner(
        business_id=business_id,
        body=OwnerCreateRequest(email=f"owner-e2e-{unique}@test.co.za", full_name="E2E Owner", role="OWNER"),
        user=onb_auth, db=db,
    )
    owner = _business_auth(owner_login.id, business_id, "OWNER")

    # ══ Menu ═══════════════════════════════════════════════════════════════
    category = await create_category(body=CategoryCreate(name="Mains", sort_order=0), user=owner, db=db)
    burger = await create_item(
        body=ItemCreate(category_id=category.id, name="Burger", price_cents=8000), user=owner, db=db,
    )

    # ══ Staff + PINs ═══════════════════════════════════════════════════════
    staff_response = await create_staff(body=StaffCreate(staff_name="E2E Cashier", role="STAFF"), user=owner, db=db)
    assert staff_response.initial_pin is not None and len(staff_response.initial_pin) == 4
    staff = _business_auth(staff_response.id, business_id, "STAFF")

    # ══ Till opened ════════════════════════════════════════════════════════
    till = await open_till(body=TillOpenRequest(opening_float_cents=0), user=staff, db=db)
    assert till.status == "OPEN"

    # ══ Supplier created ═══════════════════════════════════════════════════
    supplier = await create_supplier(body=SupplierCreateRequest(name="E2E Bun Supplier"), user=owner, db=db)
    assert supplier.id is not None

    # ══ Ingredients (created before opening stock, which requires one) ═════
    bun = await create_ingredient(
        body=IngredientCreateRequest(
            name="Burger Bun", canonical_unit="UNIT",
            purchase_pack_quantity="1", purchase_pack_unit="UNIT", cost_per_pack_cents=200,
        ),
        user=owner, db=db,
    )

    # ══ Opening stock posted ═══════════════════════════════════════════════
    opening = await set_opening_stock(
        body=OpeningStockRequest(ingredient_id=bun.id, quantity="100", unit="UNIT", source="E2E go-live count"),
        user=owner, db=db,
    )
    assert opening.quantity_milli == 100_000  # 100 units

    # ══ Recipes ════════════════════════════════════════════════════════════
    recipe = await upsert_recipe(
        body=RecipeUpsertRequest(
            menu_item_id=burger.id, yield_qty=1,
            items=[RecipeItemRequest(ingredient_id=bun.id, quantity="1", unit="UNIT")],
        ),
        user=owner, db=db,
    )
    assert recipe.items[0].quantity_milli == 1_000  # 1 unit per portion

    # ══ Loyalty programme enabled BEFORE the sale that should earn points ══
    await update_loyalty_program(
        body=LoyaltyProgramUpdate(is_enabled=True, points_per_rand=1, points_per_order=10, min_redemption_points=0),
        user=owner, db=db,
    )
    reward = await create_reward(
        body=LoyaltyRewardCreate(name="R10 Off", points_cost=100, value_cents=1000), user=owner, db=db,
    )

    # ══ POS sale #1 (3x Burger) ═══════════════════════════════════════════
    order_1 = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=burger.id, quantity=3)],
            payment_method="CASH", cash_tendered_cents=24000,
            customer_phone="0821110001", customer_name="E2E Loyal Customer",
            idempotency_key=f"e2e-sale1-{unique}",
        ),
        user=staff, db=db,
    )
    assert order_1.subtotal_cents == 24000
    assert order_1.total_cents == 24000
    assert order_1.payment_status == "PAID"

    # ══ Stock deducted by the correct recipe quantities ═══════════════════
    # 3 portions x 1 unit/portion = 3 units = 3000 milli consumed.
    stock = await get_stock_levels(below_par_only=False, as_of=None, user=owner, db=db)
    bun_level = next(s for s in stock if s.ingredient_id == bun.id)
    assert bun_level.on_hand_milli == 97_000, (
        f"expected 100,000 - 3,000 (3 burgers x 1 unit) = 97,000 milli, got {bun_level.on_hand_milli}"
    )

    # ══ Customer loyalty — points earned ═══════════════════════════════════
    cust_result = await db.execute(select(Customer).where(Customer.business_id == business_id))
    customer = cust_result.scalar_one()
    await db.refresh(customer)
    assert customer.loyalty_points_balance == 250, (
        f"expected (24000//100)*1 + 10 = 250 points, got {customer.loyalty_points_balance}"
    )

    # ══ POS sale #2 — reward redeemed, discount actually applied ══════════
    order_2 = await create_pos_order(
        body=POSOrderCreateRequest(
            items=[POSOrderItemRequest(menu_item_id=burger.id, quantity=1)],
            payment_method="CASH", cash_tendered_cents=7000,
            customer_phone="0821110001",
            loyalty_reward_id=reward.id,
            idempotency_key=f"e2e-sale2-{unique}",
        ),
        user=staff, db=db,
    )
    assert order_2.subtotal_cents == 8000
    assert order_2.loyalty_discount_cents == 1000
    assert order_2.total_cents == 7000, "the R10 reward discount must actually reduce the order total"

    await db.refresh(customer)
    # 250 - 100 (redeemed) + ((7000//100)*1 + 10) (earned on the discounted total) = 230
    assert customer.loyalty_points_balance == 230, (
        f"expected 250 - 100 + 80 = 230, got {customer.loyalty_points_balance}"
    )

    stock = await get_stock_levels(below_par_only=False, as_of=None, user=owner, db=db)
    bun_level = next(s for s in stock if s.ingredient_id == bun.id)
    assert bun_level.on_hand_milli == 96_000  # 97,000 - 1,000 (1 more burger)

    # ══ Receipt (printed) ══════════════════════════════════════════════════
    printed_receipt = await request_pos_receipt(
        order_id=order_1.id, body=POSReceiptRequest(channels=["PRINT"]), user=staff, db=db,
    )
    assert printed_receipt.print_requested is True

    # ══ Digital receipt — SAFE-FAILURE proof, no real Meta call ═══════════
    # No live Meta/WhatsApp credentials exist in this environment and none
    # may be used. The HTTP boundary is mocked to return a realistic
    # "no credentials" failure (never a faked success) so the DB-side
    # correctness of the attempt/failure path is still genuinely proven.
    no_creds_failure = AsyncMock(
        return_value=(
            None,
            MetaErrorInfo(
                status_code=401, code=None, subcode=None,
                message="no live Meta credentials in this test environment",
                details=None, retryable=False,
                last_error="TEST: no live Meta credentials in this test environment",
            ),
        )
    )
    with patch("backend.app.bot.whatsapp_sender.send_template_message", no_creds_failure):
        digital_receipt = await request_pos_receipt(
            order_id=order_1.id, body=POSReceiptRequest(channels=["WHATSAPP"]), user=staff, db=db,
        )
    assert no_creds_failure.called, "the digital-receipt send must genuinely be ATTEMPTED, not skipped"
    assert digital_receipt.whatsapp_status == "FAILED"  # real attempt, honestly-mocked failure — never a faked SENT

    # ══ Wastage recorded ═══════════════════════════════════════════════════
    biz = await db.get(Business, business_id)
    biz.wastage_approval_threshold_cents = 500  # 5 units x R2 = R10 > R5 threshold, forces the approval path
    await db.commit()
    wastage = await record_wastage(
        body=WastageCreateRequest(ingredient_id=bun.id, quantity="5", unit="UNIT", reason="DROPPED"),
        user=staff, db=db,
    )
    assert wastage.approved_by_user_id is None  # STAFF entry above threshold: pending, not rejected

    stock = await get_stock_levels(below_par_only=False, as_of=None, user=owner, db=db)
    bun_level = next(s for s in stock if s.ingredient_id == bun.id)
    assert bun_level.on_hand_milli == 91_000, "wastage posts its stock movement immediately, pending approval or not"

    # ══ Wastage approved ═══════════════════════════════════════════════════
    approved_wastage = await approve_wastage(entry_id=wastage.id, user=owner, db=db)
    assert approved_wastage.approved_by_user_id == owner.user_id

    # ══ Delivery CSV import (GENERIC platform — no live Uber/Mr D call) ════
    csv_body = (
        "Order ID,Order Date,Item,Quantity,Price\r\n"
        f"E2E-CSV-{unique},2026-09-20 12:00,Burger,2,160.00\r\n"
    ).encode()
    upload = UploadFile(io.BytesIO(csv_body), filename="e2e_import.csv")
    commit_result = await commit_external_orders(
        file=upload, platform="GENERIC", mapping=None,
        include_cancelled=False, acknowledge_closed_period=False,
        user=owner, db=db,
    )
    assert commit_result["orders_imported"] == 1
    assert commit_result["gross_sales_cents"] == 16000

    imported_order = (
        await db.execute(
            select(Order).where(Order.business_id == business_id, Order.source != "POS")
        )
    ).scalar_one()
    assert imported_order.total_cents == 16000
    assert imported_order.payment_status == "PAID"

    stock = await get_stock_levels(below_par_only=False, as_of=None, user=owner, db=db)
    bun_level = next(s for s in stock if s.ingredient_id == bun.id)
    assert bun_level.on_hand_milli == 89_000, (
        f"expected 91,000 - 2,000 (2 imported burgers x 1 unit) = 89,000 milli, got {bun_level.on_hand_milli}"
    )

    # ══ Stocktake with a variance ══════════════════════════════════════════
    stocktake = await open_stocktake(body=StocktakeOpenRequest(reference="E2E Count"), user=staff, db=db)
    assert stocktake.status == "DRAFT"
    stocktake = await submit_counts(
        session_id=stocktake.id,
        body=CountSubmitRequest(lines=[CountLineRequest(ingredient_id=bun.id, quantity="87", unit="UNIT")]),
        user=staff, db=db,
    )
    stocktake = await complete_stocktake(session_id=stocktake.id, user=owner, db=db)
    assert stocktake.status == "COMPLETED"
    bun_line = next(l for l in stocktake.lines if l.ingredient_id == bun.id)
    assert bun_line.expected_quantity_milli == 89_000
    assert bun_line.counted_quantity_milli == 87_000
    assert bun_line.variance_quantity_milli == -2_000, "2 units short (87 counted vs 89 expected)"

    stocktake = await approve_stocktake(session_id=stocktake.id, user=owner, db=db)
    assert stocktake.status == "APPROVED"
    stock = await get_stock_levels(below_par_only=False, as_of=None, user=owner, db=db)
    bun_level = next(s for s in stock if s.ingredient_id == bun.id)
    assert bun_level.on_hand_milli == 87_000, "approval must post the adjustment so book stock matches the count"

    # ══ Cash-up ═════════════════════════════════════════════════════════════
    closed_till = await close_till(
        session_id=till.id, body=TillCloseRequest(counted_cash_cents=31000), user=owner, db=db,
    )
    assert closed_till.status == "CLOSED"
    # opening float 0 + sale1 cash 24000 + sale2 cash 7000 = 31000
    # (the CSV-imported order carries no till_session_id, so it is correctly
    # excluded from cash-up — see external_orders/service.py's own comment)
    assert closed_till.expected_cash_cents == 31000
    assert closed_till.variance_cents == 0

    # ══ Invoice ═════════════════════════════════════════════════════════════
    draft = await create_invoice_draft(
        body=InvoiceDraftCreate(
            business_id=business_id,
            lines=[InvoiceLineIn(kind="SUBSCRIPTION", description="NextGen Complete — monthly", quantity=1, unit_amount_cents=249900)],
        ),
        user=onb_auth, db=db,
    )
    issued = await issue_invoice_route(invoice_id=draft.id, user=onb_auth, db=db)
    assert issued.status == "AWAITING_PAYMENT"
    assert issued.total_cents == 249900

    # ══ EFT payment ═════════════════════════════════════════════════════════
    paid = await mark_invoice_paid_route(
        invoice_id=draft.id,
        body=MarkPaidRequest(payment_date=date.today(), payment_method="EFT", reference=f"E2E-EFT-{unique}"),
        user=onb_auth, db=db,
    )
    assert paid.status == "PAID"

    # ══ Trial converts to paid ══════════════════════════════════════════════
    biz = await db.get(Business, business_id)
    assert biz.billing_status == "ACTIVE"
    assert biz.is_active is True
    assert biz.converted_at is not None

    # ══ Analytics reconcile arithmetically against the raw orders ══════════
    # Every sale in this business is PAID immediately (POS + CSV import), so
    # gross revenue is exactly the sum of the three orders' own total_cents —
    # computed independently here from the numbers already asserted above,
    # not re-derived from the analytics endpoint itself.
    expected_gross_cents = order_1.total_cents + order_2.total_cents + imported_order.total_cents
    assert expected_gross_cents == 24000 + 7000 + 16000 == 47000

    summary = await get_summary(period="30d", user=owner, db=db)
    assert summary.total_orders == 3
    assert summary.gross_cents == expected_gross_cents
    assert summary.revenue_cents == expected_gross_cents  # no refunds/voids in this workflow -> net == gross

    aov = await get_average_order_value(period="30d", user=owner, db=db)
    # AOV = net_cents / completed_orders, same IS_SALE-derived base as /summary.
    assert aov.average_order_value_cents == round(expected_gross_cents / 3)

    payment_methods = await get_payment_method_breakdown(days=30, user=owner, db=db)
    cash_bucket = next(p for p in payment_methods.methods if p.payment_method == "CASH")
    assert cash_bucket.gross_cents == order_1.total_cents + order_2.total_cents == 31000
    external_bucket = next(p for p in payment_methods.methods if p.payment_method == "EXTERNAL_PLATFORM")
    assert external_bucket.gross_cents == imported_order.total_cents == 16000

    channels = await channel_breakdown(days=30, user=owner, db=db)
    channels_by_source = {c["source"]: c for c in channels["channels"]}
    # POS channel must show exactly the two till sales; the imported CSV
    # order lands under OTHER_EXTERNAL (GENERIC platform's mapped source,
    # shared/enums.py PLATFORM_TO_ORDER_SOURCE) -- arithmetic reconciliation
    # against the same raw Order rows, via a completely different report.
    assert channels_by_source["POS"]["gross_cents"] == 31000
    assert channels_by_source["POS"]["order_count"] == 2
    assert channels_by_source["OTHER_EXTERNAL"]["gross_cents"] == 16000
    assert channels_by_source["OTHER_EXTERNAL"]["order_count"] == 1
    assert channels["total_gross_cents"] == expected_gross_cents

    # ══ Cleanup ═════════════════════════════════════════════════════════════
    # IDs captured as plain values BEFORE rollback -- db.rollback() expires
    # every ORM instance in this session's identity map (see
    # backend/app/receipts/whatsapp_receipt.py's own docstring on exactly
    # this), so a later `onboarder.id` attribute access would need a fresh
    # lazy-load that cannot happen synchronously outside the SQLAlchemy
    # greenlet context.
    onboarder_id, super_admin_id = onboarder.id, super_admin.id
    await db.rollback()
    from shared.models.admin import AdminUser as _AdminUser
    from shared.models.commercial_invoice import CommercialInvoice, CommercialInvoiceLine

    await db.execute(delete(CommercialInvoiceLine).where(
        CommercialInvoiceLine.invoice_id.in_(
            select(CommercialInvoice.id).where(CommercialInvoice.business_id == business_id)
        )
    ))
    await db.execute(delete(CommercialInvoice).where(CommercialInvoice.business_id == business_id))
    await db.execute(delete(AuditEvent).where(AuditEvent.business_id == business_id))
    await db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id.in_([onboarder_id, super_admin_id])))
    await db.execute(delete(Business).where(Business.id == business_id))
    await db.execute(delete(_AdminUser).where(_AdminUser.id.in_([onboarder_id, super_admin_id])))
    await db.commit()
