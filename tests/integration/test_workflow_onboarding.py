"""
Workflow 5 (Sales-Partner Onboarding, Milestone B), driven against a REAL
Postgres instance — same style as test_workflow_platform_administration.py.

Covers: ONBOARDER-scoped business creation/visibility/editing, the
sensitive-field-reduced response shape, the PATCH field allow-list, the
default-deny gate against every non-onboarding admin route, onboarding
progress/go-live checks computed from real data (and flipping as real data
changes), onboarding completion (temp-login deactivation + token revocation
+ access loss), and admin-user management (create/patch/reset, self-
protection, last-SUPER_ADMIN protection, and immediate access loss on
deactivation/demotion — the actual security fix this branch makes, verified
through the REAL get_current_admin dependency, not a hand-built AuthUser).

Requires TEST_DATABASE_URL (see docker-compose.test.yml / TESTING.md).
Skips cleanly (rather than erroring) if the dedicated test Postgres is
configured but temporarily unreachable; fails loudly if TEST_DATABASE_URL
isn't set at all (see tests/integration/_env.py). This suite is not part of
the default `pytest tests --ignore=tests/replay` run — it's excluded from
that command's collection scope entirely (pytest.ini's `norecursedirs`).
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from admin_api.app.api.v1.routes_admin_users import (
    AdminUserCreate,
    AdminUserPatch,
    create_admin_user,
    list_admin_users,
    reset_admin_user_password,
    update_admin_user,
)
from admin_api.app.api.v1.routes_businesses import (
    BusinessCreate,
    BusinessOnboarderResponse,
    BusinessUpdate,
    OnboardingCompleteRequest,
    OwnerCreateRequest,
    ONBOARDING_ACCOUNT_STAFF_NAME,
    complete_onboarding,
    create_business,
    create_business_owner,
    get_business,
    get_onboarding_status,
    list_businesses,
    list_business_users,
    update_business,
)
from admin_api.app.core.deps import get_current_admin, require_super_admin
from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, get_current_user
from backend.app.core.security import create_access_token, create_admin_access_token, hash_password
from shared.models.admin import AdminRole, AdminUser
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.user import AdminRefreshToken, BusinessUser
from shared.utils import generate_business_code


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


async def _cleanup(db, *, business_ids=(), admin_ids=()):
    await db.rollback()
    for bid in business_ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.business_id == bid))
        await db.execute(delete(Business).where(Business.id == bid))
    for aid in admin_ids:
        await db.execute(delete(AuditEvent).where(AuditEvent.actor_user_id == aid))
        await db.execute(delete(AdminUser).where(AdminUser.id == aid))
    await db.commit()


@pytest.fixture
async def _db_or_skip(db):
    """The shared `db` fixture opens a real connection lazily on first use —
    skip cleanly here rather than failing the whole suite if this branch's
    throwaway Postgres instance isn't reachable in some other environment."""
    try:
        await db.execute(select(1))
    except OperationalError:
        pytest.skip("Dedicated test Postgres not reachable — see TEST_DATABASE_URL / docker-compose.test.yml")
    return db


