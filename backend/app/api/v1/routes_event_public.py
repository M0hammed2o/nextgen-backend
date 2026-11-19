"""
Public (unauthenticated) souk endpoints.

    GET  /v1/public/events/{slug}            event facts a prospective vendor needs
    POST /v1/public/events/{slug}/register   vendor self-registration

There is no business_id anywhere here: the caller has no tenant yet. Both
routes are throttled per client IP with fixed scopes (the path carries a
parameter, so ``shared_limit(scope=...)`` is required, see
routes_receipt_public.py).

Enumeration and error design
----------------------------
- An unknown slug and a DRAFT or CANCELLED event return the same 404.
- Registration returns ONE error ("REGISTRATION_REJECTED") for: unknown slug,
  event not open, registration closed, no code set, wrong code. A bcrypt
  check always runs so these also take the same time.
- Every registration that passes the code check gets the SAME 202 body
  ("check your email"), whether the email is new or already belongs to an
  account. A duplicate creates nothing and the existing address gets a
  throttled "someone tried to register" notice instead (mock email). The
  password is bcrypt-hashed on both paths so timing matches. The business
  code is no longer in the response: it is in the verification email (and
  returned by POST /public/auth/verify-email). Owner login is email+password
  and never needed it.
- Re-registration over an abandoned, never-verified self-registration
  releases the old address in the same transaction
  (services/email_verification.py). A verified or admin-provisioned account
  is never taken over.
- Access for the new business is decided for THAT business only
  (reconcile_single_business_access); a public request never writes other tenants' rows.
  A per-event hourly ceiling (EVENT_REGISTER_RATE_LIMIT_PER_EVENT_PER_HOUR) is spent only
  after the registration code is accepted.
- The owner email must be verified (docs/VENDOR_EMAIL_VERIFICATION.md): until
  then the vendor may log in and configure, but the access decision caps it
  at CONFIGURE (EMAIL_NOT_VERIFIED) and a payment claim is refused.
- Vendors are created with NO external identity (no WhatsApp number, no
  payment credentials): provision_business(allow_external_identity=False).
"""
# No `from __future__ import annotations` here: the slowapi decorator wraps these
# routes, and FastAPI would then resolve string annotations against slowapi's
# globals, leaving RegisterRequest an unresolved ForwardRef that it treats as a
# required query parameter (every POST /register got 422; found by the HTTP E2E).

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from limits import parse as parse_limit
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from admin_api.app.services.provisioning import provision_business
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, DuplicateError
from backend.app.core.ratelimit import limiter
from backend.app.core.security import hash_password
from backend.app.db.session import get_db
from backend.app.events import registration as reg
from backend.app.events.rules import evaluate_access
from backend.app.email.sender import send_safely
from backend.app.events.service import reconcile_single_business_access
from backend.app.services import email_verification as ev_email
from shared.enums import EVENT_PLAN, AuditScope
from shared.models.audit import AuditEvent
from shared.models.event import Event, EventVendor
from shared.models.user import BusinessUser

router = APIRouter(prefix="/public/events", tags=["public-events"])
settings = get_settings()
logger = logging.getLogger("nextgen.events.public")

INFO_SCOPE = "public_event_info"
REGISTER_SCOPE = "public_event_register"
EVENT_REGISTER_EVENT_SCOPE = "public_event_register_event"
# M-2: a ceiling on businesses created for ONE event per hour, whichever IPs they
# come from (the per-IP limit alone is beaten by a botnet or a few proxies). It
# is only spent by a request that already holds the valid registration code,
# so junk traffic cannot use it to lock real vendors out.
EVENT_REGISTER_PER_EVENT_LIMIT = parse_limit(f"{settings.EVENT_REGISTER_RATE_LIMIT_PER_EVENT_PER_HOUR}/hour")
_CATEGORY_INDUSTRY = {"FOOD": "EVENT_FOOD", "CLOTHING": "EVENT_RETAIL"}
PUBLIC_STATUSES = ("OPEN", "CLOSED")


