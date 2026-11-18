"""
Business provisioning: the one place a new tenant and its owner login are
created. Extracted unchanged from ``POST /admin/businesses`` so that bulk
flows (souk vendor onboarding now, multi-branch creation later) reuse exactly
the same rules instead of re-implementing them:

- a unique slug and a unique business code;
- owner email unique platform-wide;
- the owner gets a generated temporary password and must change it at
  first login;
- a new business never inherits any external identifier (WhatsApp phone
  number id, WABA, delivery store ids, payment credentials). They are simply
  not parameters here.

Nothing here commits. The caller owns the transaction, so a bulk import is
all-or-nothing and the caller writes its own audit event.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backend.app.billing.trials import start_trial
from backend.app.core.errors import AppError, DuplicateError
from backend.app.core.security import hash_password
from shared.models.business import Business
from shared.models.user import BusinessUser
from shared.utils import generate_business_code, generate_temp_password

# Fields a caller may never set through provisioning: identities that belong
# to exactly one real business and must be connected by that business itself.
FORBIDDEN_FIELDS = frozenset({
    "whatsapp_phone_number_id",
    "whatsapp_business_account_id",
    "payment_api_key",
    "payment_api_secret",
    "payment_webhook_secret",
    "stripe_customer_id",
    "stripe_subscription_id",
    "payfast_subscription_token",
})


@dataclass
class ProvisionedBusiness:
    business: Business
    owner: BusinessUser | None
    owner_temporary_password: str | None


async def unique_slug(db: AsyncSession, name: str, requested: str | None = None) -> str:
    slug = requested or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "business"
    for suffix in range(20):
        candidate = slug if suffix == 0 else f"{slug}-{suffix}"
        if not (await db.execute(select(Business.id).where(Business.slug == candidate))).scalar_one_or_none():
            return candidate
    raise AppError("SLUG_GENERATION_FAILED", "Could not generate unique slug", 500)


async def unique_business_code(db: AsyncSession) -> str:
    for _ in range(10):
        code = generate_business_code()
        if not (await db.execute(select(Business.id).where(Business.business_code == code))).scalar_one_or_none():
            return code
    raise AppError("CODE_GENERATION_FAILED", "Could not generate unique business code", 500)


async def ensure_owner_email_free(db: AsyncSession, email: str) -> None:
    taken = await db.execute(select(BusinessUser.id).where(BusinessUser.email == email.lower().strip()))
    if taken.scalar_one_or_none():
        raise DuplicateError("User", "email")


async def provision_business(
    db: AsyncSession,
    *,
    fields: dict[str, Any],
    requested_slug: str | None = None,
    owner_email: str | None = None,
    owner_full_name: str | None = None,
    trial: bool = False,
    onboarded_by_admin_id: uuid.UUID | None = None,
    allow_external_identity: bool = False,
) -> ProvisionedBusiness:
    """
    Create a business (and optionally its owner) in the caller's transaction.
    Flushes, never commits.

    ``allow_external_identity`` is True only for the single-business admin
    create route, where a super admin may deliberately set that one
    business's own WhatsApp number at creation (the admin suite's create
    modal does this today). Bulk callers (event vendors, branch templates)
    leave it False, so no external identity can ever be copied in bulk. A
    key present with a None value is not an identity, so it is ignored.
    """
    if not allow_external_identity:
        forbidden = {k for k in FORBIDDEN_FIELDS & set(fields) if fields[k] is not None}
        if forbidden:
            raise AppError("FIELD_NOT_ALLOWED", f"cannot provision with {sorted(forbidden)}", 422)
    slug = await unique_slug(db, fields["name"], requested_slug)
    if owner_email:
        await ensure_owner_email_free(db, owner_email)
    code = await unique_business_code(db)

    biz = Business(business_code=code, slug=slug, onboarded_by_admin_id=onboarded_by_admin_id, **fields)
    if trial:
        start_trial(biz)
    else:
        # Explicit ACTIVE: the model default TRIAL with no trial_ends_at would
        # read as an immediately expired trial (billing/account_status.py).
        biz.billing_status = "ACTIVE"
    db.add(biz)
    await db.flush()

    owner = None
    temp_password = None
    if owner_email:
        temp_password = generate_temp_password()
        owner = BusinessUser(
            business_id=biz.id,
            role="OWNER",
            email=owner_email.lower().strip(),
            # bcrypt is CPU-bound (~0.8 s here); run it off the event loop so a
            # bulk import does not stall every other admin request.
            password_hash=await run_in_threadpool(hash_password, temp_password),
            staff_name=owner_full_name,
            is_active=True,
            must_change_password=True,
        )
        db.add(owner)
        await db.flush()
    return ProvisionedBusiness(business=biz, owner=owner, owner_temporary_password=temp_password)