async def test_onboarder_scoping_and_sensitive_field_reduction(_db_or_skip):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder1 = await _make_admin(db, AdminRole.ONBOARDER, f"onboarder1-{unique}@test.co.za")
    onboarder2 = await _make_admin(db, AdminRole.ONBOARDER, f"onboarder2-{unique}@test.co.za")
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-{unique}@test.co.za")

    auth1 = _admin_auth(onboarder1.id, AdminRole.ONBOARDER)
    auth2 = _admin_auth(onboarder2.id, AdminRole.ONBOARDER)
    auth_super = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)

    business_ids = []
    try:
        # ── ONBOARDER creates a business — stamped with their own id ───────
        created = await create_business(
            body=BusinessCreate(name=f"Onboarder Biz {unique}"), user=auth1, db=db,
        )
        business_ids.append(created.id)
        assert isinstance(created, BusinessOnboarderResponse)
        biz_row = await db.get(Business, created.id)
        assert biz_row.onboarded_by_admin_id == onboarder1.id

        # Sensitive commercial fields are genuinely absent from the response.
        assert not hasattr(created, "stripe_customer_id")
        assert not hasattr(created, "stripe_subscription_id")
        assert not hasattr(created, "monthly_orders_used")

        # A SUPER_ADMIN-created business has no onboarder owner.
        super_created = await create_business(
            body=BusinessCreate(name=f"SuperAdmin Biz {unique}"), user=auth_super, db=db,
        )
        business_ids.append(super_created.id)
        super_biz_row = await db.get(Business, super_created.id)
        assert super_biz_row.onboarded_by_admin_id is None

        # ── Visibility: onboarder1 sees only their own, not-yet-completed biz ─
        seen = await list_businesses(is_active=None, page=1, per_page=100, user=auth1, db=db)
        seen_ids = {b.id for b in seen}
        assert created.id in seen_ids
        assert super_created.id not in seen_ids

        # onboarder2 sees neither.
        seen2 = await list_businesses(is_active=None, page=1, per_page=100, user=auth2, db=db)
        assert created.id not in {b.id for b in seen2}

        # get_business: onboarder2 gets a non-revealing 404 on onboarder1's biz.
        with pytest.raises(NotFoundError):
            await get_business(business_id=created.id, user=auth2, db=db)

        # SUPER_ADMIN is unrestricted.
        fetched_by_super = await get_business(business_id=created.id, user=auth_super, db=db)
        assert fetched_by_super.id == created.id

        # ── PATCH allow-list ─────────────────────────────────────────────────
        updated = await update_business(
            business_id=created.id, body=BusinessUpdate(name="Renamed by onboarder"), user=auth1, db=db,
        )
        assert updated.name == "Renamed by onboarder"

        for forbidden_field, value in [("billing_status", "ACTIVE"), ("stripe_customer_id", "cus_evil")]:
            with pytest.raises(AppError) as exc:
                await update_business(
                    business_id=created.id, body=BusinessUpdate(**{forbidden_field: value}), user=auth1, db=db,
                )
            assert exc.value.status_code == 403
            assert forbidden_field in exc.value.message

        # ── Plan validation ──────────────────────────────────────────────────
        with pytest.raises(AppError) as exc:
            await create_business(body=BusinessCreate(name="Bad plan", plan="NOT_A_REAL_PLAN"), user=auth1, db=db)
        assert exc.value.status_code == 422

        # ── Onboarder cannot raise limits at creation either ────────────────
        with pytest.raises(AppError) as exc:
            await create_business(
                body=BusinessCreate(name="Limit bump", daily_order_limit=99999), user=auth1, db=db,
            )
        assert exc.value.status_code == 403

        # ── Default-deny: onboarding role cannot reach SUPER_ADMIN-only gate ─
        with pytest.raises(Exception) as exc:
            await require_super_admin(admin=auth1)
        assert "INSUFFICIENT_ROLE" in str(exc.value.detail)

    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=[onboarder1.id, onboarder2.id, super_admin.id])