class RegisterRequest(BaseModel):
    registration_code: str = Field(min_length=1, max_length=64)
    business_name: str = Field(min_length=2, max_length=255)
    category: Literal["FOOD", "CLOTHING"]
    owner_full_name: str = Field(min_length=1, max_length=255)
    owner_email: EmailStr
    # Same rule as the login/set-password schemas (min 8, max 128), plus bcrypt's
    # 72-byte input limit so a long password is a 422 rather than a server error.
    password: str = Field(min_length=8, max_length=128)
    accept_terms: bool

    @field_validator("password")
    @classmethod
    def _bcrypt_limit(cls, v: str) -> str:
        if len(v.encode("utf-8")) > 72:
            raise ValueError("password must be at most 72 bytes")
        return v

    @field_validator("accept_terms")
    @classmethod
    def _terms(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("the terms must be accepted")
        return v

    @field_validator("business_name", "owner_full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


@router.get("/{slug}")
@limiter.shared_limit(
    f"{settings.EVENT_PUBLIC_RATE_LIMIT_PER_MINUTE}/minute",
    scope=INFO_SCOPE,
)
async def get_public_event(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Public facts about an OPEN or CLOSED event. Never any vendor data."""
    event = (await db.execute(
        select(Event).where(Event.slug == slug, Event.status.in_(PUBLIC_STATUSES)).options(selectinload(Event.days))
    )).scalar_one_or_none()
    if event is None:
        raise AppError("EVENT_NOT_FOUND", "Event not found", 404)
    return {
        "name": event.name, "slug": event.slug, "venue": event.venue, "timezone": event.timezone,
        "status": event.status, "prep_days": event.prep_days,
        "days": [{"day": d.day, "is_billable": d.is_billable} for d in event.days],
        "food_day_rate_cents": event.food_day_rate_cents,
        "clothing_day_rate_cents": event.clothing_day_rate_cents,
        "registration_open": bool(event.registration_open and event.status == "OPEN"),
    }


def event_register_budget_key(slug: str) -> str:
    """Rate-limit key for the per-event registration budget."""
    return slug.strip().lower()


def _spend_event_register_budget(slug: str) -> None:
    """Raise 429 once this event has had its hourly registration allowance. No-op when limiting is disabled."""
    if not limiter.enabled:
        return
    if not limiter.limiter.hit(EVENT_REGISTER_PER_EVENT_LIMIT, EVENT_REGISTER_EVENT_SCOPE,
                               event_register_budget_key(slug)):
        raise AppError("EVENT_REGISTRATION_RATE_LIMITED",
                       "Too many registrations for this event right now; please try again later", 429)


def _rejected() -> AppError:
    return AppError("REGISTRATION_REJECTED", "Registration is not available with these details", 403)


def _failed() -> AppError:
    return AppError("REGISTRATION_FAILED", "We could not register with these details", 409)


def _accepted(tz_name: str, billable: set, prep_days: int) -> dict:
    """
    The ONE answer for every registration that passed the code check: new
    account, existing email, or re-registration over an abandoned one. Only
    event facts (public anyway) are included; nothing about the account.
    """
    local_day = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()
    decision = evaluate_access(local_day, billable, [], prep_days)
    return {
        "status": "verification_pending",
        "next": "check_email",
        "message": ("Check your email to continue. If this address can be used, we have sent a link to "
                    "confirm it."),
        # Informational: a vendor registering long before the event cannot log in yet.
        "access_level": decision.level,
        "login_opens_on": (min(billable) - timedelta(days=prep_days)) if billable else None,
    }


async def _email_taken(db: AsyncSession, email: str) -> bool:
    found = (await db.execute(select(BusinessUser.id).where(BusinessUser.email == email))).scalar_one_or_none()
    return found is not None


@router.post("/{slug}/register", status_code=202)
@limiter.shared_limit(
    f"{settings.EVENT_REGISTER_RATE_LIMIT_PER_MINUTE}/minute;{settings.EVENT_REGISTER_RATE_LIMIT_PER_HOUR}/hour",
    scope=REGISTER_SCOPE,
)
async def register_vendor(slug: str, body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Create an isolated event-plan business, its owner login (with the vendor's
    own password, email verification required) and a PENDING EventVendor, and
    email a verification link. Returns no tokens and no account data: the same
    202 body is returned when the email already belongs to an account (that
    account gets a notice email instead and nothing is created).
    """
    event = (await db.execute(
        select(Event).where(Event.slug == slug).options(selectinload(Event.days))
    )).scalar_one_or_none()
    usable = event is not None and event.status == "OPEN" and event.registration_open
    code_ok = await reg.verify_code(body.registration_code, event.registration_code_hash if event else None)
    if not (usable and code_ok):
        raise _rejected()
    _spend_event_register_budget(slug)

    # Plain values captured now: nothing is lazily loaded after the commit.
    tz_name, prep_days = event.timezone, event.prep_days
    billable = {d.day for d in event.days if d.is_billable}
    event_id = event.id
    accepted = _accepted(tz_name, billable, prep_days)

    email = str(body.owner_email).lower().strip()
    # Hashed on every path (new, duplicate, takeover) so their timing matches.
    password_hash = await run_in_threadpool(hash_password, body.password)

    existing = (await db.execute(
        select(BusinessUser).where(BusinessUser.email == email).with_for_update()
    )).scalar_one_or_none()
    if existing is not None and not ev_email.releasable_abandoned_owner(existing):
        # Enumeration: same 202 as a new registration. Nothing is created; the
        # address's real owner is told (throttled) that someone tried.
        notice = await ev_email.record_duplicate_attempt(db, existing)
        await db.commit()
        if notice is not None:
            await send_safely(notice)
        return accepted

    try:
        if existing is not None:
            # The only account with this email is an abandoned, never-verified
            # self-registration: release its address in THIS transaction. The
            # new verification proves ownership of the mailbox; the old rows
            # stay deactivated and are never reused.
            await ev_email.release_abandoned_email(db, existing)
        provisioned = await provision_business(
            db,
            fields={"name": body.business_name, "plan": EVENT_PLAN, "industry": _CATEGORY_INDUSTRY[body.category],
                    "pos_enabled": True, "inventory_enabled": True, "currency": "ZAR",
                    "timezone": tz_name,
                    # WhatsApp starts off; a super admin enables it per vendor.
                    "is_whatsapp_enabled": False},
            owner_email=email,
            owner_full_name=body.owner_full_name,
            owner_password_hash=password_hash,
            allow_external_identity=False,
        )
        biz = provisioned.business
        owner = provisioned.owner
        owner.email_verification_required = True
        vendor = EventVendor(event_id=event_id, business_id=biz.id, category=body.category, status="PENDING")
        db.add(vendor)
        await db.flush()
        db.add(AuditEvent(scope=AuditScope.BUSINESS.value, business_id=biz.id,
                          actor_user_id=owner.id, action="event.vendor_self_registered",
                          target_type="event_vendor", target_id=vendor.id,
                          diff_json={"event_id": str(event_id), "category": body.category}))
        await db.flush()
        message = await ev_email.send_verification(db, owner, biz.business_code)
        # M-2: decide ONLY the business created here. The global reconcile
        # wrote other events' vendors and other tenants' businesses from a
        # public, unauthenticated request.
        await reconcile_single_business_access(db, biz.id)
        await db.commit()
    except (IntegrityError, DuplicateError):
        # A concurrent registration took the same email between our check and
        # the insert: answer exactly like the duplicate path. Any other
        # integrity failure (e.g. a slug race) stays a generic 409.
        await db.rollback()
        if await _email_taken(db, email):
            return accepted
        raise _failed()

    await send_safely(message)
    return accepted
