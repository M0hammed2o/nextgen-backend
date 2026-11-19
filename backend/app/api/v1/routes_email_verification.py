"""
Public (unauthenticated) email verification for self-registered souk vendors.

    POST /v1/public/auth/verify-email          {token}  -> 200 | 400 VERIFICATION_TOKEN_INVALID
    POST /v1/public/auth/resend-verification   {email}  -> 202, always the same body

Enumeration and error design
----------------------------
- verify-email answers an unknown, expired, already used or superseded token
  with ONE error (400 VERIFICATION_TOKEN_INVALID, same message), so a caller
  cannot tell which. A token only ever verifies the user it was issued to.
- resend-verification answers 202 with the same body whether the email is
  unknown, already verified, closed, throttled or actually sent to. The
  per-email cooldown and hourly cap are enforced silently in
  services/email_verification.py; a per-email-hash Redis budget is spent here
  too, also silently.
- Both routes are throttled per client IP (slowapi). They have no path
  parameter, so slowapi's default per-route scope is correct (no fixed
  ``scope=`` needed, unlike routes_event_public.py).

No business_id anywhere: the caller has no session. Raw tokens are never
logged or audited.
"""
# No `from __future__ import annotations` here, for the same reason as
# routes_event_public.py: slowapi wraps these routes and FastAPI would resolve
# string annotations against slowapi's globals (tests/test_route_annotations_resolved.py).

import hashlib
import logging

from fastapi import APIRouter, Depends, Request
from limits import parse as parse_limit
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.core.errors import AppError
from backend.app.core.ratelimit import limiter
from backend.app.db.session import get_db
from backend.app.services import email_verification as ev_email
from shared.enums import AuditScope
from shared.models.audit import AuditEvent
from shared.models.business import Business

router = APIRouter(prefix="/public/auth", tags=["public-auth"])
settings = get_settings()
logger = logging.getLogger("nextgen.auth.email_verification")

RESEND_EMAIL_SCOPE = "public_resend_verification_email"
# Per email hash, whoever asks (on top of the per-IP limit and the DB-counted
# per-user cooldown/cap). Silent: an exhausted budget still answers 202.
RESEND_PER_EMAIL_LIMIT = parse_limit("10/hour")

RESEND_ACCEPTED = {
    "status": "accepted",
    "message": "If this email belongs to an account that still needs verification, we have sent a new link.",
}


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


def _invalid() -> AppError:
    return AppError("VERIFICATION_TOKEN_INVALID",
                    "This verification link is invalid or has expired. Request a new one.", 400)


def email_budget_key(email: str) -> str:
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()


def _spend_email_budget(email: str) -> bool:
    """False once this email hash used its hourly budget. Fails open: the DB-counted cap still holds."""
    if not limiter.enabled:
        return True
    try:
        return bool(limiter.limiter.hit(RESEND_PER_EMAIL_LIMIT, RESEND_EMAIL_SCOPE, email_budget_key(email)))
    except Exception:  # noqa: BLE001 - Redis down must not turn into an oracle or a 500
        logger.warning("Resend per-email budget unavailable (storage error); relying on the database cap.")
        return True


@router.post("/verify-email")
@limiter.limit(f"{settings.EMAIL_VERIFY_RATE_LIMIT_PER_MINUTE}/minute")
async def verify_email(body: VerifyEmailRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Spend a verification token. 200 on success; every failure is the same 400."""
    result = await ev_email.consume_token(db, body.token.strip())
    if result is None:
        await db.rollback()
        raise _invalid()
    db.add(AuditEvent(scope=AuditScope.BUSINESS.value, business_id=result.business_id,
                      actor_user_id=result.user_id, action="auth.email_verified",
                      target_type="business_user", target_id=result.user_id, diff_json={}))
    business_code = (await db.execute(
        select(Business.business_code).where(Business.id == result.business_id)
    )).scalar_one_or_none()
    await db.commit()
    return {"verified": True, "next": "login", "business_code": business_code}


@router.post("/resend-verification", status_code=202)
@limiter.limit(
    f"{settings.EMAIL_RESEND_RATE_LIMIT_PER_MINUTE}/minute;{settings.EMAIL_RESEND_RATE_LIMIT_PER_HOUR}/hour"
)
async def resend_verification(body: ResendVerificationRequest, request: Request,
                              db: AsyncSession = Depends(get_db)):
    """Always 202 with the same body. Sends only to a pending self-registered owner, within the limits."""
    email = str(body.email)
    if _spend_email_budget(email):
        await ev_email.request_resend(db, email)
    else:
        # Same work shape as an ineligible email: one lookup and a rollback.
        await db.execute(select(Business.id).where(Business.id.is_(None)))
        await db.rollback()
    return RESEND_ACCEPTED