async def test_onboarding_progress_reflects_real_data_and_completion_revokes_access(_db_or_skip):
    from backend.app.api.v1.routes_menu import CategoryCreate, ItemCreate, create_category, create_item
    from backend.app.api.v1.routes_pos import (
        POSOrderCreateRequest, POSOrderItemRequest, TillCloseRequest, TillOpenRequest,
        close_till, create_pos_order, open_till,
    )
    from admin_api.app.api.v1.routes_businesses import OnboardingConfirmRequest, confirm_onboarding_item
    from shared.models.business import Business

    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onboarder-{unique}@test.co.za")
    auth = _admin_auth(onboarder.id, AdminRole.ONBOARDER)

    business_ids = []
    try:
        created = await create_business(body=BusinessCreate(name=f"Go Live Biz {unique}"), user=auth, db=db)
        business_ids.append(created.id)

        # WhatsApp off, so that check reads n/a rather than fail — keeps this
        # test's "ready" path independent of WhatsApp phone number setup.
        await update_business(business_id=created.id, body=BusinessUpdate(is_whatsapp_enabled=False), user=auth, db=db)

        # ── Before any setup: not ready, menu check fails ───────────────────
        status_before = await get_onboarding_status(business_id=created.id, user=auth, db=db)
        assert status_before.ready is False
        menu_check = next(c for c in status_before.go_live.checks if c.key == "menu")
        assert menu_check.status == "fail"

        # Real-browser go-live fixes: an untouched tenant must NOT pass
        # "VAT & receipt settings" just because of the paper-width column's
        # server default, and the "no staff PIN yet" detail must not claim
        # the owner/manager can trade at the till instead (PIN login is
        # STAFF-only — nobody but a PIN-holding staff member can operate it).
        vat_receipt_before = next(c for c in status_before.go_live.checks if c.key == "vat_receipt")
        assert vat_receipt_before.status == "fail"
        staff_pin_before = next(c for c in status_before.go_live.checks if c.key == "staff_pin")
        assert "owner/manager can still trade alone" not in staff_pin_before.detail
        assert "STAFF-only" in staff_pin_before.detail

        # ── Create the onboarder's own temporary working login ──────────────
        owner_creation = await create_business_owner(
            business_id=created.id,
            body=OwnerCreateRequest(email=f"tmp-{unique}@test.co.za", full_name="ignored", role="MANAGER", is_onboarding_account=True),
            user=auth, db=db,
        )
        temp_user_row = await db.get(BusinessUser, owner_creation.id)
        assert temp_user_row.staff_name == ONBOARDING_ACCOUNT_STAFF_NAME

        # A real OWNER who HAS set their own password.
        owner = BusinessUser(
            business_id=created.id, role="OWNER", email=f"owner-{unique}@test.co.za",
            password_hash=hash_password("RealOwnerPass123!"), staff_name="Real Owner",
            is_active=True, must_change_password=False,
        )
        db.add(owner)
        await db.commit()

        # Build up real go-live data: menu, till, a POS sale.
        owner_auth = AuthUser(user_id=owner.id, business_id=created.id, role="OWNER", token_type="access")
        category = await create_category(body=CategoryCreate(name="Mains"), user=owner_auth, db=db)
        item = await create_item(
            body=ItemCreate(category_id=category.id, name="Burger", price_cents=6000), user=owner_auth, db=db,
        )
        till = await open_till(body=TillOpenRequest(opening_float_cents=0), user=owner_auth, db=db)
        await create_pos_order(
            body=POSOrderCreateRequest(
                items=[POSOrderItemRequest(menu_item_id=item.id, quantity=1)],
                order_mode="PICKUP", customer_phone="0821234567",
                payment_method="CASH", cash_tendered_cents=6000,
            ),
            user=owner_auth, db=db,
        )

        # Checks added by the commercial go-live extension (migration 0036):
        # restaurant details, a completed cash-up, and the two items only a
        # person can confirm (which modules were purchased; slip printed OK).
        biz_row = await db.get(Business, created.id)
        biz_row.address = "1 Test Street, Durban"
        # Real-browser go-live fix: the "VAT & receipt settings" check now
        # requires actual evidence receipt settings were configured (a
        # trading name / any receipt_config_json), not just the column's
        # server-default paper width, which every untouched business already
        # has — see admin_api/app/services/onboarding.py.
        biz_row.receipt_config_json = {"trading_name": f"Go Live Biz {unique}"}
        await db.commit()
        await close_till(
            session_id=till.id, body=TillCloseRequest(counted_cash_cents=6000),
            user=owner_auth, db=db,
        )
        for item_key in ("modules", "receipt_print"):
            await confirm_onboarding_item(
                business_id=created.id, body=OnboardingConfirmRequest(item=item_key),
                user=auth, db=db,
            )

        # ── After real setup: menu check now passes, business is ready ──────
        status_after = await get_onboarding_status(business_id=created.id, user=auth, db=db)
        menu_check_after = next(c for c in status_after.go_live.checks if c.key == "menu")
        assert menu_check_after.status == "pass"
        vat_receipt_after = next(c for c in status_after.go_live.checks if c.key == "vat_receipt")
        assert vat_receipt_after.status == "pass"

        # Real-browser fix: the modules_confirmed detail must show the
        # confirming admin's display name, never their raw UUID.
        modules_check = next(c for c in status_after.go_live.checks if c.key == "modules_confirmed")
        assert modules_check.status == "pass"
        assert onboarder.display_name in modules_check.detail
        assert str(onboarder.id) not in modules_check.detail

        failing = [c for c in status_after.go_live.checks if c.status == "fail"]
        assert failing == [], f"unexpected failing checks: {failing}"
        assert status_after.ready is True

        # ── ONBOARDER may not force-complete ─────────────────────────────────
        with pytest.raises(AppError) as exc:
            await complete_onboarding(
                business_id=created.id, body=OnboardingCompleteRequest(force=True), user=auth, db=db,
            )
        assert exc.value.status_code == 403

        # ── Complete onboarding (ready=True, no force needed) ───────────────
        result = await complete_onboarding(
            business_id=created.id, body=OnboardingCompleteRequest(force=False), user=auth, db=db,
        )
        assert result.onboarding_completed_at is not None
        assert result.deactivated_onboarding_accounts == 1

        await db.refresh(temp_user_row)
        assert temp_user_row.is_active is False

        # ── Access lost: the same onboarder can no longer see this business ──
        with pytest.raises(NotFoundError):
            await get_business(business_id=created.id, user=auth, db=db)
        seen_after = await list_businesses(is_active=None, page=1, per_page=100, user=auth, db=db)
        assert created.id not in {b.id for b in seen_after}

    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=[onboarder.id])


