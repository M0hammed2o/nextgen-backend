"""
Server-side trading gate for event vendors (souk).

A business on the event plan may only create a real sale or record money
while it holds a PAID subscription covering the current trading date at one of
its events (access level TRADE, rules.py). The level is computed live from the
database on every call, never from a cached flag, and "today" is the event's
own timezone.

Non-event businesses (any plan other than EVENT_PLAN) are never affected.

WhatsApp-originated orders ARE gated (souk WhatsApp gate, owner-approved
change to the protected bot files; docs/EVENT_WHATSAPP.md):
  - bot_trade_refusal() is checked early in pipeline._process (no LLM, no cart
    on a non-trading day; the customer gets a neutral notice once per
    event-local day), again at the top of _handle_order_confirmation and
    _finalize_pending_delivery_order (hard gates), and
    assert_bot_order_allowed() is a raising backstop inside
    order_creator.create_order_from_cart, before any row or SSE event;
  - for a restaurant (plan != EVENT_PLAN) both return before any SQL, using
    the Business row the pipeline has already loaded.
WhatsApp for an event vendor additionally requires: a SUPER_ADMIN setting
is_whatsapp_enabled (the per-vendor kill switch from migration 0065), and the
vendor's OWN Embedded-Signup connection (token_resolver never gives an event
vendor the platform token, so Meta bills the vendor, not NextGen).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.errors import AppError
from backend.app.events.rules import TRADE, PaidRange, last_paid_trade_day
from backend.app.events.service import EVENT_SUSPENSION_REASON, business_access_decision
from shared.enums import EVENT_PLAN
from shared.models.business import Business
from shared.models.event import Event, EventVendor

logger = logging.getLogger("nextgen.events.trading")

# Offline POS sync (Phase 3b). A queued sale carries the time it was rung up;
# it is only believable inside this window. The 72h bound is what limits
# backdating abuse: a vendor cannot claim a paid day from last week.
OFFLINE_CAPTURE_MAX_AGE = timedelta(hours=72)
# Souk security review H-2. A vendor on an unpaid (CONFIGURE-only) day can still
# log in and sync, and a device timestamp cannot be proven, so event-plan
# businesses get a shorter window and extra flags. OWNER MAY ADJUST all three
# (documented in docs/OFFLINE_POS_SYNC_CONTRACT.md).
EVENT_OFFLINE_CAPTURE_MAX_AGE = timedelta(hours=36)
# Any plan: a capture this much older than the sync is flagged "late_sync".
OFFLINE_LATE_SYNC_THRESHOLD = timedelta(hours=12)
REASON_CAPTURE_DAY_UNPAID_AT_SYNC = "capture_day_unpaid_at_sync"
REASON_LATE_SYNC = "late_sync"
REASON_CASHIER_DIFFERS = "cashier_differs_from_syncer"
OFFLINE_CAPTURE_MAX_FUTURE_SKEW = timedelta(minutes=5)

# BUG-0208 post-event sync grace (owner approval 2026-11-19). After an event
# vendor's last paid day the hourly reconcile suspends it
# (EVENT_SUBSCRIPTION_ENDED). For this long after the END of that last paid
# day (end of that local date, in the event's timezone) the suspended vendor
# may still authenticate, but ONLY for the sync-only allowlist in
# backend/app/events/sync_grace.py (upload queued offline sales, view
# reconciliation, close the till). OWNER MAY ADJUST. It does NOT change the
# capture age bound above: a capture older than 36h still goes to manager
# review, never auto-accepted.
EVENT_SYNC_GRACE = timedelta(hours=72)
# A paid-day capture synced while the vendor is in that grace (informational;
# replaces capture_day_unpaid_at_sync, which would wrongly suggest backdating).
REASON_POST_EVENT_GRACE_SYNC = "post_event_grace_sync"
# An offline capture refused by the automatic age bound and later approved by
# an owner/manager from the late-upload review queue.
REASON_LATE_CAPTURE_APPROVED = "late_capture_approved"


def validate_capture_window(
    captured_at: datetime, now: datetime | None = None, max_age: timedelta | None = OFFLINE_CAPTURE_MAX_AGE,
) -> datetime:
    """
    Return `captured_at` as an aware UTC datetime, or raise 422
    OFFLINE_CAPTURE_TOO_OLD / OFFLINE_CAPTURE_IN_FUTURE. A naive timestamp is
    refused (422 OFFLINE_CAPTURE_NO_TIMEZONE): guessing a zone would let the
    event-day decision move by hours.

    `max_age=None` skips ONLY the age bound. It is used for exactly two
    things: deciding whether a too-old capture may be queued for manager
    review (everything else must still pass) and re-validating a capture an
    owner/manager is approving from that queue. Never for an automatic accept.
    """
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise AppError("OFFLINE_CAPTURE_NO_TIMEZONE", "offline_captured_at must include a timezone offset", 422)
    now = now or datetime.now(timezone.utc)
    captured = captured_at.astimezone(timezone.utc)
    if max_age is not None and captured < now - max_age:
        raise AppError(
            "OFFLINE_CAPTURE_TOO_OLD",
            "This offline sale was captured too long ago to be accepted",
            422,
            details={"max_age_hours": int(max_age.total_seconds() // 3600)},
        )
    if captured > now + OFFLINE_CAPTURE_MAX_FUTURE_SKEW:
        raise AppError(
            "OFFLINE_CAPTURE_IN_FUTURE",
            "offline_captured_at is in the future; check the device clock",
            422,
        )
    return captured


@dataclass
class OfflineCaptureGate:
    """Result of judging an offline capture: the aware-UTC capture time and any review reasons."""
    captured_at: datetime
    reasons: list[str] = field(default_factory=list)


async def offline_capture_gate(
    db: AsyncSession, business_id: uuid.UUID, captured_at: datetime, now: datetime | None = None,
    *, in_sync_grace: bool = False, enforce_max_age: bool = True, judge_sync_time: bool = True,
) -> OfflineCaptureGate:
    """
    Judge an offline capture for `business_id` (from the JWT). Raises like
    assert_trading_allowed(captured_at=...). Plan-specific rules:
    - EVENT plan: window is EVENT_OFFLINE_CAPTURE_MAX_AGE (36h); the capture
      day must be a paid TRADE day; and if the access decision AT SYNC TIME is
      not TRADE the sale is still accepted but gets reason
      capture_day_unpaid_at_sync (a vendor trading on unpaid days by
      backdating into a paid one).
    - Any plan: a capture more than OFFLINE_LATE_SYNC_THRESHOLD older than
      the sync gets reason late_sync.

    `in_sync_grace` (BUG-0208): the caller is a suspended event vendor inside
    its post-event sync grace (decided live by get_current_user, never by the
    client). The paid-day check is unchanged; the sync-time reason becomes
    post_event_grace_sync instead of capture_day_unpaid_at_sync.
    `enforce_max_age=False` / `judge_sync_time=False`: review-queue use only
    (see validate_capture_window).
    """
    now = now or datetime.now(timezone.utc)
    plan = (await db.execute(select(Business.plan).where(Business.id == business_id))).scalar_one_or_none()
    is_event = plan == EVENT_PLAN
    max_age = EVENT_OFFLINE_CAPTURE_MAX_AGE if is_event else OFFLINE_CAPTURE_MAX_AGE
    captured = validate_capture_window(captured_at, now, max_age if enforce_max_age else None)
    reasons: list[str] = []
    if is_event:
        await _require_trade_at(db, business_id, captured)
        if judge_sync_time and (await business_access_decision(db, business_id, now)).level != TRADE:
            reasons.append(REASON_POST_EVENT_GRACE_SYNC if in_sync_grace else REASON_CAPTURE_DAY_UNPAID_AT_SYNC)
    if captured < now - OFFLINE_LATE_SYNC_THRESHOLD:
        reasons.append(REASON_LATE_SYNC)
    return OfflineCaptureGate(captured_at=captured, reasons=reasons)


async def event_sync_grace_until(
    db: AsyncSession, business_id: uuid.UUID, now: datetime | None = None,
) -> datetime | None:
    """
    BUG-0208: when this business's post-event sync grace ends, or None if it
    has none right now. Computed live from the database on every call; no
    flag is cached. ALL of these must hold, otherwise None:

    - the business is suspended (is_active false) with EXACTLY the automatic
      end-of-event reason EVENT_SUBSCRIPTION_ENDED. An administrator's
      suspension (EVENT_ADMIN_SUSPENDED), a billing suspension, or any other
      reason never gets grace;
    - the business is on the EVENT plan (a restaurant never gets grace);
    - at some event that is not CANCELLED, where this vendor is not
      CANCELLED, the vendor's last paid TRADE day L ended at or before `now`
      and `now` is before end(L) + EVENT_SYNC_GRACE, with end(L) the end of
      local date L in that event's timezone.

    A cancelled event or vendor contributes nothing, even if it had paid days.
    business_id must come from the caller's JWT.
    """
    from sqlalchemy.orm import selectinload

    now = now or datetime.now(timezone.utc)
    row = (await db.execute(
        select(Business.is_active, Business.suspended_reason, Business.plan).where(Business.id == business_id)
    )).one_or_none()
    if row is None:
        return None
    is_active, reason, plan = row
    if is_active or reason != EVENT_SUSPENSION_REASON or plan != EVENT_PLAN:
        return None
    vendors = (await db.execute(
        select(EventVendor)
        .where(EventVendor.business_id == business_id)
        .options(selectinload(EventVendor.subscriptions), selectinload(EventVendor.event).selectinload(Event.days))
    )).scalars().all()
    best: datetime | None = None
    for vendor in vendors:
        event = vendor.event
        if vendor.status == "CANCELLED" or event.status == "CANCELLED":
            continue
        billable = {d.day for d in event.days if d.is_billable}
        paid = [PaidRange(s.starts_on, s.ends_on) for s in vendor.subscriptions if s.status == "PAID"]
        last = last_paid_trade_day(billable, paid)
        if last is None:
            continue
        ended = datetime.combine(last + timedelta(days=1), time(0), tzinfo=ZoneInfo(event.timezone))
        ended = ended.astimezone(timezone.utc)
        until = ended + EVENT_SYNC_GRACE
        if ended <= now < until and (best is None or until > best):
            best = until
    return best


_MESSAGES = {
    "NO_PAID_COVER_TODAY": "Trading is not allowed: there is no paid event subscription covering today.",
    "NOT_AN_EVENT_DAY": "Trading is not allowed: today is not an event trading day.",
    "CALENDAR_NOT_PUBLISHED": "Trading is not allowed: the event calendar has not been published yet.",
    "PREP_WINDOW": "Trading is not allowed yet: the event has not started. You can configure your stall.",
    "EMAIL_NOT_VERIFIED": "Trading is not allowed until the owner's email address is verified. "
                          "Use the link in the verification email, or request a new one.",
}


async def assert_trading_allowed(
    db: AsyncSession,
    business_id: uuid.UUID,
    now: datetime | None = None,
    *,
    captured_at: datetime | None = None,
) -> None:
    """
    Raise 403 EVENT_TRADING_NOT_ALLOWED unless an event-plan business may trade right now.

    `captured_at` is for an offline POS sync only: the access decision is then
    evaluated at the time the sale was rung up (its local date in the event's
    own timezone), not at sync time, so a sale made on a paid day and synced
    the next morning is not refused, and a sale made on an unpaid day is not
    laundered by syncing it on a paid one. The capture time is bounded to
    [now - 72h, now + 5min] here as well, whatever the caller did. Without
    `captured_at` behaviour is exactly the online behaviour.
    """
    if captured_at is not None:
        await offline_capture_gate(db, business_id, captured_at, now)
        return
    plan = (await db.execute(select(Business.plan).where(Business.id == business_id))).scalar_one_or_none()
    if plan != EVENT_PLAN:
        return
    await _require_trade_at(db, business_id, now)


async def _require_trade_at(db: AsyncSession, business_id: uuid.UUID, at: datetime | None) -> None:
    decision = await business_access_decision(db, business_id, at)
    if decision.level == TRADE:
        return
    raise AppError(
        "EVENT_TRADING_NOT_ALLOWED",
        _MESSAGES.get(decision.reason, "Trading is not allowed for this event vendor right now."),
        403,
        details={"reason": decision.reason, "access_level": decision.level},
    )


# -- WhatsApp bot (souk WhatsApp gate) ----------------------------------------

# Returned by bot_trade_refusal when the access check itself failed: the bot
# fails closed (no order), but never raises into the pipeline.
BOT_ACCESS_CHECK_FAILED = "ACCESS_CHECK_FAILED"


async def bot_trade_refusal(db: AsyncSession, business: Business, now: datetime | None = None) -> str | None:
    """
    None when the WhatsApp bot may take a real order for `business` right now,
    else the refusal reason (never shown to the customer).

    Uses the Business row the pipeline already loaded: a non-event business
    returns None before any SQL. An event-plan business gets a live
    business_access_decision (event timezone) inside a SAVEPOINT, so a failed
    check cannot abort the pipeline's transaction (the inbound message row and
    the notice reply survive). Never raises; fails closed.
    """
    if getattr(business, "plan", None) != EVENT_PLAN:
        return None
    try:
        async with db.begin_nested():
            decision = await business_access_decision(db, business.id, now)
    except Exception:  # noqa: BLE001 - must never escape into the bot pipeline
        logger.exception("EVENT_BOT_ACCESS_CHECK_FAILED: business_id=%s - refusing (fail closed)", business.id)
        return BOT_ACCESS_CHECK_FAILED
    if decision.level == TRADE:
        return None
    return decision.reason or decision.level


async def assert_bot_order_allowed(db: AsyncSession, business: Business, now: datetime | None = None) -> None:
    """
    Backstop for order_creator.create_order_from_cart: raise 403
    EVENT_TRADING_NOT_ALLOWED for an event-plan business that may not trade
    now. Non-event businesses return before any SQL. Called before any order
    row, stock movement or SSE/push event is produced.
    """
    if getattr(business, "plan", None) != EVENT_PLAN:
        return
    await _require_trade_at(db, business.id, now)


@dataclass(frozen=True)
class BotEventContext:
    """What the customer notice names, and the event-local day for "once per day"."""
    event_name: str | None
    local_day: str  # ISO date in the chosen event's timezone (UTC when there is no event)


_EVENT_STATUS_PREFERENCE = {"OPEN": 0, "DRAFT": 1, "CLOSED": 2}


async def bot_event_context(db: AsyncSession, business_id: uuid.UUID, now: datetime | None = None) -> BotEventContext:
    """
    The event to name in the bot's notice: of this business's own (non-
    cancelled) vendor rows, an OPEN event first, then DRAFT, then CLOSED;
    oldest first on a tie. Only this business's rows are read. Never raises.
    """
    now = now or datetime.now(timezone.utc)
    try:
        async with db.begin_nested():
            vendors = (await db.execute(
                select(EventVendor)
                .where(EventVendor.business_id == business_id)
                .options(selectinload(EventVendor.event))
            )).scalars().all()
    except Exception:  # noqa: BLE001
        logger.exception("EVENT_BOT_CONTEXT_FAILED: business_id=%s", business_id)
        vendors = []
    candidates = [
        v.event for v in vendors
        if v.status != "CANCELLED" and v.event is not None and v.event.status != "CANCELLED"
    ]
    if not candidates:
        return BotEventContext(event_name=None, local_day=now.astimezone(timezone.utc).date().isoformat())
    event = sorted(
        candidates,
        key=lambda e: (_EVENT_STATUS_PREFERENCE.get(e.status, 9), e.created_at or now),
    )[0]
    try:
        local_day = now.astimezone(ZoneInfo(event.timezone)).date().isoformat()
    except Exception:  # noqa: BLE001 - a bad zone name must not break the notice
        local_day = now.astimezone(timezone.utc).date().isoformat()
    label = event.name if not event.venue else f"{event.name} ({event.venue})"
    return BotEventContext(event_name=label, local_day=local_day)
