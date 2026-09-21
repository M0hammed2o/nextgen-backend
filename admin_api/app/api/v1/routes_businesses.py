"""
Admin business management — CRUD, suspend/unsuspend, set limits,
create owner login, WhatsApp test send, sales-partner onboarding.

Most routes here stay SUPER_ADMIN-only (default-deny). A small explicit
allow-list (see require_onboarding_role usages below) also accepts the
ONBOARDER role, scoped to businesses that admin personally onboarded and
only until that business's onboarding is marked complete — see
admin_api/app/core/deps.py::get_business_scoped, the single helper every
one of those routes uses.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from admin_api.app.core.deps import (
    get_business_commercial_scoped,
    get_business_scoped,
    require_onboarding_role,
    require_super_admin,
)
from admin_api.app.services.onboarding import GoLiveResult, compute_go_live_checks
from backend.app.api.v1.routes_setup import SetupProgress, compute_setup_progress
from backend.app.billing.trials import extend_trial, start_trial
from backend.app.core.errors import AppError, DuplicateError, NotFoundError
from backend.app.core.rbac import AuthUser
from backend.app.core.security import hash_password
from backend.app.db.session import get_db
from shared.enums import AuditScope, PlanTier
from shared.models.admin import AdminRole
from shared.models.audit import AuditEvent
from shared.models.business import Business
from shared.models.user import BusinessUser, RefreshToken
from shared.utils import generate_business_code, generate_temp_password

logger = logging.getLogger("nextgen.admin")
router = APIRouter(prefix="/admin/businesses", tags=["admin-businesses"])

# Reserved staff_name marking an onboarder's own temporary MANAGER login on
# `business_users` — see OwnerCreateRequest.is_onboarding_account below. No
# suitable dedicated column exists on business_users, and the task scope for
# this branch permits no second migration, so this convention (rather than a
# new column) is how POST .../onboarding/complete finds and deactivates it.
# See this task's DOC NOTE for DECISIONS.md (memory files are not edited
# directly by this branch — see CLAUDE.md's task instructions).
ONBOARDING_ACCOUNT_STAFF_NAME = "NextGen Onboarding"

# Fields an ONBOARDER may change via PATCH /admin/businesses/{id}. Everything
# else (billing_status, stripe_*, limits, is_active) is default-deny for
# that role — SUPER_ADMIN is unaffected by this list.
ONBOARDER_PATCH_ALLOWED_FIELDS = {
    "name", "timezone", "plan", "whatsapp_phone_number_id",
    "is_whatsapp_enabled", "inventory_enabled",
    "staff_live_orders_tab_enabled", "staff_wastage_tab_enabled", "staff_stocktake_tab_enabled",
}

# Daily limits an ONBOARDER must not raise/lower at creation time either —
# same "NOT limits" invariant as the PATCH allow-list above, applied
# symmetrically to POST so an ONBOARDER can't achieve the same effect by
# setting a non-default value on create instead of PATCH.
_DEFAULT_DAILY_LIMITS = {"daily_message_limit": 800, "daily_llm_call_limit": 400, "daily_order_limit": 200}


# ── Schemas ──────────────────────────────────────────────────────────────────

class BusinessCreate(BaseModel):
    name: str = Field(max_length=255)
    slug: str | None = Field(default=None, max_length=255, pattern=r"^[a-z0-9\-]*$")
    timezone: str = "Africa/Johannesburg"
    plan: str = "STARTER"
    whatsapp_phone_number_id: str | None = None
    daily_message_limit: int = 800
    daily_llm_call_limit: int = 400
    daily_order_limit: int = 200
    # Optional: create owner user in the same transaction.
    # No password field — the system always generates a temporary password
    # (see owner_temporary_password on BusinessAdminResponse) and the owner
    # must set a real one via POST /auth/set-password before they can log in.
    owner_email: EmailStr | None = None
    owner_full_name: str | None = Field(default=None, max_length=255)
    start_trial: bool = Field(
        default=True,
        description="Start a commercial trial (TRIAL_DEFAULT_DAYS, config) immediately. "
                     "When false, the business is created as ACTIVE with no trial — for "
                     "a client invoiced/paying from day one with no trial period.",
    )


class BusinessUpdate(BaseModel):
    name: str | None = None
    timezone: str | None = None
    plan: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_business_account_id: str | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    billing_status: str | None = None

    # ── Per-tenant module switches (Ciao Pizza P0) ───────────────────────
    # These belong on the ADMIN plane, not the business plane: which modules
    # a restaurant has bought is a platform/commercial decision, not
    # something a tenant toggles for itself. This is what lets NextGen sell
    # POS+Inventory to Ciao Pizza with WhatsApp off, while another tenant
    # keeps WhatsApp on — without either affecting the other.
    is_whatsapp_enabled: bool | None = None
    inventory_enabled: bool | None = None
    # Staff app navigation — set during onboarding so a new tenant's till
    # app matches their workflow on day one. Also settable by the owner via
    # the business plane; these hide tabs only, never features.
    staff_live_orders_tab_enabled: bool | None = None
    staff_wastage_tab_enabled: bool | None = None
    staff_stocktake_tab_enabled: bool | None = None


class LimitsUpdate(BaseModel):
    daily_message_limit: int | None = Field(default=None, ge=0)
    daily_llm_call_limit: int | None = Field(default=None, ge=0)
    daily_order_limit: int | None = Field(default=None, ge=0)


class SuspendRequest(BaseModel):
    reason: str = Field(max_length=512)


class BusinessAdminResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    business_code: str
    is_active: bool
    suspended_reason: str | None
    timezone: str
    plan: str
    billing_status: str
    currency: str
    whatsapp_phone_number_id: str | None
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    daily_message_limit: int
    daily_llm_call_limit: int
    daily_order_limit: int
    monthly_order_quota: int = 1500
    order_pack_credits: int = 0
    monthly_orders_used: int = 0
    monthly_orders_remaining: int = 1500
    last_webhook_received_at: datetime | None
    created_at: datetime
    # Module flags — were accepted by BusinessUpdate but never returned,
    # so the admin UI showed "Unknown" for all four (found while wiring the
    # onboarding go-live checks — see this task's DOC NOTE for KNOWN_BUGS.md).
    is_whatsapp_enabled: bool
    inventory_enabled: bool
    staff_live_orders_tab_enabled: bool
    staff_wastage_tab_enabled: bool
    staff_stocktake_tab_enabled: bool
    onboarded_by_admin_id: uuid.UUID | None
    onboarding_completed_at: datetime | None
    # Populated only by POST /admin/businesses when owner_email was provided —
    # shown once, never returned by GET/PATCH.
    owner_temporary_password: str | None = None
    model_config = {"from_attributes": True}


class BusinessOnboarderResponse(BaseModel):
    """
    Reduced business view for ONBOARDER admins. A sales partner doing
    setup does not need Stripe/PayFast identifiers or usage/cost figures —
    those are NextGen's own commercial data, not onboarding data — so this
    is a genuinely smaller schema rather than the same fields nulled out.
    """
    id: uuid.UUID
    name: str
    slug: str
    business_code: str
    is_active: bool
    suspended_reason: str | None
    timezone: str
    plan: str
    billing_status: str
    currency: str
    whatsapp_phone_number_id: str | None
    daily_message_limit: int
    daily_llm_call_limit: int
    daily_order_limit: int
    last_webhook_received_at: datetime | None
    created_at: datetime
    is_whatsapp_enabled: bool
    inventory_enabled: bool
    staff_live_orders_tab_enabled: bool
    staff_wastage_tab_enabled: bool
    staff_stocktake_tab_enabled: bool
    onboarded_by_admin_id: uuid.UUID | None
    onboarding_completed_at: datetime | None
    owner_temporary_password: str | None = None
    model_config = {"from_attributes": True}


def _business_response(biz: Business, role: str) -> "BusinessAdminResponse | BusinessOnboarderResponse":
    """The one place that decides which of the two response shapes a caller sees."""
    if role == AdminRole.ONBOARDER:
        return BusinessOnboarderResponse.model_validate(biz)
    return BusinessAdminResponse.model_validate(biz)


class OwnerCreateRequest(BaseModel):
    """
    Create an owner/manager login for a business.
    No password field — the system always generates a temporary password
    (returned once as OwnerResponse.temporary_password) and the owner must
    set a real one via POST /auth/set-password before they can log in.
    """
    email: EmailStr
    full_name: str = Field(max_length=255)
    role: str = Field(default="OWNER", pattern=r"^(OWNER|MANAGER)$")
    is_onboarding_account: bool = Field(
        default=False,
        description="Mark this as the onboarder's own temporary working login "
                     "(NextGen Onboarding), not a real restaurant staff member. "
                     "Deactivated automatically by POST .../onboarding/complete. "
                     "Must be role=MANAGER.",
    )


class OwnerResponse(BaseModel):
    id: uuid.UUID
    email: str
    staff_name: str | None
    role: str
    business_id: uuid.UUID
    is_active: bool
    created_at: datetime
    # Shown ONCE, in this creation response only — never persisted in plaintext,
    # never returned by any other endpoint.
    temporary_password: str
    model_config = {"from_attributes": True}


class WhatsAppTestSendRequest(BaseModel):
    """Send a test WhatsApp message to verify outbound works."""
    to: str = Field(
        description="Recipient phone number in international format, e.g. 27612345678",
        pattern=r"^\d{10,15}$",
    )
    text: str = Field(
        default="Hello from NextGen test 🚀",
        max_length=4096,
    )


class WhatsAppTestSendResponse(BaseModel):
    success: bool
    wa_message_id: str | None = None
    error: str | None = None
    phone_number_id: str


# ── Existing CRUD Routes (unchanged) ────────────────────────────────────────

@router.get("", response_model=None)
async def list_businesses(
    is_active: bool | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25, ge=1, le=100),
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
) -> list[BusinessAdminResponse] | list[BusinessOnboarderResponse]:
    """SUPER_ADMIN sees every business. ONBOARDER sees only businesses they
    personally onboarded whose onboarding isn't complete yet — see
    get_business_scoped's docstring for the full rule."""
    query = select(Business)
    if is_active is not None:
        query = query.where(Business.is_active == is_active)
    if user.role == AdminRole.ONBOARDER:
        query = query.where(
            Business.onboarded_by_admin_id == user.user_id,
            Business.onboarding_completed_at.is_(None),
        )
    query = query.order_by(Business.created_at.desc())
    query = query.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    return [_business_response(b, user.role) for b in result.scalars().all()]