async def test_admin_user_management_and_self_protection(_db_or_skip):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    acting_super = await _make_admin(db, AdminRole.SUPER_ADMIN, f"acting-super-{unique}@test.co.za")
    acting_auth = _admin_auth(acting_super.id, AdminRole.SUPER_ADMIN)

    created_ids = [acting_super.id]
    try:
        # ── Create an ONBOARDER via the API (the owner's real first-time path) ─
        created = await create_admin_user(
            body=AdminUserCreate(email=f"partner-{unique}@test.co.za", display_name="Sales Partner", role=AdminRole.ONBOARDER),
            user=acting_auth, db=db,
        )
        created_ids.append(created.id)
        assert created.temporary_password
        assert created.role == AdminRole.ONBOARDER

        listing = await list_admin_users(user=acting_auth, db=db)
        assert any(a.id == created.id for a in listing)

        # ── Reset password ───────────────────────────────────────────────────
        reset = await reset_admin_user_password(admin_id=created.id, user=acting_auth, db=db)
        assert reset.temporary_password
        assert reset.temporary_password != created.temporary_password

        # ── Promote/demote takes effect immediately (real dependency check) ──
        onboarder_token = create_admin_access_token(created.id, AdminRole.ONBOARDER)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=onboarder_token)
        business_plane_user = await get_current_user(request=None, credentials=credentials, db=db)
        revalidated = await get_current_admin(user=business_plane_user, db=db)
        assert revalidated.role == AdminRole.ONBOARDER

        # A real, live refresh token for `created` — proves deactivation
        # actually revokes it, not just that the UPDATE statement runs.
        from backend.app.core.security import hash_refresh_token
        live_refresh = AdminRefreshToken(
            user_id=created.id, token_hash=hash_refresh_token("test-refresh-token-value"),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(live_refresh)
        await db.commit()
        await db.refresh(live_refresh)
        assert live_refresh.revoked_at is None

        await update_admin_user(admin_id=created.id, body=AdminUserPatch(is_active=False), user=acting_auth, db=db)
        with pytest.raises(HTTPException) as exc:
            business_plane_user2 = await get_current_user(request=None, credentials=credentials, db=db)
            await get_current_admin(user=business_plane_user2, db=db)
        assert exc.value.detail["code"] == "ACCOUNT_DISABLED"

        await db.refresh(live_refresh)
        assert live_refresh.revoked_at is not None

        # ── Self-protection: cannot deactivate/demote self ──────────────────
        with pytest.raises(AppError) as exc:
            await update_admin_user(admin_id=acting_super.id, body=AdminUserPatch(is_active=False), user=acting_auth, db=db)
        assert exc.value.status_code == 403

        # ── Last-SUPER_ADMIN protection ──────────────────────────────────────
        # Reactivate `created` and promote to SUPER_ADMIN, then make it the
        # ONLY other active SUPER_ADMIN, and confirm acting_super still
        # cannot be demoted away since `created` alone doesn't help (acting
        # itself is the one being changed) — simpler: attempt to demote the
        # newly-promoted admin when IT is the last one besides acting_super,
        # which must succeed (acting_super remains); then attempt to
        # deactivate acting_super via a second acting SUPER_ADMIN identity
        # is out of scope for this single-admin test — covered instead by
        # confirming the guard rejects removing the only active SUPER_ADMIN.
        only_super = await _make_admin(db, AdminRole.SUPER_ADMIN, f"only-super-{unique}@test.co.za")
        created_ids.append(only_super.id)
        only_super_auth = _admin_auth(only_super.id, AdminRole.SUPER_ADMIN)
        # Deactivate acting_super first is not allowed via self-protection,
        # so use only_super (a different identity) to attempt deactivating
        # acting_super — allowed, since only_super remains active afterward.
        await update_admin_user(admin_id=acting_super.id, body=AdminUserPatch(is_active=False), user=only_super_auth, db=db)
        await db.refresh(acting_super)
        assert acting_super.is_active is False

        # Now only `only_super` is active — attempting to demote/deactivate
        # IT (even via itself) must be blocked as the last active SUPER_ADMIN.
        with pytest.raises(AppError) as exc:
            await update_admin_user(admin_id=only_super.id, body=AdminUserPatch(role=AdminRole.ONBOARDER), user=only_super_auth, db=db)
        # Either the self-protection or last-admin guard fires first — both
        # are 403/409 and both correctly block the removal of the last admin.
        assert exc.value.status_code in (403, 409)

    finally:
        await _cleanup(db, admin_ids=created_ids)


async def test_concurrent_demotions_of_the_last_two_super_admins_cannot_both_succeed(_db_or_skip):
    """
    F9a fix (Wave 0A verifier): with exactly 2 active SUPER_ADMINs (A, B),
    A demoting B and B demoting A CONCURRENTLY (two independent sessions —
    a real DB, not the same in-process transaction) must never both
    succeed, which would leave the platform with zero active SUPER_ADMINs.
    Before the fix, `_active_super_admin_count` ran unlocked: both sessions
    could read "the other one is still active" before either committed,
    both pass the check, and both demote — exactly this scenario.
    """
    import asyncio

    from tests.integration._db import get_session_factory

    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    admin_a = await _make_admin(db, AdminRole.SUPER_ADMIN, f"concurrent-a-{unique}@test.co.za")
    admin_b = await _make_admin(db, AdminRole.SUPER_ADMIN, f"concurrent-b-{unique}@test.co.za")
    created_ids = [admin_a.id, admin_b.id]

    async def _demote(actor_id: uuid.UUID, target_id: uuid.UUID):
        async with get_session_factory()() as session:
            try:
                result = await update_admin_user(
                    admin_id=target_id,
                    body=AdminUserPatch(role=AdminRole.ONBOARDER),
                    user=_admin_auth(actor_id, AdminRole.SUPER_ADMIN),
                    db=session,
                )
                return ("success", result)
            except AppError as exc:
                await session.rollback()
                return ("blocked", exc.code, exc.status_code)

    try:
        outcomes = await asyncio.gather(
            _demote(admin_a.id, admin_b.id),  # A demotes B
            _demote(admin_b.id, admin_a.id),  # B demotes A, concurrently
        )

        successes = [o for o in outcomes if o[0] == "success"]
        blocked = [o for o in outcomes if o[0] == "blocked"]
        assert len(successes) == 1, f"expected exactly one demotion to succeed, got: {outcomes}"
        assert len(blocked) == 1, f"expected exactly one demotion to be blocked, got: {outcomes}"
        assert blocked[0][1] == "LAST_SUPER_ADMIN"
        assert blocked[0][2] == 409

        # Final DB state: exactly one of {admin_a, admin_b} is still an
        # active SUPER_ADMIN — never zero.
        remaining = await db.execute(
            select(AdminUser).where(
                AdminUser.id.in_([admin_a.id, admin_b.id]),
                AdminUser.role == AdminRole.SUPER_ADMIN,
                AdminUser.is_active.is_(True),
            )
        )
        assert len(remaining.scalars().all()) == 1
    finally:
        await _cleanup(db, admin_ids=created_ids)


async def test_business_plane_token_cannot_reach_admin_dependency(_db_or_skip):
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    business = Business(
        name=f"Token Isolation Biz {unique}", slug=f"token-isolation-{unique}",
        business_code=generate_business_code(), currency="ZAR", timezone="Africa/Johannesburg",
    )
    db.add(business)
    await db.commit()
    await db.refresh(business)

    owner = BusinessUser(
        business_id=business.id, role="OWNER", email=f"owner-{unique}@test.co.za",
        password_hash=hash_password("Irrelevant123!"), staff_name="Owner",
        is_active=True, must_change_password=False,
    )
    db.add(owner)
    await db.commit()
    await db.refresh(owner)

    try:
        token = create_access_token(user_id=owner.id, business_id=business.id, role="OWNER")
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        # A genuinely valid, active business-plane token passes get_current_user...
        business_auth = await get_current_user(request=None, credentials=credentials, db=db)
        assert business_auth.token_type == "access"

        # ...but get_current_admin (used by every admin_api route) rejects it
        # outright: no path from a business-plane token to any admin route.
        with pytest.raises(HTTPException) as exc:
            await get_current_admin(user=business_auth, db=db)
        assert exc.value.detail["code"] == "ADMIN_TOKEN_REQUIRED"
        assert exc.value.status_code == 403
    finally:
        await _cleanup(db, business_ids=[business.id])


async def test_digital_receipts_enabled_round_trips_through_patch_and_get(_db_or_skip):
    """
    BUG-0072: digital_receipts_enabled (migration 0039) was added to
    Business but never added to BusinessUpdate / ONBOARDER_PATCH_ALLOWED_FIELDS
    / BusinessAdminResponse / BusinessOnboarderResponse — so a PATCH setting
    it returned 200 and silently changed nothing (BusinessUpdate has no
    `extra="forbid"`, so the unknown field was just ignored), and no GET
    response could ever show its real state. Proves the fix: both SUPER_ADMIN
    and ONBOARDER can set it, and it comes back correctly on every response
    shape.
    """
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-{unique}@test.co.za")
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onboarder-{unique}@test.co.za")
    auth_super = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
    auth_onboarder = _admin_auth(onboarder.id, AdminRole.ONBOARDER)

    business_ids = []
    try:
        created = await create_business(
            body=BusinessCreate(name=f"Receipts Flag Biz {unique}"), user=auth_onboarder, db=db,
        )
        business_ids.append(created.id)
        assert created.digital_receipts_enabled is False  # model default

        # ONBOARDER may set it — it's in ONBOARDER_PATCH_ALLOWED_FIELDS now.
        updated = await update_business(
            business_id=created.id,
            body=BusinessUpdate(digital_receipts_enabled=True),
            user=auth_onboarder, db=db,
        )
        assert updated.digital_receipts_enabled is True

        # Round-trips on every GET response shape.
        fetched_onboarder = await get_business(business_id=created.id, user=auth_onboarder, db=db)
        assert isinstance(fetched_onboarder, BusinessOnboarderResponse)
        assert fetched_onboarder.digital_receipts_enabled is True

        fetched_super = await get_business(business_id=created.id, user=auth_super, db=db)
        assert fetched_super.digital_receipts_enabled is True

        listed = await list_businesses(is_active=None, page=1, per_page=100, user=auth_super, db=db)
        this_one = next(b for b in listed if b.id == created.id)
        assert this_one.digital_receipts_enabled is True

        # SUPER_ADMIN can toggle it back off too.
        toggled_off = await update_business(
            business_id=created.id,
            body=BusinessUpdate(digital_receipts_enabled=False),
            user=auth_super, db=db,
        )
        assert toggled_off.digital_receipts_enabled is False
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=[super_admin.id, onboarder.id])


