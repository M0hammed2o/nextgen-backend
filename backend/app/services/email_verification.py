"""
Email verification for self-registered souk vendors.

Scope. Only an owner created by POST /v1/public/events/{slug}/register has
``email_verification_required = True``. Restaurants, admin-provisioned and
bulk-onboarded owners keep the column default (False) and nothing here ever
applies to them.

Tokens
------
- 32 random bytes from ``secrets`` (``token_urlsafe(32)``, 43 characters).
  Only ``sha256(raw).hexdigest()`` is stored.
- Expire after ``TOKEN_TTL`` (24 h).
- Single use: ``consume_token`` sets ``used_at`` in ONE conditional UPDATE
  (``WHERE used_at IS NULL AND invalidated_at IS NULL AND expires_at > now
  RETURNING user_id``). Two concurrent calls with the same token serialise
  on the row lock and the second re-checks the WHERE after the first commits,
  so exactly one succeeds.
- Issuing a new token invalidates every earlier unused token of that user,
  and a successful verification invalidates the rest.
- A token is bound to one user id; consuming it can only ever write that
  user's row.

Resend
------
``request_resend`` never reveals anything: the route answers the same 202
whatever happens here. Per-email limits (``RESEND_COOLDOWN``,
``RESEND_HOURLY_CAP``) are counted from this user's own token rows and are
enforced silently. Unknown / already verified / inactive emails run the same
lookup and count queries against a placeholder id, so the database work is
roughly the same; the remaining difference (one INSERT and one commit for a
real resend) is small and noted in docs/VENDOR_EMAIL_VERIFICATION.md.

Abandoned registrations
-----------------------
``close_abandoned_registrations`` (hourly worker job) soft-closes a
self-registration still unverified ``ABANDON_AFTER`` (7 days) after it was
created, unless the business has a PAID event subscription: owner and
business deactivated, unpaid subscriptions voided, vendor rows cancelled,
sessions ended, outstanding tokens invalidated, one audit row. Nothing is
deleted. ``releasable_abandoned_owner`` decides when a later registration
with the same email may take the address over: only an owner that was
self-registered, never verified AND already closed as abandoned.

Nothing here commits unless its docstring says so; callers own transactions.
No raw token is ever logged or written to the audit log.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import get_settings
from backend.app.email.sender import EmailMessage, send_safely
from shared.enums import AuditScope
from shared.models.audit import AuditEvent
from shared.models.user import BusinessUser, EmailVerificationToken

logger = logging.getLogger("nextgen.auth.email_verification")

TOKEN_TTL = timedelta(hours=24)
TOKEN_BYTES = 32
RESEND_COOLDOWN = timedelta(seconds=60)
RESEND_HOURLY_CAP = 5
# "Someone tried to register with your email" notices per existing account per hour.
DUPLICATE_NOTICE_HOURLY_CAP = 3
ABANDON_AFTER = timedelta(days=7)
ABANDONED_SUSPENSION_REASON = "EMAIL_VERIFICATION_ABANDONED"
EMAIL_NOT_VERIFIED = "EMAIL_NOT_VERIFIED"

_PLACEHOLDER_ID = uuid.UUID(int=0)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_raw_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def verification_link(raw: str) -> str:
    # The token travels in the URL FRAGMENT: browsers never send a fragment to
    # a server, so it does not land in access logs or Referer headers.
    base = get_settings().EMAIL_VERIFICATION_LINK_BASE_URL.rstrip("/")
    return f"{base}/verify-email#token={raw}"


def is_pending(user: BusinessUser) -> bool:
    """A self-registered owner whose email is not verified yet (and not closed)."""
    return bool(user.email_verification_required and user.email_verified_at is None
                and user.registration_abandoned_at is None)


def releasable_abandoned_owner(user: BusinessUser) -> bool:
    """May a NEW registration take this user's email address? Only an abandoned, never-verified self-registration."""
    return bool(user.email_verification_required and user.email_verified_at is None
                and user.registration_abandoned_at is not None and user.role == "OWNER")


# ── Access ────────────────────────────────────────────────────────────────────