@router.post("", response_model=None, status_code=201)
async def create_business(
    body: BusinessCreate,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
) -> BusinessAdminResponse | BusinessOnboarderResponse:
    if user.role == AdminRole.ONBOARDER:
        if body.plan not in (t.value for t in PlanTier):
            raise AppError(
                "INVALID_PLAN",
                f"plan must be one of: {', '.join(t.value for t in PlanTier)}",
                422,
            )
        for field, default in _DEFAULT_DAILY_LIMITS.items():
            if getattr(body, field) != default:
                raise AppError("FIELD_NOT_ALLOWED", f"ONBOARDER cannot set {field}", 403)

    # Auto-generate slug from name if not provided
    import re as _re
    slug = body.slug
    if not slug:
        slug = _re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")
        if not slug:
            slug = "business"

    # Ensure slug uniqueness — append suffix if needed
    base_slug = slug
    for suffix in range(20):
        candidate = base_slug if suffix == 0 else f"{base_slug}-{suffix}"
        existing = await db.execute(
            select(Business.id).where(Business.slug == candidate)
        )
        if not existing.scalar_one_or_none():
            slug = candidate
            break
    else:
        raise AppError("SLUG_GENERATION_FAILED", "Could not generate unique slug", 500)
    body_slug = slug

    # If owner_email provided, check email uniqueness
    if body.owner_email:
        email_check = await db.execute(
            select(BusinessUser.id).where(
                BusinessUser.email == body.owner_email.lower().strip()
            )
        )
        if email_check.scalar_one_or_none():
            raise DuplicateError("User", "email")

    # Generate unique business code
    for _ in range(10):
        code = generate_business_code()
        check = await db.execute(
            select(Business.id).where(Business.business_code == code)
        )
        if not check.scalar_one_or_none():
            break
    else:
        raise AppError("CODE_GENERATION_FAILED", "Could not generate unique business code", 500)

    # Create business
    biz_data = body.model_dump(exclude={"owner_email", "owner_full_name", "slug", "start_trial"})
    biz = Business(
        business_code=code, slug=body_slug,
        onboarded_by_admin_id=user.user_id if user.role == AdminRole.ONBOARDER else None,
        **biz_data,
    )
    if body.start_trial:
        start_trial(biz)
    else:
        # No trial period — e.g. a client already invoiced/paying from day
        # one. Explicit ACTIVE rather than leaving the model's default
        # billing_status="TRIAL" with no trial_ends_at set, which would
        # otherwise read as an immediately-EXPIRED trial via
        # compute_account_status (see backend/app/billing/account_status.py).
        biz.billing_status = "ACTIVE"
    db.add(biz)
    await db.flush()  # Get biz.id before creating user

    # Optionally create owner user in same transaction, with a generated
    # temporary password — same policy as POST /admin/businesses/{id}/owner.
    owner_temp_password: str | None = None
    if body.owner_email:
        owner_temp_password = generate_temp_password()
        owner = BusinessUser(
            business_id=biz.id,
            role="OWNER",
            email=body.owner_email.lower().strip(),
            password_hash=hash_password(owner_temp_password),
            staff_name=body.owner_full_name,
            is_active=True,
            must_change_password=True,
        )
        db.add(owner)

    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.created",
        target_type="business",
        target_id=biz.id,
        diff_json={"name": biz.name, "plan": biz.plan, "owner_email_provided": bool(body.owner_email)},
    ))
    await db.commit()
    await db.refresh(biz)

    response = _business_response(biz, user.role)
    response.owner_temporary_password = owner_temp_password
    return response