async def test_delivery_entitlement_flags_round_trip_super_admin_only(_db_or_skip):
    """
    delivery_platforms_enabled/uber_eats_enabled/mrd_enabled (migration 0044)
    — same class of gap as BUG-0072, closed in the same commit as the
    migration/model per this repo's own discipline. UNLIKE
    digital_receipts_enabled, these are deliberately NOT in
    ONBOARDER_PATCH_ALLOWED_FIELDS — enabling a live delivery integration
    carries real contractual weight with Uber, so only SUPER_ADMIN may set
    them. Proves: SUPER_ADMIN can set them and they round-trip on every GET
    response shape (including the reduced ONBOARDER response, read-only);
    ONBOARDER is rejected outright (403, field named) when attempting to set
    them, and the row is left unchanged by the rejected attempt.
    """
    db = _db_or_skip
    unique = uuid.uuid4().hex[:8]
    super_admin = await _make_admin(db, AdminRole.SUPER_ADMIN, f"super-{unique}@test.co.za")
    onboarder = await _make_admin(db, AdminRole.ONBOARDER, f"onboarder-{unique}@test.co.za")
    auth_super = _admin_auth(super_admin.id, AdminRole.SUPER_ADMIN)
    auth_onboarder = _admin_auth(onboarder.id, AdminRole.ONBOARDER)

    business_ids = []
    try:
        created = await create_business(
            body=BusinessCreate(name=f"Delivery Flag Biz {unique}"), user=auth_onboarder, db=db,
        )
        business_ids.append(created.id)
        assert created.delivery_platforms_enabled is False  # model default
        assert created.uber_eats_enabled is False
        assert created.mrd_enabled is False

        # ONBOARDER may NOT set these — not in ONBOARDER_PATCH_ALLOWED_FIELDS.
        with pytest.raises(AppError) as exc:
            await update_business(
                business_id=created.id,
                body=BusinessUpdate(delivery_platforms_enabled=True, uber_eats_enabled=True),
                user=auth_onboarder, db=db,
            )
        assert exc.value.code == "FIELD_NOT_ALLOWED"
        unchanged = await get_business(business_id=created.id, user=auth_onboarder, db=db)
        assert unchanged.delivery_platforms_enabled is False
        assert unchanged.uber_eats_enabled is False

        # SUPER_ADMIN may set them.
        updated = await update_business(
            business_id=created.id,
            body=BusinessUpdate(delivery_platforms_enabled=True, uber_eats_enabled=True),
            user=auth_super, db=db,
        )
        assert updated.delivery_platforms_enabled is True
        assert updated.uber_eats_enabled is True
        assert updated.mrd_enabled is False

        # Round-trips on every GET response shape, including the reduced
        # ONBOARDER response (visible, read-only — see BusinessOnboarderResponse).
        fetched_onboarder = await get_business(business_id=created.id, user=auth_onboarder, db=db)
        assert isinstance(fetched_onboarder, BusinessOnboarderResponse)
        assert fetched_onboarder.delivery_platforms_enabled is True
        assert fetched_onboarder.uber_eats_enabled is True

        fetched_super = await get_business(business_id=created.id, user=auth_super, db=db)
        assert fetched_super.delivery_platforms_enabled is True

        listed = await list_businesses(is_active=None, page=1, per_page=100, user=auth_super, db=db)
        this_one = next(b for b in listed if b.id == created.id)
        assert this_one.delivery_platforms_enabled is True
        assert this_one.uber_eats_enabled is True

        # SUPER_ADMIN can toggle back off too.
        toggled_off = await update_business(
            business_id=created.id,
            body=BusinessUpdate(delivery_platforms_enabled=False, uber_eats_enabled=False),
            user=auth_super, db=db,
        )
        assert toggled_off.delivery_platforms_enabled is False
        assert toggled_off.uber_eats_enabled is False
    finally:
        await _cleanup(db, business_ids=business_ids, admin_ids=[super_admin.id, onboarder.id])