async def owner_email_unverified(db: AsyncSession, business_id: uuid.UUID) -> bool:
    """True if this business has an owner whose required email verification is not done. Live, no cache."""
    return bool((await db.execute(select(exists().where(
        BusinessUser.business_id == business_id,
        BusinessUser.role == "OWNER",
        BusinessUser.email_verification_required.is_(True),
        BusinessUser.email_verified_at.is_(None),
    )))).scalar())


async def verification_status(db: AsyncSession, business_id: uuid.UUID) -> dict:
    """{"required": bool, "verified": bool} for the business's owner(s), for the event-access screen."""
    rows = (await db.execute(
        select(BusinessUser.email_verification_required, BusinessUser.email_verified_at)
        .where(BusinessUser.business_id == business_id, BusinessUser.role == "OWNER")
    )).all()
    required = any(r.email_verification_required for r in rows)
    verified = not any(r.email_verification_required and r.email_verified_at is None for r in rows)
    return {"required": required, "verified": verified}


# ── Tokens ────────────────────────────────────────────────────────────────────

async def _invalidate_unused(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    await db.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user_id,
               EmailVerificationToken.used_at.is_(None),
               EmailVerificationToken.invalidated_at.is_(None))
        .values(invalidated_at=now)
        .execution_options(synchronize_session=False)
    )


async def issue_token(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> str:
    """Invalidate the user's earlier unused tokens and store a new one. Returns the RAW token (never persisted)."""
    now = now or datetime.now(timezone.utc)
    await _invalidate_unused(db, user_id, now)
    raw = new_raw_token()
    db.add(EmailVerificationToken(user_id=user_id, token_hash=hash_token(raw),
                                  expires_at=now + TOKEN_TTL, created_at=now))
    await db.flush()
    return raw


@dataclass
class VerifyResult:
    user_id: uuid.UUID
    business_id: uuid.UUID


async def consume_token(db: AsyncSession, raw: str, now: datetime | None = None) -> VerifyResult | None:
    """
    Mark the token used and the owner verified. None for an unknown, expired,
    already used or superseded token (the caller answers all of these the same
    way). Caller commits.
    """
    now = now or datetime.now(timezone.utc)
    if not raw or len(raw) > 256:
        return None
    user_id = (await db.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == hash_token(raw),
               EmailVerificationToken.used_at.is_(None),
               EmailVerificationToken.invalidated_at.is_(None),
               EmailVerificationToken.expires_at > now)
        .values(used_at=now)
        .returning(EmailVerificationToken.user_id)
        .execution_options(synchronize_session=False)
    )).scalar_one_or_none()
    if user_id is None:
        return None
    # Only THIS token's user. A closed (abandoned) or deactivated account is
    # not verified by an old link: the UPDATE matches nothing and the token
    # stays spent.
    row = (await db.execute(
        update(BusinessUser)
        .where(BusinessUser.id == user_id,
               BusinessUser.is_active.is_(True),
               BusinessUser.registration_abandoned_at.is_(None))
        .values(email_verified_at=func.coalesce(BusinessUser.email_verified_at, now))
        .returning(BusinessUser.id, BusinessUser.business_id)
        .execution_options(synchronize_session=False)
    )).one_or_none()
    if row is None:
        return None
    await _invalidate_unused(db, user_id, now)
    return VerifyResult(user_id=row.id, business_id=row.business_id)


