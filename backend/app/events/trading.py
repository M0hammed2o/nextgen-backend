"""
Server-side trading gate for event vendors (souk).

A business on the event plan may only create a real sale or record money
while it holds a PAID subscription covering the current trading date at one of
its events (access level TRADE, rules.py). The level is computed live from the
database on every call, never from a cached flag, and "today" is the event's
own timezone.

Non-event businesses (any plan other than EVENT_PLAN) are never affected.

NOT applied to WhatsApp-originated orders: the protected bot pipeline files
(pipeline.py, order_creator.py, ...) are out of scope for the change that added
this gate, so a bot order is NOT checked here. The earlier assumption that
event vendors "have no WhatsApp number, so that channel is unreachable" was
false (security review H1): an event vendor's owner could connect their own
number through Embedded Signup. The interim mitigation is therefore:
  - every /business/whatsapp/connection mutation is refused for event-plan
    businesses (403 EVENT_WHATSAPP_NOT_AVAILABLE);
  - event vendors are provisioned with is_whatsapp_enabled = false, and
    migration 0065 switched it off for existing event-plan businesses.
OWNER APPROVAL NEEDED: gate bot order creation for event vendors (call
assert_trading_allowed from the bot's order-creation path, which means editing
the protected files). Until then WhatsApp must stay off for event vendors.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from backend.app.events.rules import TRADE, PaidRange, last_paid_trade_day
from backend.app.events.service import EVENT_SUSPENSION_REASON, business_access_decision
from shared.enums import EVENT_PLAN
from shared.models.business import Business
from shared.models.event import Event, EventVendor

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
# Review M-1 (coordinator decision under the owner rule "suspicious offline
# transactions require explicit review"): during the sync-only grace a device
# clock can be backdated, so EVERY offline upload in grace is held for an
# owner/manager instead of posting. Stored on the review, carried to the order.
REASON_POST_EVENT_GRACE_UPLOAD = "post_event_grace_upload"


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
      CANCELLED and has at least one paid TRADE day, the grace ANCHOR ended at
      or before `now` and `now` is before end(anchor) + EVENT_SYNC_GRACE. The
      anchor is the later of the vendor's last paid TRADE day and the event's
      last billable day; end(d) is the end of local date d in the event's
      timezone.

    Why that anchor (review L-3): the reconcile job only suspends once access
    becomes NONE, and for a vendor with any paid day that is after the
    event's LAST billable day (an unpaid day during the event is CONFIGURE,
    not a suspension). Anchoring on the last paid day alone gave a vendor who
    paid only early days little or no grace once it was finally suspended. No
    suspension timestamp is stored (that would be a cached flag plus a schema
    change); the end of the event's last billable day is the earliest moment
    the automatic suspension can happen, and it is derived live.

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
            continue  # never paid for a trading day: nothing legitimate to upload
        anchor = max(last, max(billable))
        ended = datetime.combine(anchor + timedelta(days=1), time(0), tzinfo=ZoneInfo(event.timezone))
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
