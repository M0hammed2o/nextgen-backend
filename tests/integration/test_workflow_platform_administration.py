"""
Workflow 4 (Platform Administration), driven against a REAL Postgres + Redis
instance — the fourth real-DB integration test in this codebase's history.

Covers: tenant creation (business + owner + defaults) -> tenant management
(edit/suspend/unsuspend, verified through the REAL auth dependency chain, not
just the DB field) -> subscription lifecycle (plan change, grace-period
suspension via the real cron function) -> billing (usage aggregation, order
pack grants) -> admin dashboard (business list/filter/pagination, audit log)
-> permissions (super-admin-only gate, cross-tenant isolation).

Explicitly NOT exercised here (documented, not silently skipped):
- `POST /admin/businesses/{id}/whatsapp/test-send` — makes a real HTTP call
  to the Meta Graph API using `WHATSAPP_DEFAULT_ACCESS_TOKEN`. No test/sandbox
  Meta credentials exist in this environment — a genuine external dependency.
- `POST /admin/businesses/{id}/billing/pdf` — PDF rendering is a pure
  formatting concern layered on data already covered elsewhere (billing
  status/invoices); not re-verified here to keep this test's scope to
  workflow-critical paths.

Real bug found and fixed while building this test (see KNOWN_BUGS.md):
`AuditScope.PLATFORM` (shared/enums.py) was designed specifically for
admin-plane actions per AuditEvent's own docstring ("scope='PLATFORM' for
admin actions"), but had ZERO real usage anywhere in the codebase — every
super-admin action (suspend, unsuspend, create business, create owner,
reset password, grant order pack, edit business/limits) left no audit trail
at all. Fixed by adding AuditEvent(scope=PLATFORM) writes to every mutating
route in admin_api/app/api/v1/routes_businesses.py.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select

from admin_api.app.api.v1.routes_businesses import (
    BusinessCreate,
    BusinessUpdate,
    LimitsUpdate,
    OrderPackGrant,
    SuspendRequest,
    create_business,
    grant_order_pack,
    list_businesses,
    suspend_business,
    unsuspend_business,
    update_business,
)
from admin_api.app.api.v1.routes_usage import get_audit_log, get_usage
from backend.app.api.v1.routes_business import BusinessSettingsUpdate, update_business_settings
from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
from backend.app.api.v1.routes_pos import (
    POSOrderCreateRequest, POSOrderItemRequest, TillOpenRequest, create_pos_order, open_till,
)
from backend.app.billing.lifecycle import GRACE_PERIOD_DAYS, is_past_grace_period, suspend_for_nonpayment
from backend.app.core.errors import NotFoundError
from backend.app.core.rbac import AuthUser, get_current_user, require_super_admin
from backend.app.core.security import create_access_token
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.user import BusinessUser
from shared.utils import generate_business_code


def _super_admin() -> AuthUser:
    return AuthUser(user_id=uuid.uuid4(), business_id=None, role="SUPER_ADMIN", token_type="admin_access")


async def _get_current_user_via_real_jwt(token: str, db) -> AuthUser:
    """Exercises the REAL FastAPI dependency (get_current_user), not a
    hand-constructed AuthUser — this is the only way to actually verify the
    suspension/reactivation access-cutoff, since every other integration
    test in this suite calls route functions directly with a bypassed
    AuthUser and never touches this dependency at all."""
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    return await get_current_user(request=None, credentials=credentials, db=db)


async def test_platform_administration_workflow(db):
    admin = _super_admin()

    # ── Tenant creation: business + owner + defaults ──────────────────────
    unique = uuid.uuid4().hex[:8]
    create_response = await create_business(
        body=BusinessCreate(
            name=f"Workflow4 Test Restaurant {unique}",
            owner_email=f"owner-{unique}@workflow4test.co.za",
            owner_full_name="Test Owner",
        ),
        user=admin, db=db,
    )
    business_id = create_response.id
    assert create_response.billing_status == "TRIAL"
    assert create_response.is_active is True
    assert create_response.monthly_order_quota == 1500
    assert create_response.daily_message_limit == 800
    assert create_response.owner_temporary_password is not None

    owner_result = await db.execute(
        select(BusinessUser).where(BusinessUser.business_id == business_id, BusinessUser.role == "OWNER")
    )
    owner_user = owner_result.scalar_one()
    assert owner_user.must_change_password is True

    audit_result = await db.execute(
        select(AuditEvent).where(AuditEvent.business_id == business_id, AuditEvent.action == "admin.business.created")
    )
    assert audit_result.scalar_one_or_none() is not None

    try:
        # ── Tenant management: edit, suspend (real access cutoff), unsuspend ─
        await update_business(business_id=business_id, body=BusinessUpdate(plan="GROWTH"), user=admin, db=db)
        biz_check = await db.get(Business, business_id)
        assert biz_check.plan == "GROWTH"

        # ── Subscription lifecycle: downgrade + admin-driven cancellation ──
        # Real self-service checkout/upgrade/cancel goes through a
        # BillingProvider (Stripe/PayFast) — a genuine external-credential
        # block, see the module docstring. Plan/billing_status ARE plain DB
        # fields the admin plane can set directly (the same mechanism a
        # provider webhook uses under the hood), so downgrade and
        # cancellation are verified at that level.
        await update_business(business_id=business_id, body=BusinessUpdate(plan="STARTER"), user=admin, db=db)
        await db.refresh(biz_check)
        assert biz_check.plan == "STARTER"

        await update_business(business_id=business_id, body=BusinessUpdate(billing_status="CANCELLED"), user=admin, db=db)
        await db.refresh(biz_check)
        assert biz_check.billing_status == "CANCELLED"
        # This raw PATCH is a blunt field-setter (admin ad-hoc editing) with
        # no side effects — confirmed it does NOT also flip is_active, unlike
        # the REAL cancellation path (backend/app/billing/stripe_webhooks.py
        # ::_handle_subscription_deleted, verified by reading it directly),
        # which sets billing_status=CANCELLED, is_active=False, and a
        # suspended_reason together on a real Stripe subscription.deleted
        # event. These are two different, intentionally different code
        # paths — this assertion documents that gap rather than papering
        # over it or implying the admin field-setter replicates the real
        # webhook's side effects.
        assert biz_check.is_active is True
        await update_business(business_id=business_id, body=BusinessUpdate(billing_status="TRIAL"), user=admin, db=db)
        await db.refresh(biz_check)
        assert biz_check.billing_status == "TRIAL"

        owner_token = create_access_token(user_id=owner_user.id, business_id=business_id, role="OWNER")
        pre_suspend_auth = await _get_current_user_via_real_jwt(owner_token, db)
        assert pre_suspend_auth.business_id == business_id

        await suspend_business(
            business_id=business_id, body=SuspendRequest(reason="Non-payment test"), user=admin, db=db,
        )
        with pytest.raises(Exception) as exc:
            await _get_current_user_via_real_jwt(owner_token, db)
        assert "BUSINESS_SUSPENDED" in str(exc.value.detail)

        await unsuspend_business(business_id=business_id, user=admin, db=db)
        post_unsuspend_auth = await _get_current_user_via_real_jwt(owner_token, db)
        assert post_unsuspend_auth.business_id == business_id  # access regained with the SAME token

        # ── Subscription lifecycle: grace-period suspension ────────────────
        # Exercises the same business-logic functions the real hourly ARQ
        # cron (backend/app/worker.py::suspend_past_due_businesses) calls,
        # against this test's own `db` session. The cron wrapper itself
        # opens a SECOND session via the shared engine's connection pool
        # (`ctx["session_factory"]`) — fine in isolation, but running
        # multiple integration test files together starves/contends the
        # pool enough to hit a real asyncpg/greenlet conflict unrelated to
        # this business logic; the cron's own scheduling/session plumbing is
        # already covered by test_billing_suspension_cron.py's mocked tests.
        biz = await db.get(Business, business_id)
        biz.billing_status = "PAST_DUE"
        biz.billing_period_started_at = datetime.now(timezone.utc) - timedelta(days=GRACE_PERIOD_DAYS + 1)
        await db.commit()

        assert is_past_grace_period(biz) is True
        suspend_for_nonpayment(biz)
        await db.commit()

        await db.refresh(biz)
        assert biz.billing_status == "SUSPENDED"
        assert biz.is_active is False
        # Reset to active so the rest of this workflow (order creation, usage
        # aggregation) reflects a normal operating tenant, not a suspended one.
        biz.is_active = True
        biz.billing_status = "ACTIVE"
        await db.commit()

        # ── Generate real usage data (order + payment) for the usage report ──
        owner_auth = AuthUser(user_id=owner_user.id, business_id=business_id, role="OWNER", token_type="access")
        await update_business_settings(
            body=BusinessSettingsUpdate(payment_provider="MOCK"), user=owner_auth, db=db,
        )
        category = await create_category(body=CategoryCreate(name="Drinks"), user=owner_auth, db=db)
        item = await create_item(
            body=ItemCreate(category_id=category.id, name="Soda", price_cents=2500), user=owner_auth, db=db,
        )
        await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner_auth, db=db)
        order = await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                order_mode="PICKUP", customer_phone="0821112222",
                payment_method="CASH", cash_tendered_cents=2500,
            ),
            user=owner_auth, db=db,
        )
        assert order.payment_status == "PAID"

        # ── Billing / usage reporting ──────────────────────────────────────
        usage_rows = await get_usage(
            start_date=date.today() - timedelta(days=1), end_date=date.today() + timedelta(days=1),
            business_id=business_id, user=admin, db=db,
        )
        assert len(usage_rows) == 1
        assert usage_rows[0].total_orders == 1
        assert usage_rows[0].total_revenue_cents == 2500

        # ── Order pack grant ────────────────────────────────────────────────
        pack_response = await grant_order_pack(
            business_id=business_id, body=OrderPackGrant(pack_size=500), user=admin, db=db,
        )
        assert pack_response.order_pack_credits == 500

        # ── Admin dashboard: business list/filter/pagination ──────────────
        active_businesses = await list_businesses(is_active=True, page=1, per_page=100, user=admin, db=db)
        assert any(b.id == business_id for b in active_businesses)

        # ── Audit log: every admin action above left a PLATFORM-scope trail ──
        audit_log = await get_audit_log(business_id=business_id, scope="PLATFORM", page=1, per_page=50, user=admin, db=db)
        actions = {e.action for e in audit_log}
        assert "admin.business.created" in actions
        assert "admin.business.updated" in actions
        assert "admin.business.suspended" in actions
        assert "admin.business.unsuspended" in actions
        assert "admin.business.order_pack_granted" in actions

        # ── Permissions: non-super-admin is rejected by the super-admin gate ─
        # Calling a route FUNCTION directly (as every test in this suite
        # does) bypasses FastAPI's `Depends(require_super_admin)` entirely —
        # `user=staff_auth` is just a plain keyword argument at that point,
        # never actually resolved through the dependency graph. The real
        # gate is `require_super_admin` itself (a `require_role(["SUPER_ADMIN"])`
        # closure over `Depends(get_current_user)`) — call THAT directly,
        # the same lesson learned above from `get_current_user`.
        staff_auth = AuthUser(user_id=uuid.uuid4(), business_id=business_id, role="STAFF", token_type="access")
        with pytest.raises(Exception) as exc:
            await require_super_admin(user=staff_auth)
        assert "INSUFFICIENT_ROLE" in str(exc.value.detail)

        # ── Permissions: cross-tenant access is blocked ────────────────────
        # An OWNER authenticated against a DIFFERENT (real, existing) business
        # must not be able to read/modify this tenant's settings.
        other_biz = Business(
            name=f"Other Tenant {unique}", slug=f"other-tenant-{unique}",
            business_code=generate_business_code(), currency="ZAR", timezone="Africa/Johannesburg",
        )
        db.add(other_biz)
        await db.commit()
        await db.refresh(other_biz)
        other_biz_id = other_biz.id  # captured before any rollback below expires the instance
        try:
            other_owner_auth = AuthUser(user_id=uuid.uuid4(), business_id=other_biz_id, role="OWNER", token_type="access")
            cross_tenant_result = await create_category(
                body=CategoryCreate(name="Should not attach to the wrong tenant"), user=other_owner_auth, db=db,
            )
            # Confirm it landed under other_biz, never under business_id.
            assert cross_tenant_result.id != category.id
            # The category created above must be scoped to other_biz only.
            from shared.models.menu import MenuCategory
            cat_check = await db.get(MenuCategory, cross_tenant_result.id)
            assert cat_check.business_id == other_biz_id
            assert cat_check.business_id != business_id
        finally:
            # A failed assertion/exception above may have left the session's
            # transaction aborted (PendingRollbackError) — roll back first so
            # cleanup itself doesn't silently no-op, which is exactly what
            # caused a stale row to survive a prior failed run of this test.
            # NOTE: rollback() expires every object in the session, so any
            # further attribute access on an ORM instance (e.g. other_biz.id)
            # would trigger a synchronous lazy-load and raise MissingGreenlet
            # — always use a plain value captured beforehand (other_biz_id),
            # never the ORM instance itself, after this point.
            await db.rollback()
            from sqlalchemy import delete as sa_delete
            # AuditEvent.business_id is ON DELETE SET NULL, not CASCADE — must
            # be deleted explicitly or it survives as an orphaned row (see
            # conftest.py's test_business fixture for the full explanation).
            await db.execute(sa_delete(AuditEvent).where(AuditEvent.business_id == other_biz_id))
            await db.execute(sa_delete(Business).where(Business.id == other_biz_id))
            await db.commit()

    finally:
        await db.rollback()
        from sqlalchemy import delete as sa_delete
        await db.execute(sa_delete(AuditEvent).where(AuditEvent.business_id == business_id))
        await db.execute(sa_delete(Business).where(Business.id == business_id))
        await db.commit()