@router.get("/{business_id}", response_model=None)
async def get_business(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
) -> BusinessAdminResponse | BusinessOnboarderResponse:
    biz = await get_business_scoped(business_id, user, db)

    if user.role == AdminRole.ONBOARDER:
        return _business_response(biz, user.role)

    from backend.app.billing.usage import get_order_quota_status
    quota = await get_order_quota_status(db, biz)

    response = BusinessAdminResponse.model_validate(biz)
    response.monthly_orders_used = quota.used
    response.monthly_orders_remaining = quota.remaining
    return response


class OrderPackGrant(BaseModel):
    pack_size: int = Field(ge=1, description="e.g. 500/1000/2500/5000, but not restricted to those exact values")


@router.post("/{business_id}/order-pack", response_model=BusinessAdminResponse)
async def grant_order_pack(
    business_id: uuid.UUID,
    body: OrderPackGrant,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Grant additional order-pack credits for the current billing period.

    Admin-operated for now, not customer self-service — the order-pack
    purchase flow isn't wired to any payment provider yet (NextGen's own
    SaaS-billing provider is itself unresolved, see DECISIONS.md
    DECISION-0014), so this is the honest interim: a super admin action,
    same pattern as the existing manual plan/billing-field editor.
    """
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    biz.order_pack_credits += body.pack_size
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.order_pack_granted",
        target_type="business",
        target_id=biz.id,
        diff_json={"pack_size": body.pack_size, "new_total_credits": biz.order_pack_credits},
    ))
    await db.commit()
    await db.refresh(biz)

    from backend.app.billing.usage import get_order_quota_status
    quota = await get_order_quota_status(db, biz)
    response = BusinessAdminResponse.model_validate(biz)
    response.monthly_orders_used = quota.used
    response.monthly_orders_remaining = quota.remaining
    return response


@router.patch("/{business_id}", response_model=None)
async def update_business(
    business_id: uuid.UUID,
    body: BusinessUpdate,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
) -> BusinessAdminResponse | BusinessOnboarderResponse:
    biz = await get_business_scoped(business_id, user, db)

    changes = body.model_dump(exclude_unset=True)

    if user.role == AdminRole.ONBOARDER:
        disallowed = set(changes) - ONBOARDER_PATCH_ALLOWED_FIELDS
        if disallowed:
            raise AppError(
                "FIELD_NOT_ALLOWED",
                f"ONBOARDER cannot set: {', '.join(sorted(disallowed))}",
                403,
            )
        if "plan" in changes and changes["plan"] != biz.plan and changes["plan"] not in (t.value for t in PlanTier):
            raise AppError(
                "INVALID_PLAN",
                f"plan must be one of: {', '.join(t.value for t in PlanTier)}",
                422,
            )

    for field, value in changes.items():
        setattr(biz, field, value)
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.updated",
        target_type="business",
        target_id=biz.id,
        diff_json=changes,
    ))
    try:
        await db.commit()
    except IntegrityError as exc:
        # whatsapp_phone_number_id/stripe_customer_id/stripe_subscription_id
        # are UNIQUE-but-nullable columns -- setting one to a value another
        # business already has (including two businesses both set to the
        # same non-null "") previously crashed this endpoint with an
        # unhandled 500 and no JSON error body. Found via the admin-portal
        # browser test against real (pre-existing, unrelated) dev data, not
        # by guessing. Roll back and return a normal, actionable 409 instead.
        await db.rollback()
        raise DuplicateError("business field", "value you entered") from exc
    await db.refresh(biz)
    return _business_response(biz, user.role)


@router.post("/{business_id}/suspend", response_model=BusinessAdminResponse)
async def suspend_business(
    business_id: uuid.UUID,
    body: SuspendRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    biz.is_active = False
    biz.suspended_reason = body.reason
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.suspended",
        target_type="business",
        target_id=biz.id,
        diff_json={"reason": body.reason},
    ))
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.post("/{business_id}/unsuspend", response_model=BusinessAdminResponse)
async def unsuspend_business(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    previous_reason = biz.suspended_reason
    biz.is_active = True
    biz.suspended_reason = None
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.unsuspended",
        target_type="business",
        target_id=biz.id,
        diff_json={"previous_suspended_reason": previous_reason},
    ))
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


@router.post("/{business_id}/limits", response_model=BusinessAdminResponse)
async def set_business_limits(
    business_id: uuid.UUID,
    body: LimitsUpdate,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(biz, field, value)
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.limits_updated",
        target_type="business",
        target_id=biz.id,
        diff_json=changes,
    ))
    await db.commit()
    await db.refresh(biz)
    return BusinessAdminResponse.model_validate(biz)


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Feature 1 — Create owner/manager login for a business
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/{business_id}/owner",
    response_model=OwnerResponse,
    status_code=201,
    summary="Create owner/manager login for a business",
)
async def create_business_owner(
    business_id: uuid.UUID,
    body: OwnerCreateRequest,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a BusinessUser with OWNER or MANAGER role linked to the business.
    Uses email + password auth (same as POST /auth/login on backend).
    """
    biz = await get_business_scoped(business_id, user, db)

    if body.is_onboarding_account and body.role != "MANAGER":
        raise AppError(
            "INVALID_ONBOARDING_ACCOUNT",
            "An onboarding working account must be role=MANAGER",
            422,
        )

    # Check email uniqueness (globally unique)
    email_lower = body.email.lower().strip()
    existing = await db.execute(
        select(BusinessUser.id).where(BusinessUser.email == email_lower)
    )
    if existing.scalar_one_or_none():
        raise DuplicateError("User", "email")

    temp_password = generate_temp_password()
    owner = BusinessUser(
        business_id=biz.id,
        role=body.role,
        email=email_lower,
        password_hash=hash_password(temp_password),
        staff_name=ONBOARDING_ACCOUNT_STAFF_NAME if body.is_onboarding_account else body.full_name,
        is_active=True,
        must_change_password=True,
    )
    db.add(owner)
    await db.flush()
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=business_id,
        actor_user_id=user.user_id,
        action="admin.business.owner_created",
        target_type="business_user",
        target_id=owner.id,
        # Never log the temporary password itself — only metadata.
        diff_json={"email": email_lower, "role": body.role},
    ))
    await db.commit()
    await db.refresh(owner)

    # Transient attribute — not a mapped column, never persisted.
    owner.temporary_password = temp_password
    return OwnerResponse.model_validate(owner)


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Feature 3 — List and reset password for existing owner/manager logins
# ═══════════════════════════════════════════════════════════════════════════════