async def _sent_counts(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> tuple[int, datetime | None]:
    row = (await db.execute(
        select(func.count(EmailVerificationToken.id), func.max(EmailVerificationToken.created_at))
        .where(EmailVerificationToken.user_id == user_id,
               EmailVerificationToken.created_at > now - timedelta(hours=1))
    )).one()
    return int(row[0]), row[1]


def _audit(action: str, user: BusinessUser, diff: dict | None = None) -> AuditEvent:
    return AuditEvent(scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.id,
                      action=action, target_type="business_user", target_id=user.id, diff_json=diff or {})


# ── Messages ──────────────────────────────────────────────────────────────────

def verification_message(to: str, raw: str, business_code: str | None) -> EmailMessage:
    code_line = f"Your business code (keep it for support): {business_code}\n\n" if business_code else ""
    return EmailMessage(
        to=to, kind="email_verification",
        subject="Confirm your email to finish registering with NextGen",
        text=(
            "Hello,\n\n"
            "Please confirm your email address to finish registering your stall:\n\n"
            f"{verification_link(raw)}\n\n"
            f"This link works once and expires in {int(TOKEN_TTL.total_seconds() // 3600)} hours.\n\n"
            f"{code_line}"
            "You can log in and set up your stall before confirming, but you cannot take payments or "
            "trade until your email is confirmed.\n\n"
            "If you did not register, ignore this email; the registration is closed automatically "
            f"after {ABANDON_AFTER.days} days.\n"
        ),
    )


def duplicate_registration_message(to: str) -> EmailMessage:
    return EmailMessage(
        to=to, kind="duplicate_registration",
        subject="Someone tried to register with your email at NextGen",
        text=(
            "Hello,\n\n"
            "Somebody just tried to register a new event stall with this email address, but a NextGen "
            "account already uses it, so nothing was created.\n\n"
            "If it was you: log in with your existing account. If you have not confirmed your email yet, "
            "use \"Resend verification email\" on the login or registration page. To trade at another "
            "event with the same account, contact the event organiser.\n\n"
            "If it was not you, you can ignore this email. Your account has not changed.\n"
        ),
    )


async def send_verification(db: AsyncSession, user: BusinessUser, business_code: str | None,
                            now: datetime | None = None) -> EmailMessage:
    """
    Issue a token for ``user``, write the audit row, and return the message to
    send AFTER the caller commits (so a rolled-back registration never emails
    a link that does not work).
    """
    raw = await issue_token(db, user.id, now)
    db.add(_audit("auth.email_verification_sent", user))
    return verification_message(user.email, raw, business_code)


async def record_duplicate_attempt(db: AsyncSession, user: BusinessUser,
                                   now: datetime | None = None) -> EmailMessage | None:
    """
    An attempt to register with an email an account already uses. Writes one
    audit row (no PII) and returns the notice to send after commit, or None
    once DUPLICATE_NOTICE_HOURLY_CAP notices went to this account in the last hour.
    """
    now = now or datetime.now(timezone.utc)
    sent = (await db.execute(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.target_id == user.id,
            AuditEvent.action == "auth.duplicate_registration_notice_sent",
            AuditEvent.created_at > now - timedelta(hours=1),
        )
    )).scalar_one()
    if sent >= DUPLICATE_NOTICE_HOURLY_CAP or not user.email:
        db.add(_audit("auth.duplicate_registration_attempt", user, {"notice": "throttled"}))
        return None
    db.add(_audit("auth.duplicate_registration_notice_sent", user))
    return duplicate_registration_message(user.email)


# ── Resend ────────────────────────────────────────────────────────────────────

async def request_resend(db: AsyncSession, email: str, now: datetime | None = None) -> bool:
    """
    Send a fresh verification link if ``email`` belongs to a pending
    self-registered owner and the per-email limits allow it. Commits when it
    sends. Returns whether a message was sent (for tests and logs only; the
    route never exposes it).
    """
    now = now or datetime.now(timezone.utc)
    email = email.lower().strip()
    user = (await db.execute(
        select(BusinessUser).where(BusinessUser.email == email).with_for_update()
    )).scalar_one_or_none()
    eligible = user is not None and user.is_active and user.role == "OWNER" and is_pending(user)
    # Same count query either way, against a placeholder id when not eligible.
    count, last = await _sent_counts(db, user.id if eligible else _PLACEHOLDER_ID, now)
    new_raw_token()  # same CPU work as issuing, for the not-eligible path
    if not eligible:
        await db.rollback()
        return False
    if count >= RESEND_HOURLY_CAP or (last is not None and now - last < RESEND_COOLDOWN):
        user_id = user.id  # read before rollback expires the instance
        await db.rollback()
        logger.info("Verification resend throttled for user %s", user_id)
        return False
    from shared.models.business import Business  # local: keeps this module's import graph small
    business_code = (await db.execute(
        select(Business.business_code).where(Business.id == user.business_id)
    )).scalar_one_or_none()
    message = await send_verification(db, user, business_code, now)
    await db.commit()
    await send_safely(message)
    return True


# ── Abandoned registrations ───────────────────────────────────────────────────

@dataclass
class AbandonResult:
    closed_user_ids: list[uuid.UUID] = field(default_factory=list)


