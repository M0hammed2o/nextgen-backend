"""
Real-DB integration coverage for migration 0057_pos_entitlement (the new
WhatsApp-only subscription package):

1. `require_pos_enabled` (backend/app/core/rbac.py) actually reads the live
   `Business.pos_enabled` column and fails closed.
2. `get_whatsapp_order_usage` (backend/app/billing/whatsapp_usage.py) counts
   only WHATSAPP-source, terminal-status orders in the current billing
   period, excludes CANCELLED, and resets across a billing-period boundary.
3. An ONBOARDER cannot set `pos_enabled`/`whatsapp_order_allowance` (create
   or PATCH), but CAN complete go-live for a `pos_enabled=False` tenant
   without `force=true` once the POS-dependent checks read n/a.
4. `check_whatsapp_usage_alerts` (backend/app/worker.py) records a
   `UsageAlert` row once per newly-crossed threshold per billing period,
   not repeatedly.
5. Cross-tenant isolation for the new `GET /business/customers` /
   `GET /business/customers/{id}` endpoints.

Requires TEST_DATABASE_URL (see docker-compose.test.yml / TESTING.md).
Skips cleanly if the dedicated test Postgres is configured but temporarily
unreachable — same pattern as test_workflow_onboarding.py /
test_business_list_search.py.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from admin_api.app.api.v1.routes_businesses import (
    BusinessCreate,
    BusinessUpdate,
    create_business,
    create_business_owner,
    get_business_usage,
    get_onboarding_status,
    complete_onboarding,
    update_business,
    OnboardingCompleteRequest,
    OwnerCreateRequest,
)
from backend.app.api.v1.routes_customers import get_customer, list_customers
from backend.app.billing.whatsapp_usage import get_whatsapp_order_usage
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_pos_enabled
from backend.app.core.security import hash_password
from backend.app.worker import check_whatsapp_usage_alerts
from shared.enums import OrderSource, OrderStatus
from shared.models.admin import AdminRole, AdminUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.customer import Customer
from shared.models.order import Order
from shared.models.usage_alert import UsageAlert
from shared.models.user import BusinessUser


def _admin_auth(admin_id: uuid.UUID, role: str) -> AuthUser:
    return AuthUser(user_id=admin_id, business_id=None, role=role, token_type="admin_access")


async def _make_admin(db, role: str, email: str) -> AdminUser:
    admin = AdminUser(
        email=email, password_hash=hash_password("Irrelevant123!"),
        display_name="Test Admin", role=role, is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


async def _make_standalone_business(db, *, name_prefix: str) -> Business:
    """
    A real Business row, created/committed directly — deliberately NOT the
    shared `test_business` fixture (tests/integration/conftest.py) for the
    two tests below that insert many orders/alerts in rapid succession:
    that fixture's own teardown re-touches `business.id` after this test's
    many commits and intermittently hits a `MissingGreenlet` pool-checkout
    error in THIS specific Windows/asyncpg/Docker environment (reproduced
    consistently, not caused by anything in this change — the fixture
    itself is unmodified, pre-existing code, out of scope to fix here).
    Managing the row's lifecycle explicitly, the same way
    test_business_list_search.py / test_workflow_onboarding.py already do
    for every Business they create, avoids that path entirely.
    """
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"{name_prefix} {unique}", slug=f"{name_prefix.lower().replace(' ', '-')}-{unique}",
        business_code=uuid.uuid4().hex[:6].upper(), currency="ZAR", timezone="Africa/Johannesburg",
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)
    return business


def _make_order(business_id, *, source, status, created_at, order_number):
    return Order(
        business_id=business_id, order_number=order_number,
        status=status, payment_status="PAID", source=source,
        subtotal_cents=1000, total_cents=1000, currency="ZAR",
        created_at=created_at,
    )


async def _cleanup(db, *, business_ids=(), admin_ids=(), order_ids=(), customer_ids=()):
    await db.rollback()
    for oid in order_ids:
        await db.execute(delete(Order).where(Order.id == oid))
    for cid in customer_ids:
        await db.execute(delete(Customer).where(Customer.id == cid))
    for bid in business_ids:
        await db.execute(delete(UsageAlert).where(UsageAlert.business_id == bid))
        await db.execute(delete(Order).where(Order.business_id == bid))
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
        pytest.skip("Dedicated test Postgres not reachable — see TEST_DATABASE_URL / docker-compose.test.yml")
    return db


# ── 1. require_pos_enabled dependency ───────────────────────────────────────

async def test_require_pos_enabled_fails_closed_against_real_column(_db_or_skip, test_business):
    db = _db_or_skip
    user = AuthUser(user_id=uuid.uuid4(), business_id=test_business.id, role="STAFF", token_type="access")

    # Default (pos_enabled=True from the migration's server_default) — passes.
    await require_pos_enabled(user=user, db=db)

    # Flip it off directly in the DB (simulating a SUPER_ADMIN entitlement
    # change) and confirm the dependency re-reads the live value, not a
    # cached one.
    test_business.pos_enabled = False
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await require_pos_enabled(user=user, db=db)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "POS_NOT_ENABLED"

    # Flip back on — the dependency must reflect the live value both ways.
    test_business.pos_enabled = True
    await db.commit()
    await require_pos_enabled(user=user, db=db)


async def test_require_pos_enabled_rejects_no_business_context(_db_or_skip):
    db = _db_or_skip
    user = AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")
    with pytest.raises(HTTPException) as exc:
        await require_pos_enabled(user=user, db=db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "NO_BUSINESS_CONTEXT"


# ── 2. WhatsApp order-usage counter ─────────────────────────────────────────

async def test_whatsapp_order_usage_counts_only_whatsapp_terminal_current_period(_db_or_skip):
    db = _db_or_skip
    biz = await _make_standalone_business(db, name_prefix="WA Usage Counter Biz")
    business_id = biz.id
    biz.whatsapp_order_allowance = 10
    period_start = datetime.now(timezone.utc) - timedelta(days=5)
    biz.billing_period_started_at = period_start
    await db.commit()

    order_ids = []
    business_ids = [business_id]
    now = datetime.now(timezone.utc)

    def _add(**kwargs):
        o = _make_order(business_id, **kwargs)
        db.add(o)
        return o

    try:
        # Counts: WHATSAPP + terminal statuses, within the current period.
        counted = [
            _add(source=OrderSource.WHATSAPP.value, status=OrderStatus.COLLECTED.value,
                 created_at=now, order_number=f"BO-{uuid.uuid4().hex[:8]}"),
            _add(source=OrderSource.WHATSAPP.value, status=OrderStatus.DELIVERED.value,
                 created_at=now, order_number=f"BO-{uuid.uuid4().hex[:8]}"),
            _add(source=OrderSource.WHATSAPP.value, status=OrderStatus.COMPLETED.value,
                 created_at=now, order_number=f"BO-{uuid.uuid4().hex[:8]}"),
        ]
        # Excluded: CANCELLED (never counted, even though WHATSAPP + in-period).
        excluded_cancelled = _add(
            source=OrderSource.WHATSAPP.value, status=OrderStatus.CANCELLED.value,
            created_at=now, order_number=f"BO-{uuid.uuid4().hex[:8]}",
        )
        # Excluded: still in-flight (non-terminal) WhatsApp order.
        excluded_in_flight = _add(
            source=OrderSource.WHATSAPP.value, status=OrderStatus.NEW.value,
            created_at=now, order_number=f"BO-{uuid.uuid4().hex[:8]}",
        )
        # Excluded: a terminal POS order — not WhatsApp-source at all.
        excluded_pos = _add(
            source=OrderSource.POS.value, status=OrderStatus.COMPLETED.value,
            created_at=now, order_number=f"BO-{uuid.uuid4().hex[:8]}",
        )
        # Excluded: a terminal WhatsApp order from BEFORE the current
        # billing period (billing-period-boundary correctness).
        excluded_prior_period = _add(
            source=OrderSource.WHATSAPP.value, status=OrderStatus.COMPLETED.value,
            created_at=period_start - timedelta(days=1), order_number=f"BO-{uuid.uuid4().hex[:8]}",
        )
        await db.commit()
        order_ids = [o.id for o in counted + [excluded_cancelled, excluded_in_flight, excluded_pos, excluded_prior_period]]

        usage = await get_whatsapp_order_usage(db, biz)
        assert usage.orders_used == 3, "must count only WHATSAPP + terminal + current-period orders"
        # Transparency total: every WHATSAPP order in-period regardless of
        # status (COLLECTED/DELIVERED/COMPLETED/CANCELLED/NEW = 5), excludes
        # the POS order and the prior-period order.
        assert usage.orders_total_all_statuses == 5
        assert usage.orders_allowance == 10
        assert usage.orders_remaining == 7
        assert usage.tier == "ok"
    finally:
        await _cleanup(db, order_ids=order_ids, business_ids=business_ids)


async def test_whatsapp_order_usage_none_allowance_reports_none_fields(_db_or_skip, test_business):
    db = _db_or_skip
    assert test_business.whatsapp_order_allowance is None
    usage = await get_whatsapp_order_usage(db, test_business)
    assert usage.orders_allowance is None
    assert usage.orders_remaining is None
    assert usage.pct_used is None
    assert usage.tier is None


# ── 3. ONBOARDER restrictions + go-live n/a for a POS-off tenant ───────────

async def test_onboarder_cannot_set_pos_fields_but_can_complete_go_live_without_force(_db_or_skip):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onb-pos-{unique}@test.co.za")
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-pos-{unique}@test.co.za")
    auth = _admin_auth(onboarder.id, AdminRole.ONBOARDER)
    auth_super = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)

    business_ids, admin_ids = [], [onboarder.id, super_admin.id]
    try:
        # ONBOARDER cannot set pos_enabled=False at creation time.
        with pytest.raises(AppError) as exc:
            await create_business(
                body=BusinessCreate(name=f"WA Only Attempt {unique}", pos_enabled=False),
                user=auth, db=db,
            )
        assert exc.value.status_code == 403
        assert exc.value.code == "FIELD_NOT_ALLOWED"

        with pytest.raises(AppError) as exc2:
            await create_business(
                body=BusinessCreate(name=f"WA Only Attempt 2 {unique}", whatsapp_order_allowance=1000),
                user=auth, db=db,
            )
        assert exc2.value.status_code == 403

        # Normal creation (defaults) succeeds.
        created = await create_business(body=BusinessCreate(name=f"WA Only Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)

        # ONBOARDER cannot PATCH pos_enabled either (default-deny field).
        with pytest.raises(AppError) as exc3:
            await update_business(business_id=created.id, body=BusinessUpdate(pos_enabled=False), user=auth, db=db)
        assert exc3.value.status_code == 403
        assert exc3.value.code == "FIELD_NOT_ALLOWED"

        # SUPER_ADMIN flips the entitlement — this is the billing-tier
        # decision only the platform operator may make.
        await update_business(business_id=created.id, body=BusinessUpdate(pos_enabled=False), user=auth_super, db=db)
        biz_row = await db.get(Business, created.id)
        assert biz_row.pos_enabled is False

        # WhatsApp off too, so that check reads n/a — isolates this test to
        # exactly the POS-dependent checks' n/a behaviour.
        await update_business(business_id=created.id, body=BusinessUpdate(is_whatsapp_enabled=False), user=auth, db=db)

        # ── Build up the NON-POS go-live evidence (owner, staff PIN, menu,
        # restaurant details, modules confirmation) — deliberately NO till,
        # NO POS order, NO receipt print confirmation, since this tenant has
        # no till at all.
        from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
        from admin_api.app.api.v1.routes_businesses import OnboardingConfirmRequest, confirm_onboarding_item

        owner = BusinessUser(
            business_id=created.id, role="OWNER", email=f"owner-pos-{unique}@test.co.za",
            password_hash=hash_password("RealOwnerPass123!"), staff_name="Real Owner",
            is_active=True, must_change_password=False,
        )
        staff = BusinessUser(
            business_id=created.id, role="STAFF", email=None, staff_name="Real Staff",
            pin_hash=hash_password("1234"), is_active=True,
        )
        db.add_all([owner, staff])
        await db.commit()

        owner_auth = AuthUser(user_id=owner.id, business_id=created.id, role="OWNER", token_type="access")
        category = await create_category(body=CategoryCreate(name="Mains"), user=owner_auth, db=db)
        await create_item(
            body=ItemCreate(category_id=category.id, name="Burger", price_cents=6000), user=owner_auth, db=db,
        )

        biz_row.address = "1 Test Street, Durban"
        await db.commit()
        await confirm_onboarding_item(
            business_id=created.id, body=OnboardingConfirmRequest(item="modules"), user=auth, db=db,
        )

        status = await get_onboarding_status(business_id=created.id, user=auth, db=db)
        for key in ("vat_receipt", "pos_payment_method", "till_session", "pos_test_order", "receipt_test", "cashup_test"):
            check = next(c for c in status.go_live.checks if c.key == key)
            assert check.status == "n/a", f"{key} should be n/a for a pos_enabled=False tenant, got {check.status}"

        failing = [c for c in status.go_live.checks if c.status == "fail"]
        assert failing == [], f"unexpected failing checks for a WhatsApp-only tenant: {failing}"
        assert status.ready is True

        # The actual point of this test: complete go-live WITHOUT force=true.
        result = await complete_onboarding(
            business_id=created.id, body=OnboardingCompleteRequest(force=False), user=auth, db=db,
        )
        assert result.onboarding_completed_at is not None
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=admin_ids)


# ── 4. Daily usage-alert job fires once per newly-crossed threshold ───────

async def test_usage_alert_job_fires_once_per_threshold_per_period(_db_or_skip):
    from tests.integration._db import get_session_factory

    db = _db_or_skip
    biz = await _make_standalone_business(db, name_prefix="WA Usage Alert Biz")
    business_id = biz.id
    biz.whatsapp_order_allowance = 10
    await db.commit()

    order_ids = []
    business_ids = [business_id]
    try:
        # 9 billable WhatsApp orders = 90% of a 10-order allowance.
        for i in range(9):
            o = _make_order(
                business_id, source=OrderSource.WHATSAPP.value, status=OrderStatus.COMPLETED.value,
                created_at=datetime.now(timezone.utc), order_number=f"BO-{uuid.uuid4().hex[:8]}",
            )
            db.add(o)
            order_ids.append(o)
        await db.commit()
        order_ids = [o.id for o in order_ids]

        # The production code opens its OWN session per run via
        # ctx["session_factory"] — use the real integration-test session
        # factory here too (same pattern as
        # test_workflow_onboarding.py::test_concurrent_demotions_...),
        # rather than reusing this test's own `db` fixture session, so this
        # exercises the actual production code path.
        ctx = {"session_factory": get_session_factory()}
        await check_whatsapp_usage_alerts(ctx)

        alerts = (await db.execute(select(UsageAlert).where(UsageAlert.business_id == business_id))).scalars().all()
        thresholds = sorted(a.threshold_crossed for a in alerts)
        assert thresholds == [80, 90], f"expected both 80 and 90 crossed at 90% usage, got {thresholds}"

        # Running the job again for the SAME state must NOT create duplicates.
        await check_whatsapp_usage_alerts(ctx)
        alerts_again = (await db.execute(select(UsageAlert).where(UsageAlert.business_id == business_id))).scalars().all()
        assert len(alerts_again) == 2, "re-running the job must not duplicate already-recorded alerts"
    finally:
        await db.execute(delete(UsageAlert).where(UsageAlert.business_id == business_id))
        await _cleanup(db, order_ids=order_ids, business_ids=business_ids)


async def test_usage_alerts_route_includes_business_name(_db_or_skip, db):
    """GET /admin/usage-alerts (admin_api/app/api/v1/routes_usage.py::
    list_usage_alerts) -- exercises the actual route function, not just the
    cron job that writes the rows. Added to reconcile a real contract gap:
    a parallel frontend session assumed a `business_name` field would be
    present; the route originally returned bare UUIDs only. Proves the fix
    (a join to Business.name) actually works against a real business row,
    not just that it type-checks."""
    biz = await _make_standalone_business(db, name_prefix="WA Usage Alert Name Biz")
    business_id = biz.id
    biz.whatsapp_order_allowance = 10
    await db.commit()
    await db.refresh(biz)
    expected_name = biz.name

    alert = UsageAlert(
        business_id=business_id,
        billing_period_started_at=biz.billing_period_started_at,
        threshold_crossed=80,
        orders_used=8,
        orders_allowance=10,
    )
    db.add(alert)
    await db.commit()

    try:
        from admin_api.app.api.v1.routes_usage import list_usage_alerts

        super_admin = _admin_auth(uuid.uuid4(), "SUPER_ADMIN")
        # page/per_page must be passed explicitly: calling a route function
        # directly (bypassing FastAPI's own request handling) does not
        # resolve Query(default=...) objects to their defaults -- the same
        # trap already documented elsewhere in this file/repo.
        results = await list_usage_alerts(
            business_id=business_id, page=1, per_page=50, user=super_admin, db=db,
        )

        assert len(results) == 1
        assert results[0].business_id == business_id
        assert results[0].business_name == expected_name
        assert results[0].threshold_crossed == 80
        assert results[0].orders_used == 8
        assert results[0].orders_allowance == 10
    finally:
        await db.execute(delete(UsageAlert).where(UsageAlert.business_id == business_id))
        await _cleanup(db, order_ids=[], business_ids=[business_id])


# ── 5. Cross-tenant isolation for the new Customers endpoints ──────────────

async def test_customers_endpoints_are_tenant_isolated(_db_or_skip, test_business):
    db = _db_or_skip
    biz_a = test_business
    unique = uuid.uuid4().hex[:8]
    biz_b = Business(
        name=f"Other Biz {unique}", slug=f"other-biz-{unique}",
        business_code=uuid.uuid4().hex[:6].upper(), currency="ZAR", timezone="Africa/Johannesburg",
    )
    db.add(biz_b)
    await db.commit()
    await db.refresh(biz_b)

    customer_a = Customer(business_id=biz_a.id, display_name="Alice", phone_number="0821111111")
    customer_b = Customer(business_id=biz_b.id, display_name="Bob", phone_number="0822222222")
    db.add_all([customer_a, customer_b])
    await db.commit()

    user_a = AuthUser(user_id=uuid.uuid4(), business_id=biz_a.id, role="STAFF", token_type="access")
    user_b = AuthUser(user_id=uuid.uuid4(), business_id=biz_b.id, role="STAFF", token_type="access")

    try:
        listing_a = await list_customers(page=1, per_page=50, search=None, user=user_a, db=db)
        ids_a = {c["id"] if isinstance(c, dict) else c.id for c in listing_a.data}
        assert customer_a.id in ids_a
        assert customer_b.id not in ids_a

        # Business A can read its own customer...
        detail = await get_customer(customer_id=customer_a.id, user=user_a, db=db)
        assert detail.id == customer_a.id

        # ...but never Business B's, even by direct id (404, not a leak).
        with pytest.raises(NotFoundError):
            await get_customer(customer_id=customer_b.id, user=user_a, db=db)
        with pytest.raises(NotFoundError):
            await get_customer(customer_id=customer_a.id, user=user_b, db=db)
    finally:
        await db.execute(delete(Customer).where(Customer.id.in_([customer_a.id, customer_b.id])))
        await db.execute(delete(Business).where(Business.id == biz_b.id))
        await db.commit()
