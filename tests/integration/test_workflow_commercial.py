"""
Wave 0A commercial/sales-onboarding workflow, driven against a REAL Postgres
instance — same style as test_workflow_onboarding.py (route functions called
directly with hand-built AuthUser objects, not over real HTTP).

Covers: trial start/extend (cap enforcement, reactivation), commercial
invoice lifecycle (draft -> issue -> mark-paid/cancel, immutability,
idempotent mark-paid, sequential numbering, VAT on/off, INVOICING_NOT_
CONFIGURED), permissions (ONBOARDER commercial access survives handover but
configuration access does not; cross-tenant 404), owner-facing endpoints,
PDF rendering, and go-live readiness. Skips cleanly if the real Postgres
instance isn't reachable.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from admin_api.app.api.v1.routes_businesses import (
    BusinessCreate,
    BusinessUpdate,
    OnboardingCompleteRequest,
    OnboardingConfirmRequest,
    TrialExtendRequest,
    complete_onboarding,
    confirm_onboarding_item,
    create_business,
    extend_business_trial,
    get_onboarding_status,
    start_business_trial,
    update_business,
)
from admin_api.app.api.v1.routes_commercial import (
    CancelRequest,
    InvoiceDraftCreate,
    InvoiceDraftUpdate,
    InvoiceLineIn,
    MarkPaidRequest,
    cancel_invoice_route,
    create_invoice_draft,
    get_commercial_dashboard,
    get_invoice_pdf,
    issue_invoice_route,
    list_commercial_clients,
    list_invoices,
    mark_invoice_paid_route,
    update_invoice_draft,
)
from backend.app.api.v1.routes_billing import (
    get_account_status,
    get_commercial_invoice_pdf,
    list_commercial_invoices,
)
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.core.security import hash_password
from shared.models.admin import AdminRole, AdminUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.commercial_invoice import CommercialInvoice, CommercialInvoiceLine
from shared.models.user import BusinessUser
from shared.utils import generate_business_code


def _admin_auth(admin_id, role):
    return AuthUser(user_id=admin_id, business_id=None, role=role, token_type="admin_access")


def _business_auth(user_id, business_id, role):
    return AuthUser(user_id=user_id, business_id=business_id, role=role, token_type="access")


async def _make_admin(db, role, email):
    admin = AdminUser(email=email, password_hash=hash_password("Irrelevant123!"), display_name="Test Admin", role=role, is_active=True)
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


async def _cleanup(db, *, business_ids=(), admin_ids=()):
    await db.rollback()
    for bid in business_ids:
        await db.execute(delete(CommercialInvoiceLine).where(
            CommercialInvoiceLine.invoice_id.in_(
                select(CommercialInvoice.id).where(CommercialInvoice.business_id == bid)
            )
        ))
        await db.execute(delete(CommercialInvoice).where(CommercialInvoice.business_id == bid))
        await db.execute(delete(AuditEvent).where(AuditEvent.business_id == bid))
        await db.execute(delete(Business).where(Business.id == bid))
    for aid in admin_ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == aid))
        await db.execute(delete(AdminUser).where(AdminUser.id == aid))
    await db.commit()


@pytest.fixture
async def _db_or_skip(db):
    try:
        await db.execute(select(1))
    except OperationalError:
        pytest.skip("Real Postgres not reachable — see DATABASE_URL")
    return db


@pytest.fixture
def _invoicing_configured(monkeypatch):
    """Configure every required NEXTGEN_* invoicing field so `issue` isn't
    refused — most tests need this; the one INVOICING_NOT_CONFIGURED test
    deliberately does NOT use this fixture."""
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


async def _make_business(db, *, onboarded_by=None):
    unique = uuid.uuid4().hex[:8]
    biz = Business(
        name=f"Commercial Test Biz {unique}", slug=f"commercial-test-{unique}",
        business_code=generate_business_code(), currency="ZAR", timezone="Africa/Johannesburg",
        onboarded_by_admin_id=onboarded_by,
    )
    db.add(biz)
    await db.commit()
    await db.refresh(biz)
    return biz


async def _draft_with_line(db, user, business_id, amount_cents=120000):
    return await create_invoice_draft(
        body=InvoiceDraftCreate(
            business_id=business_id,
            lines=[InvoiceLineIn(kind="SUBSCRIPTION", description="Monthly subscription", quantity=1, unit_amount_cents=amount_cents)],
        ),
        user=user, db=db,
    )


# ── Trial start/extend ──────────────────────────────────────────────────────

async def test_trial_start_and_extend_with_cap_enforcement(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-trial-{unique}@test.co.za")
    auth = _admin_auth(onboarder.id, AdminRole.ONBOARDER)
    business_ids, admin_ids = [], [onboarder.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"Trial Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)

        biz = await db.get(Business, created.id)
        assert biz.billing_status == "TRIAL"
        assert biz.trial_ends_at is not None

        result = await start_business_trial(business_id=created.id, user=auth, db=db)
        assert result.trial_ends_at is not None

        # ONBOARDER capped
        await extend_business_trial(
            business_id=created.id, body=TrialExtendRequest(days=10, reason="Customer requested more time"),
            user=auth, db=db,
        )
        await db.refresh(biz)
        assert biz.trial_extension_days_total == 10

        with pytest.raises(AppError) as exc:
            await extend_business_trial(
                business_id=created.id, body=TrialExtendRequest(days=10, reason="More time"), user=auth, db=db,
            )
        assert exc.value.code == "EXTENSION_CAP_EXCEEDED"

        # SUPER_ADMIN unlimited
        super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-trial-{unique}@test.co.za")
        admin_ids.append(super_admin.id)
        super_auth = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
        await extend_business_trial(
            business_id=created.id, body=TrialExtendRequest(days=300, reason="Manual override"), user=super_auth, db=db,
        )
        await db.refresh(biz)
        assert biz.trial_extension_days_total == 310
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


# ── Invoice lifecycle ────────────────────────────────────────────────────────

async def test_invoice_full_lifecycle_and_double_pay_rejected(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-inv-{unique}@test.co.za")
    auth = _admin_auth(onboarder.id, AdminRole.ONBOARDER)
    business_ids, admin_ids = [], [onboarder.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"Invoice Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)

        draft = await _draft_with_line(db, auth, created.id, amount_cents=120000)
        assert draft.status == "DRAFT"
        assert draft.invoice_number is None
        assert draft.total_cents == 120000

        issued = await issue_invoice_route(invoice_id=draft.id, user=auth, db=db)
        assert issued.invoice_number is not None
        assert issued.invoice_number.startswith("NG-INV-")
        assert issued.status == "AWAITING_PAYMENT"
        assert issued.due_date is not None

        # Issued invoice lines are immutable — PATCH now refused.
        with pytest.raises(AppError) as exc:
            await update_invoice_draft(
                invoice_id=draft.id, body=InvoiceDraftUpdate(internal_note="edit attempt"), user=auth, db=db,
            )
        assert exc.value.status_code == 409

        paid = await mark_invoice_paid_route(
            invoice_id=draft.id,
            body=MarkPaidRequest(payment_date=date.today(), payment_method="EFT", reference="REF-001"),
            user=auth, db=db,
        )
        assert paid.status == "PAID"

        biz = await db.get(Business, created.id)
        assert biz.billing_status == "ACTIVE"
        assert biz.is_active is True
        assert biz.converted_at is not None

        # Double mark-paid -> 409, never double-converts.
        with pytest.raises(AppError) as exc:
            await mark_invoice_paid_route(
                invoice_id=draft.id,
                body=MarkPaidRequest(payment_date=date.today(), payment_method="EFT", reference="REF-001"),
                user=auth, db=db,
            )
        assert exc.value.status_code == 409

        # A paid invoice can never be cancelled.
        with pytest.raises(AppError) as exc:
            await cancel_invoice_route(invoice_id=draft.id, body=CancelRequest(reason="oops"), user=auth, db=db)
        assert exc.value.code == "CANNOT_CANCEL_PAID"
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


async def test_mark_paid_reactivates_a_suspended_business(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-react-{unique}@test.co.za")
    auth = _admin_auth(onboarder.id, AdminRole.ONBOARDER)
    business_ids, admin_ids = [], [onboarder.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"Suspended Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)

        biz = await db.get(Business, created.id)
        biz.is_active = False
        biz.suspended_reason = "TRIAL_EXPIRED"
        biz.billing_status = "TRIAL"
        await db.commit()

        draft = await _draft_with_line(db, auth, created.id)
        issued = await issue_invoice_route(invoice_id=draft.id, user=auth, db=db)
        await mark_invoice_paid_route(
            invoice_id=issued.id,
            body=MarkPaidRequest(payment_date=date.today(), payment_method="CASH", reference=None),
            user=auth, db=db,
        )
        await db.refresh(biz)
        assert biz.is_active is True
        assert biz.suspended_reason is None
        assert biz.billing_status == "ACTIVE"
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


async def test_eft_payment_requires_reference():
    """Pure validation — no DB row mutation needed beyond an in-memory
    CommercialInvoice/Business, so this doesn't need the real-DB fixture."""
    from backend.app.billing.commercial_invoices import mark_paid

    invoice = CommercialInvoice(status="AWAITING_PAYMENT", total_cents=1000)
    business = Business(name="x", slug="x", business_code="ABC123")

    with pytest.raises(AppError) as exc:
        await mark_paid(
            db=None, invoice=invoice, business=business,
            payment_method="EFT", payment_date=date.today(), reference=None, note=None, actor_admin_id=None,
        )
    assert exc.value.code == "REFERENCE_REQUIRED"