class BusinessUserSummary(BaseModel):
    id: uuid.UUID
    email: str | None
    staff_name: str | None
    role: str
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    model_config = {"from_attributes": True}


class ResetPasswordResponse(BaseModel):
    id: uuid.UUID
    email: str | None
    # Shown once, in this response only.
    temporary_password: str
    model_config = {"from_attributes": True}


@router.get(
    "/{business_id}/users",
    response_model=list[BusinessUserSummary],
    summary="List owner/manager logins for a business",
)
async def list_business_users(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    biz = await get_business_scoped(business_id, user, db)

    result = await db.execute(
        select(BusinessUser)
        .where(
            BusinessUser.business_id == business_id,
            BusinessUser.role.in_(["OWNER", "MANAGER"]),
        )
        .order_by(BusinessUser.staff_name, BusinessUser.email)
    )
    return [BusinessUserSummary.model_validate(u) for u in result.scalars().all()]


@router.post(
    "/{business_id}/users/{user_id}/reset-password",
    response_model=ResetPasswordResponse,
    summary="Reset an owner/manager's password (generates a new temporary password)",
)
async def reset_business_user_password(
    business_id: uuid.UUID,
    user_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate a new temporary password for an existing OWNER/MANAGER login,
    force a reset via must_change_password (same flow as account creation),
    and revoke their live sessions — same pattern as staff deactivation.

    ONBOARDER may only do this while the business's onboarding is not yet
    complete — enforced by get_business_scoped below, same rule as every
    other onboarder-accessible route.
    """
    from sqlalchemy import update as sa_update

    await get_business_scoped(business_id, user, db)

    result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.id == user_id,
            BusinessUser.business_id == business_id,
            BusinessUser.role.in_(["OWNER", "MANAGER"]),
        )
    )
    target = result.scalar_one_or_none()
    if not target:
        raise NotFoundError("User", str(user_id))

    temp_password = generate_temp_password()
    target.password_hash = hash_password(temp_password)
    target.must_change_password = True
    target.failed_login_attempts = 0
    target.locked_until = None

    await db.execute(
        sa_update(RefreshToken)
        .where(RefreshToken.user_id == target.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=business_id,
        actor_user_id=user.user_id,
        action="admin.business.user_password_reset",
        target_type="business_user",
        target_id=target.id,
        # Never log the temporary password itself — only metadata.
        diff_json={"email": target.email},
    ))
    await db.commit()
    await db.refresh(target)

    target.temporary_password = temp_password
    return ResetPasswordResponse.model_validate(target)


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Milestone B — onboarding progress, go-live checks, and handover
# ═══════════════════════════════════════════════════════════════════════════════

class OnboardingStatusResponse(BaseModel):
    business_id: uuid.UUID
    business_name: str
    setup: SetupProgress
    go_live: GoLiveResult
    ready: bool


@router.get(
    "/{business_id}/onboarding",
    response_model=OnboardingStatusResponse,
    summary="Onboarding progress + go-live checks for a business",
)
async def get_onboarding_status(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Two computations, both derived from real data, never a manually-ticked
    checklist:
      - `setup`: the SAME owner-facing "how far have you configured your
        restaurant" progress shown on the owner dashboard
        (backend/app/api/v1/routes_setup.py::compute_setup_progress) —
        reused, not duplicated, so the two views can never disagree.
      - `go_live`: a stricter, admin-facing pass/fail/warn/n-a gate (see
        admin_api/app/services/onboarding.py) that also checks things the
        owner-facing progress view doesn't need to: a till has actually
        been opened, a POS order and its receipt actually render, billing
        status, etc.
    """
    biz = await get_business_scoped(business_id, user, db)
    setup = await compute_setup_progress(db, biz)
    go_live = await compute_go_live_checks(db, biz)
    return OnboardingStatusResponse(
        business_id=biz.id, business_name=biz.name,
        setup=setup, go_live=go_live, ready=go_live.ready,
    )


class OnboardingCompleteRequest(BaseModel):
    force: bool = Field(
        default=False,
        description="Complete onboarding even though a go-live check is failing. "
                     "SUPER_ADMIN only — an ONBOARDER may never force this.",
    )


class OnboardingCompleteResponse(BaseModel):
    business_id: uuid.UUID
    onboarding_completed_at: datetime
    forced: bool
    deactivated_onboarding_accounts: int


@router.post(
    "/{business_id}/onboarding/complete",
    response_model=OnboardingCompleteResponse,
    summary="Complete onboarding: hand the business over, revoke the onboarder's access",
)
async def complete_onboarding(
    business_id: uuid.UUID,
    body: OnboardingCompleteRequest,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Requires every go-live check to pass, unless `force=true` — which only
    SUPER_ADMIN may pass; an ONBOARDER attempting `force=true` is rejected
    outright, not silently downgraded to force=false.

    Deactivates the onboarder's own temporary working login(s) (identified
    by the reserved ONBOARDING_ACCOUNT_STAFF_NAME convention — see
    OwnerCreateRequest.is_onboarding_account) and revokes their refresh
    tokens, stamps onboarding_completed_at, and writes an audit event.
    From the next request onward, get_business_scoped denies this
    ONBOARDER access to this business (404) — SUPER_ADMIN is unaffected.
    """
    from sqlalchemy import update as sa_update

    biz = await get_business_scoped(business_id, user, db)

    if body.force and user.role != AdminRole.SUPER_ADMIN:
        raise AppError(
            "FORCE_NOT_ALLOWED",
            "Only SUPER_ADMIN may force-complete onboarding with failing go-live checks",
            403,
        )

    go_live = await compute_go_live_checks(db, biz)
    if not go_live.ready and not body.force:
        raise AppError(
            "NOT_READY",
            "This business has failing go-live checks — resolve them or pass force=true (SUPER_ADMIN only)",
            409,
            details={"failed": go_live.failed, "checks": [c.model_dump() for c in go_live.checks if c.status == "fail"]},
        )

    accounts_result = await db.execute(
        select(BusinessUser).where(
            BusinessUser.business_id == biz.id,
            BusinessUser.staff_name == ONBOARDING_ACCOUNT_STAFF_NAME,
            BusinessUser.role == "MANAGER",
            BusinessUser.is_active.is_(True),
        )
    )
    onboarding_accounts = accounts_result.scalars().all()
    now = datetime.now(timezone.utc)
    for account in onboarding_accounts:
        account.is_active = False
        await db.execute(
            sa_update(RefreshToken)
            .where(RefreshToken.user_id == account.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    biz.onboarding_completed_at = now
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.onboarding_completed",
        target_type="business",
        target_id=biz.id,
        diff_json={
            "forced": bool(body.force and not go_live.ready),
            "ready": go_live.ready,
            "deactivated_onboarding_accounts": len(onboarding_accounts),
        },
    ))
    await db.commit()
    await db.refresh(biz)

    return OnboardingCompleteResponse(
        business_id=biz.id,
        onboarding_completed_at=biz.onboarding_completed_at,
        forced=bool(body.force and not go_live.ready),
        deactivated_onboarding_accounts=len(onboarding_accounts),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Wave 0A — trial start/extend, manual go-live confirmations
#
# All three routes use get_business_commercial_scoped, NOT get_business_scoped
# — a sales partner keeps managing trials/payments for their own clients
# after handover (onboarding_completed_at set); only the CONFIGURATION
# routes above (which use get_business_scoped) lose ONBOARDER access then.
# ═══════════════════════════════════════════════════════════════════════════════

class TrialStartResponse(BaseModel):
    business_id: uuid.UUID
    billing_status: str
    trial_started_at: datetime | None
    trial_ends_at: datetime | None


@router.post(
    "/{business_id}/trial/start",
    response_model=TrialStartResponse,
    summary="Start (or restart) a commercial trial for an existing business",
)
async def start_business_trial(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    biz = await get_business_commercial_scoped(business_id, user, db)
    start_trial(biz)
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.trial_started",
        target_type="business",
        target_id=biz.id,
        diff_json={"trial_ends_at": biz.trial_ends_at.isoformat() if biz.trial_ends_at else None},
    ))
    await db.commit()
    await db.refresh(biz)
    return TrialStartResponse(
        business_id=biz.id, billing_status=biz.billing_status,
        trial_started_at=biz.trial_started_at, trial_ends_at=biz.trial_ends_at,
    )


class TrialExtendRequest(BaseModel):
    days: int = Field(gt=0, le=365)
    reason: str = Field(min_length=1, max_length=512)


class TrialExtendResponse(BaseModel):
    business_id: uuid.UUID
    billing_status: str
    trial_ends_at: datetime | None
    trial_extension_days_total: int


@router.post(
    "/{business_id}/trial/extend",
    response_model=TrialExtendResponse,
    summary="Extend a business's trial (ONBOARDER capped, SUPER_ADMIN unlimited)",
)
async def extend_business_trial(
    business_id: uuid.UUID,
    body: TrialExtendRequest,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """
    Extending an already-EXPIRED trial reactivates it (its derived status
    reads TRIAL again the moment trial_ends_at moves back into the future)
    and, if the hourly cron had already suspended it for TRIAL_EXPIRED,
    unsuspends it too — see backend/app/billing/trials.py::extend_trial.
    """
    biz = await get_business_commercial_scoped(business_id, user, db)
    extend_trial(biz, body.days, is_onboarder=(user.role == AdminRole.ONBOARDER))
    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.trial_extended",
        target_type="business",
        target_id=biz.id,
        diff_json={
            "days": body.days, "reason": body.reason,
            "new_trial_ends_at": biz.trial_ends_at.isoformat() if biz.trial_ends_at else None,
            "trial_extension_days_total": biz.trial_extension_days_total,
        },
    ))
    await db.commit()
    await db.refresh(biz)
    return TrialExtendResponse(
        business_id=biz.id, billing_status=biz.billing_status,
        trial_ends_at=biz.trial_ends_at, trial_extension_days_total=biz.trial_extension_days_total,
    )


class OnboardingConfirmRequest(BaseModel):
    item: str = Field(pattern=r"^(modules|receipt_print|whatsapp_test)$")
    note: str | None = Field(default=None, max_length=512)


class OnboardingConfirmResponse(BaseModel):
    business_id: uuid.UUID
    onboarding_confirmations: dict


@router.post(
    "/{business_id}/onboarding/confirm",
    response_model=OnboardingConfirmResponse,
    summary="Manually confirm a go-live item the system cannot observe automatically",
)
async def confirm_onboarding_item(
    business_id: uuid.UUID,
    body: OnboardingConfirmRequest,
    user: AuthUser = Depends(require_onboarding_role),
    db: AsyncSession = Depends(get_db),
):
    """Uses get_business_commercial_scoped (not get_business_scoped) — same
    "keeps commercial access after handover" rule as trial start/extend,
    since a sales partner may still need to confirm a go-live item shortly
    after handover completes."""
    biz = await get_business_commercial_scoped(business_id, user, db)

    now = datetime.now(timezone.utc)
    confirmations = dict(biz.onboarding_confirmations or {})
    confirmations[body.item] = {
        "confirmed_by": user.user_id.hex if hasattr(user.user_id, "hex") else str(user.user_id),
        "confirmed_at": now.isoformat(),
        "note": body.note,
    }
    biz.onboarding_confirmations = confirmations  # reassign — JSONB mutation must be a new object to be tracked

    db.add(AuditEvent(
        scope=AuditScope.PLATFORM.value,
        business_id=biz.id,
        actor_user_id=user.user_id,
        action="admin.business.onboarding_item_confirmed",
        target_type="business",
        target_id=biz.id,
        diff_json={"item": body.item, "note": body.note},
    ))
    await db.commit()
    await db.refresh(biz)
    return OnboardingConfirmResponse(business_id=biz.id, onboarding_confirmations=biz.onboarding_confirmations)


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Feature 2 — WhatsApp test message send
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/{business_id}/whatsapp/test-send",
    response_model=WhatsAppTestSendResponse,
    summary="Send a WhatsApp test message (uses platform token)",
)
async def send_whatsapp_test(
    business_id: uuid.UUID,
    body: WhatsAppTestSendRequest,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Send a text message via WhatsApp to verify outbound works.
    Uses WHATSAPP_DEFAULT_ACCESS_TOKEN from env (Model 1).
    """
    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    if not biz.whatsapp_phone_number_id:
        raise AppError(
            "NO_PHONE_NUMBER",
            "Business does not have a whatsapp_phone_number_id configured",
            422,
        )

    if not biz.is_whatsapp_enabled:
        raise AppError(
            "WHATSAPP_DISABLED",
            "WhatsApp is disabled for this business",
            422,
        )

    # Use the low-level sender (no DB persistence for test messages)
    from backend.app.bot.whatsapp_sender import _send_via_meta_api_with_detail
    from backend.app.core.config import get_settings as _get_backend_settings

    _backend_settings = _get_backend_settings()
    _masked = lambda t: f"{t[:6]}...{t[-4:]}" if len(t) > 12 else "***"  # noqa: E731

    phone_number_id = biz.whatsapp_phone_number_id
    token_source = "env:WHATSAPP_DEFAULT_ACCESS_TOKEN"
    token = _backend_settings.WHATSAPP_DEFAULT_ACCESS_TOKEN

    logger.info(
        "WhatsApp test-send: business_id=%s, phone_number_id=%s, token_source=%s, token=%s, to=%s",
        business_id,
        phone_number_id,
        token_source,
        _masked(token),
        body.to,
    )

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": body.to,
        "type": "text",
        "text": {"body": body.text},
    }

    wamid, error_detail = await _send_via_meta_api_with_detail(
        phone_number_id=phone_number_id,
        recipient_wa_id=body.to,
        payload=payload,
    )

    if wamid:
        logger.info(
            "Test message sent: business=%s, to=%s, wamid=%s",
            business_id, body.to, wamid,
        )
        return WhatsAppTestSendResponse(
            success=True,
            wa_message_id=wamid,
            phone_number_id=phone_number_id,
        )
    else:
        logger.error(
            "Test message failed: business=%s, phone_number_id=%s, error=%s",
            business_id, phone_number_id, error_detail,
        )
        return WhatsAppTestSendResponse(
            success=False,
            error=error_detail or "Meta API call failed — check logs for details",
            phone_number_id=phone_number_id,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# NEW: Feature — Billing PDF / Invoice generation
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{business_id}/billing/pdf",
    summary="Generate billing PDF summary for a business",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def generate_billing_pdf(
    business_id: uuid.UUID,
    user: AuthUser = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Generate and download a billing summary PDF for a business.
    Includes business details, plan, limits, and usage summary if available.
    """
    from fastapi.responses import Response as FastAPIResponse
    import io

    biz = await db.get(Business, business_id)
    if not biz:
        raise NotFoundError("Business", str(business_id))

    # Try to load current month's usage
    usage_data = None
    try:
        from shared.models.analytics import DailyUsage
        from datetime import date, timedelta
        today = date.today()
        first_of_month = today.replace(day=1)
        result = await db.execute(
            select(DailyUsage).where(
                DailyUsage.business_id == business_id,
                DailyUsage.day >= first_of_month,
                DailyUsage.day <= today,
            )
        )
        rows = list(result.scalars().all())
        if rows:
            usage_data = {
                "inbound_messages": sum(r.inbound_messages for r in rows),
                "outbound_messages": sum(r.outbound_messages for r in rows),
                "llm_calls": sum(r.llm_calls for r in rows),
                "llm_tokens": sum(r.llm_tokens for r in rows),
                "orders_created": sum(r.orders_created for r in rows),
                "revenue_cents": sum(r.revenue_cents for r in rows),
            }
    except Exception:
        pass  # Usage data is optional

    # Generate PDF
    try:
        from fpdf import FPDF
    except ImportError:
        raise AppError("PDF_UNAVAILABLE", "PDF generation library not installed. Run: pip install fpdf2", 500)

    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=20)

    # Header
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(30, 64, 175)
    pdf.cell(0, 12, "NextGen AI Platform", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, "Billing Summary", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    # Date
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, f"Generated: {datetime.now(timezone.utc).strftime('%d %B %Y at %H:%M UTC')}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Separator
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # Business Details section
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 8, "Business Details", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    def row(label: str, value: str):
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(55, 6, label, new_x="RIGHT")
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 6, str(value), new_x="LMARGIN", new_y="NEXT")

    row("Business Name:", biz.name)
    row("Business Code:", biz.business_code)
    row("Slug:", biz.slug)
    row("Timezone:", biz.timezone)
    row("Currency:", biz.currency)
    row("Status:", "Active" if biz.is_active else f"Suspended — {biz.suspended_reason or 'N/A'}")
    pdf.ln(4)

    # Plan & Billing
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 8, "Plan & Billing", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    row("Plan:", biz.plan)
    row("Billing Status:", biz.billing_status)
    row("Stripe Customer:", biz.stripe_customer_id or "Not connected")
    row("Stripe Subscription:", biz.stripe_subscription_id or "Not connected")
    pdf.ln(4)

    # Limits
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 8, "Daily Limits", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    row("Daily Messages:", str(biz.daily_message_limit))
    row("Daily LLM Calls:", str(biz.daily_llm_call_limit))
    row("Daily Orders:", str(biz.daily_order_limit))
    pdf.ln(4)

    # Usage (if available)
    if usage_data:
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 8, f"Usage Summary (Month to Date)", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        row("Inbound Messages:", str(usage_data["inbound_messages"]))
        row("Outbound Messages:", str(usage_data["outbound_messages"]))
        row("LLM Calls:", str(usage_data["llm_calls"]))
        row("LLM Tokens:", f"{usage_data['llm_tokens']:,}")
        row("Orders Created:", str(usage_data["orders_created"]))
        revenue_zar = usage_data["revenue_cents"] / 100
        row("Revenue:", f"R {revenue_zar:,.2f}")
        pdf.ln(4)

    # Footer
    pdf.ln(8)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, "NextGen Intelligence (Pty) Ltd — Durban, KwaZulu-Natal, South Africa", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "info@nextgenintelligence.co.za | 083 786 6021", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, "This is an internally generated billing summary, not a tax invoice.", new_x="LMARGIN", new_y="NEXT")

    # Output
    pdf_bytes = pdf.output()
    filename = f"nextgen-billing-{biz.slug}-{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"

    return FastAPIResponse(
        content=bytes(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