async def close_abandoned_registrations(db: AsyncSession, now: datetime | None = None) -> AbandonResult:
    """
    Soft-close self-registrations still unverified ABANDON_AFTER after
    creation with no PAID event subscription. Caller commits.

    Rows are locked FOR UPDATE SKIP LOCKED: an owner whose row is busy (for
    example a verification landing right now) is left to the next hourly run.
    """
    from backend.app.events.service import EVENT_SUSPENSION_REASON
    from shared.auth_sessions import end_user_sessions
    from shared.models.business import Business
    from shared.models.event import EventVendor, EventVendorSubscription

    now = now or datetime.now(timezone.utc)
    result = AbandonResult()
    has_paid = exists().where(
        EventVendorSubscription.business_id == BusinessUser.business_id,
        EventVendorSubscription.status == "PAID",
    )
    owners = (await db.execute(
        select(BusinessUser)
        .where(BusinessUser.role == "OWNER",
               BusinessUser.email_verification_required.is_(True),
               BusinessUser.email_verified_at.is_(None),
               BusinessUser.registration_abandoned_at.is_(None),
               BusinessUser.created_at < now - ABANDON_AFTER,
               ~has_paid)
        .order_by(BusinessUser.id)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    for owner in owners:
        owner.registration_abandoned_at = now
        owner.is_active = False
        await end_user_sessions(db, owner)
        await _invalidate_unused(db, owner.id, now)

        # Business: deactivate with our own reason. An administrator's
        # suspension (any other reason) is left exactly as it is.
        biz = (await db.execute(
            select(Business).where(Business.id == owner.business_id).with_for_update()
        )).scalar_one()
        business_closed = False
        if biz.is_active or biz.suspended_reason in (None, EVENT_SUSPENSION_REASON):
            biz.is_active = False
            biz.suspended_reason = ABANDONED_SUSPENSION_REASON
            business_closed = True

        voided = (await db.execute(
            update(EventVendorSubscription)
            .where(EventVendorSubscription.business_id == owner.business_id,
                   EventVendorSubscription.status == "UNPAID")
            .values(status="VOID")
            .execution_options(synchronize_session=False)
        )).rowcount
        cancelled = (await db.execute(
            update(EventVendor)
            .where(EventVendor.business_id == owner.business_id, EventVendor.status != "CANCELLED")
            .values(status="CANCELLED", status_changed_at=now)
            .execution_options(synchronize_session=False)
        )).rowcount
        db.add(AuditEvent(
            scope=AuditScope.BUSINESS.value, business_id=owner.business_id, actor_user_id=None,
            action="auth.registration_abandoned_closed", target_type="business_user", target_id=owner.id,
            diff_json={"unverified_for_days": ABANDON_AFTER.days, "business_deactivated": business_closed,
                       "voided_subscriptions": int(voided or 0), "cancelled_vendor_rows": int(cancelled or 0)},
        ))
        result.closed_user_ids.append(owner.id)

    await db.flush()
    if result.closed_user_ids:
        logger.info("Abandoned registrations: closed %d unverified self-registration(s)", len(result.closed_user_ids))
    return result


async def release_abandoned_email(db: AsyncSession, user: BusinessUser) -> None:
    """
    Tombstone an abandoned registration's email so a new registration can use
    it, in the caller's transaction. The old rows stay (deactivated); only the
    login address is cleared. Caller has checked releasable_abandoned_owner.
    """
    if not releasable_abandoned_owner(user):  # defence in depth
        raise ValueError("refusing to release a verified, active or admin-provisioned account's email")
    user.email = None
    db.add(_audit("auth.abandoned_registration_email_released", user))
    await db.flush()


__all__ = [
    "ABANDON_AFTER", "ABANDONED_SUSPENSION_REASON", "EMAIL_NOT_VERIFIED", "RESEND_COOLDOWN",
    "RESEND_HOURLY_CAP", "TOKEN_TTL", "close_abandoned_registrations", "consume_token", "hash_token",
    "issue_token", "is_pending", "owner_email_unverified", "record_duplicate_attempt",
    "releasable_abandoned_owner", "release_abandoned_email", "request_resend", "send_verification",
    "verification_link", "verification_status",
]