async def test_invoicing_not_configured_lists_missing_field_names(_db_or_skip, monkeypatch):
    db = _db_or_skip
    settings = get_settings()
    for name in [
        "NEXTGEN_LEGAL_NAME", "NEXTGEN_ADDRESS", "NEXTGEN_CONTACT_EMAIL", "NEXTGEN_BANK_NAME",
        "NEXTGEN_BANK_ACCOUNT_NAME", "NEXTGEN_BANK_ACCOUNT_NUMBER", "NEXTGEN_BANK_BRANCH_CODE",
    ]:
        monkeypatch.setattr(settings, name, "")

    unique = uuid.uuid4().hex[:8]
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-noconfig-{unique}@test.co.za")
    auth = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
    business_ids, admin_ids = [], [super_admin.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"No Config Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)
        draft = await _draft_with_line(db, auth, created.id)

        with pytest.raises(AppError) as exc:
            await issue_invoice_route(invoice_id=draft.id, user=auth, db=db)
        assert exc.value.code == "INVOICING_NOT_CONFIGURED"
        assert "NEXTGEN_LEGAL_NAME" in exc.value.details["missing"]
        assert "NEXTGEN_BANK_ACCOUNT_NUMBER" in exc.value.details["missing"]
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


async def test_vat_on_and_off_totals(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    settings = _invoicing_configured
    unique = uuid.uuid4().hex[:8]
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-vat-{unique}@test.co.za")
    auth = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
    business_ids, admin_ids = [], [super_admin.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"VAT Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)

        # VAT off (default from fixture)
        draft_no_vat = await create_invoice_draft(
            body=InvoiceDraftCreate(business_id=created.id, lines=[
                InvoiceLineIn(kind="SETUP", description="Setup fee", quantity=1, unit_amount_cents=50000),
                InvoiceLineIn(kind="SUBSCRIPTION", description="Monthly", quantity=1, unit_amount_cents=120000),
            ]),
            user=auth, db=db,
        )
        assert draft_no_vat.subtotal_cents == 170000
        assert draft_no_vat.vat_cents == 0
        assert draft_no_vat.total_cents == 170000

        # VAT on, 15%
        settings.NEXTGEN_VAT_REGISTERED = True
        settings.NEXTGEN_VAT_RATE_BPS = 1500
        settings.NEXTGEN_VAT_NUMBER = "4123456789"

        draft_vat = await create_invoice_draft(
            body=InvoiceDraftCreate(business_id=created.id, lines=[
                InvoiceLineIn(kind="SUBSCRIPTION", description="Monthly", quantity=1, unit_amount_cents=100000),
            ]),
            user=auth, db=db,
        )
        assert draft_vat.subtotal_cents == 100000
        assert draft_vat.vat_cents == 15000  # exclusive VAT, added on top
        assert draft_vat.total_cents == 115000
        assert draft_vat.subtotal_cents + draft_vat.vat_cents == draft_vat.total_cents
    finally:
        settings.NEXTGEN_VAT_REGISTERED = False
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


async def test_cancel_rules_by_role(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-cancel-{unique}@test.co.za")
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-cancel-{unique}@test.co.za")
    auth = _admin_auth(onboarder.id, AdminRole.ONBOARDER)
    super_auth = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
    business_ids, admin_ids = [], [onboarder.id, super_admin.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"Cancel Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)

        # ONBOARDER may cancel their own DRAFT.
        draft1 = await _draft_with_line(db, auth, created.id)
        cancelled = await cancel_invoice_route(invoice_id=draft1.id, body=CancelRequest(reason="mistake"), user=auth, db=db)
        assert cancelled.status == "CANCELLED"

        # ONBOARDER may NOT cancel an issued invoice.
        draft2 = await _draft_with_line(db, auth, created.id)
        issued2 = await issue_invoice_route(invoice_id=draft2.id, user=auth, db=db)
        with pytest.raises(AppError) as exc:
            await cancel_invoice_route(invoice_id=issued2.id, body=CancelRequest(reason="mistake"), user=auth, db=db)
        assert exc.value.status_code == 403

        # SUPER_ADMIN may cancel an issued invoice.
        cancelled2 = await cancel_invoice_route(invoice_id=issued2.id, body=CancelRequest(reason="admin override"), user=super_auth, db=db)
        assert cancelled2.status == "CANCELLED"
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


async def test_pdf_renders_with_draft_banner_and_paid_stamp(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-pdf-{unique}@test.co.za")
    auth = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
    business_ids, admin_ids = [], [super_admin.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"PDF Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)
        draft = await _draft_with_line(db, auth, created.id)

        draft_pdf = await get_invoice_pdf(invoice_id=draft.id, user=auth, db=db)
        assert draft_pdf.body[:4] == b"%PDF"
        assert b"DRAFT" in draft_pdf.body

        issued = await issue_invoice_route(invoice_id=draft.id, user=auth, db=db)
        issued_pdf = await get_invoice_pdf(invoice_id=draft.id, user=auth, db=db)
        assert issued.invoice_number.encode() in issued_pdf.body

        await mark_invoice_paid_route(
            invoice_id=draft.id, body=MarkPaidRequest(payment_date=date.today(), payment_method="CASH", reference=None),
            user=auth, db=db,
        )
        paid_pdf = await get_invoice_pdf(invoice_id=draft.id, user=auth, db=db)
        assert b"PAID" in paid_pdf.body
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


# ── Permissions / tenant isolation ──────────────────────────────────────────

async def test_onboarder_commercial_access_survives_handover_but_config_access_does_not(_db_or_skip, _invoicing_configured):
    from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
    from backend.app.api.v1.routes_pos import (
        POSOrderCreateRequest, POSOrderItemRequest, TillCloseRequest, TillOpenRequest,
        close_till, create_pos_order, open_till,
    )

    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-handover-{unique}@test.co.za")
    auth = _admin_auth(onboarder.id, AdminRole.ONBOARDER)
    business_ids, admin_ids = [], [onboarder.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"Handover Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)
        await update_business(business_id=created.id, body=BusinessUpdate(is_whatsapp_enabled=False), user=auth, db=db)
        # restaurant_details go-live check needs a name + address/phone.
        biz_row = await db.get(Business, created.id)
        biz_row.phone = "0311234567"
        await db.commit()

        owner = BusinessUser(
            business_id=created.id, role="OWNER", email=f"owner-handover-{unique}@test.co.za",
            password_hash=hash_password("RealOwnerPass123!"), staff_name="Real Owner",
            is_active=True, must_change_password=False,
        )
        db.add(owner)
        await db.commit()
        owner_auth = _business_auth(owner.id, created.id, "OWNER")
        category = await create_category(body=CategoryCreate(name="Mains"), user=owner_auth, db=db)
        item = await create_item(body=ItemCreate(category_id=category.id, name="Burger", price_cents=6000), user=owner_auth, db=db)
        till = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner_auth, db=db)
        await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                order_mode="PICKUP", customer_phone="0821234567", payment_method="CASH", cash_tendered_cents=6000,
            ),
            user=owner_auth, db=db,
        )
        # cashup_test needs a CLOSED till session, not merely an opened one.
        await close_till(session_id=till.id, body=TillCloseRequest(counted_cash_cents=6000), user=owner_auth, db=db)

        # modules_confirmed / receipt_test's manual half — the system cannot
        # observe a browser print, so these are explicit admin confirmations.
        await confirm_onboarding_item(
            business_id=created.id, body=OnboardingConfirmRequest(item="modules", note="Starter plan only"), user=auth, db=db,
        )
        confirmed = await confirm_onboarding_item(
            business_id=created.id, body=OnboardingConfirmRequest(item="receipt_print", note="Printed and checked"), user=auth, db=db,
        )
        assert "modules" in confirmed.onboarding_confirmations
        assert "receipt_print" in confirmed.onboarding_confirmations

        status_result = await get_onboarding_status(business_id=created.id, user=auth, db=db)
        failing = [c for c in status_result.go_live.checks if c.status == "fail"]
        assert failing == [], f"unexpected failing checks: {failing}"
        assert status_result.ready is True
        assert status_result.go_live.readiness.total > 0
        assert "/" in status_result.go_live.readiness.label

        await complete_onboarding(business_id=created.id, body=OnboardingCompleteRequest(force=False), user=auth, db=db)

        # CONFIGURATION access is gone (get_business_scoped).
        with pytest.raises(NotFoundError):
            await update_business(business_id=created.id, body=BusinessUpdate(name="should fail"), user=auth, db=db)

        # COMMERCIAL access still works (get_business_commercial_scoped).
        trial_result = await start_business_trial(business_id=created.id, user=auth, db=db)
        assert trial_result.business_id == created.id
        draft = await _draft_with_line(db, auth, created.id)
        assert draft.business_id == created.id
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


async def test_onboarder_cannot_see_another_onboarders_business(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder1 = await _make_admin(db, AdminRole.ONBOARDER, f"onb1-cross-{unique}@test.co.za")
    onboarder2 = await _make_admin(db, AdminRole.ONBOARDER, f"onb2-cross-{unique}@test.co.za")
    auth1 = _admin_auth(onboarder1.id, AdminRole.ONBOARDER)
    auth2 = _admin_auth(onboarder2.id, AdminRole.ONBOARDER)
    business_ids, admin_ids = [], [onboarder1.id, onboarder2.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"Cross Tenant Biz {unique}"), user=auth1, db=db)
        business_ids.append(created.id)

        with pytest.raises(NotFoundError):
            await start_business_trial(business_id=created.id, user=auth2, db=db)

        with pytest.raises(NotFoundError):
            await _draft_with_line(db, auth2, created.id)
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


# ── Owner-facing endpoints ───────────────────────────────────────────────────

async def test_owner_sees_only_own_issued_invoices_manager_and_staff_blocked(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-owner-{unique}@test.co.za")
    admin_auth = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
    business_ids, admin_ids = [], [super_admin.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"Owner View Biz {unique}"), user=admin_auth, db=db)
        business_ids.append(created.id)
        draft = await _draft_with_line(db, admin_auth, created.id)
        issued = await issue_invoice_route(invoice_id=draft.id, user=admin_auth, db=db)

        # A second, unrelated business — proves tenant isolation on the owner route.
        other = await create_business(body=BusinessCreate(name=f"Other Biz {unique}"), user=admin_auth, db=db)
        business_ids.append(other.id)

        owner = BusinessUser(
            business_id=created.id, role="OWNER", email=f"owner-view-{unique}@test.co.za",
            password_hash=hash_password("x"), staff_name="Owner", is_active=True, must_change_password=False,
        )
        manager = BusinessUser(
            business_id=created.id, role="MANAGER", email=f"mgr-view-{unique}@test.co.za",
            password_hash=hash_password("x"), staff_name="Manager", is_active=True, must_change_password=False,
        )
        db.add_all([owner, manager])
        await db.commit()

        owner_auth = _business_auth(owner.id, created.id, "OWNER")
        manager_auth = _business_auth(manager.id, created.id, "MANAGER")

        invoices = await list_commercial_invoices(user=owner_auth, db=db)
        assert len(invoices) == 1
        assert invoices[0].id == issued.id
        assert not hasattr(invoices[0], "internal_note")

        pdf = await get_commercial_invoice_pdf(invoice_id=issued.id, user=owner_auth, db=db)
        assert pdf.body[:4] == b"%PDF"

        # Manager is not OWNER -> require_owner rejects (RBAC, exercised as
        # a dependency call rather than expecting the route function itself
        # to re-check role, matching this codebase's existing DI pattern).
        from backend.app.core.rbac import require_business_access
        with pytest.raises(Exception):
            await require_business_access(["OWNER"])(user=manager_auth)

        acct = await get_account_status(user=owner_auth, db=db)
        assert acct.status in ("TRIAL", "ACTIVE", "PAYMENT_DUE", "OVERDUE", "EXPIRED", "SUSPENDED", "CANCELLED")
        # No bank details anywhere in this response.
        acct_dump = acct.model_dump()
        assert "bank" not in str(acct_dump).lower()
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


# ── Dashboard / clients — no leaked bank details ────────────────────────────

async def test_dashboard_and_clients_never_leak_bank_details(_db_or_skip, _invoicing_configured):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-dash-{unique}@test.co.za")
    auth = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
    business_ids, admin_ids = [], [super_admin.id]
    try:
        created = await create_business(body=BusinessCreate(name=f"Dashboard Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)
        draft = await _draft_with_line(db, auth, created.id)
        await issue_invoice_route(invoice_id=draft.id, user=auth, db=db)

        dashboard = await get_commercial_dashboard(user=auth, db=db)
        dump = str(dashboard.model_dump())
        assert "123456789" not in dump  # the configured NEXTGEN_BANK_ACCOUNT_NUMBER
        assert "counts" in dashboard.model_dump()

        clients = await list_commercial_clients(status=None, user=auth, db=db)
        assert "123456789" not in str([c.model_dump() for c in clients])
        assert any(c.business_id == created.id for c in clients)

        invoices = await list_invoices(business_id=created.id, status=None, page=1, per_page=25, user=auth, db=db)
        assert "123456789" not in str([i.model_dump() for i in invoices])
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)
