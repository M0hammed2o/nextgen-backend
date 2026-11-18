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
- A duplicate owner email returns a generic "could not register with these
  details" (REGISTRATION_FAILED). Trade-off: only a caller who already holds
  the valid code can reach this branch, so it is not an open account-exists
  oracle, but the generic message also means a legitimate vendor who already
  has a NextGen login is not told why; the organiser resolves that
  (existing_business_id flow). A dummy bcrypt hash runs on this branch so
  timing matches a successful registration.
- Vendors are created with NO external identity (no WhatsApp number, no
  payment credentials): provision_business(allow_external_identity=False).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from admin_api.app.services.provisioning import ensure_owner_email_free, provision_business
from backend.app.core.config import get_settings
from backend.app.core.errors import AppError, DuplicateError
from backend.app.core.ratelimit import limiter
from backend.app.core.security import hash_password
from backend.app.db.session import get_db
from backend.app.events import registration as reg
from backend.app.events.rules import evaluate_access
from backend.app.events.service import reconcile_event_vendor_access
from shared.enums import EVENT_PLAN, AuditScope
from shared.models.audit import AuditEvent
from shared.models.event import Event, EventVendor

router = APIRouter(prefix="/public/events", tags=["public-events"])
settings = get_settings()
logger = logging.getLogger("nextgen.events.public")

INFO_SCOPE = "public_event_info"
REGISTER_SCOPE = "public_event_register"
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


def _rejected() -> AppError:
    return AppError("REGISTRATION_REJECTED", "Registration is not available with these details", 403)


def _failed() -> AppError:
    return AppError("REGISTRATION_FAILED", "We could not register with these details", 409)


@router.post("/{slug}/register", status_code=201)
@limiter.shared_limit(
    f"{settings.EVENT_REGISTER_RATE_LIMIT_PER_MINUTE}/minute;{settings.EVENT_REGISTER_RATE_LIMIT_PER_HOUR}/hour",
    scope=REGISTER_SCOPE,
)
async def register_vendor(slug: str, body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """
    Create an isolated event-plan business, its owner login (with the vendor's
    own password) and a PENDING EventVendor. Returns no tokens: the vendor logs
    in through the normal auth route.
    """
    event = (await db.execute(
        select(Event).where(Event.slug == slug).options(selectinload(Event.days))
    )).scalar_one_or_none()
    usable = event is not None and event.status == "OPEN" and event.registration_open
    code_ok = await reg.verify_code(body.registration_code, event.registration_code_hash if event else None)
    if not (usable and code_ok):
        raise _rejected()

    # Plain values captured now: nothing is lazily loaded after the commit.
    tz_name, prep_days = event.timezone, event.prep_days
    billable = {d.day for d in event.days if d.is_billable}
    event_id = event.id

    email = str(body.owner_email).lower().strip()
    try:
        await ensure_owner_email_free(db, email)
    except DuplicateError:
        await run_in_threadpool(hash_password, body.password)  # match the success path's timing
        raise _failed()

    password_hash = await run_in_threadpool(hash_password, body.password)
    try:
        provisioned = await provision_business(
            db,
            fields={"name": body.business_name, "plan": EVENT_PLAN, "industry": _CATEGORY_INDUSTRY[body.category],
                    "pos_enabled": True, "inventory_enabled": True, "currency": "ZAR",
                    "timezone": tz_name,
                    # H1: WhatsApp orders bypass the trading gate; off until approved.
                    "is_whatsapp_enabled": False},
            owner_email=email,
            owner_full_name=body.owner_full_name,
            owner_password_hash=password_hash,
            allow_external_identity=False,
        )
        biz = provisioned.business
        business_code = biz.business_code
        vendor = EventVendor(event_id=event_id, business_id=biz.id, category=body.category, status="PENDING")
        db.add(vendor)
        await db.flush()
        db.add(AuditEvent(scope=AuditScope.BUSINESS.value, business_id=biz.id,
                          actor_user_id=provisioned.owner.id, action="event.vendor_self_registered",
                          target_type="event_vendor", target_id=vendor.id,
                          diff_json={"event_id": str(event_id), "category": body.category}))
        await db.flush()
        await reconcile_event_vendor_access(db)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _failed()

    local_day = datetime.now(timezone.utc).astimezone(ZoneInfo(tz_name)).date()
    decision = evaluate_access(local_day, billable, [], prep_days)
    return {
        "business_code": business_code,
        "next": "login",
        # Informational: a vendor registering long before the event cannot log in yet.
        "access_level": decision.level,
        "login_opens_on": (min(billable) - timedelta(days=prep_days)) if billable else None,
    }
