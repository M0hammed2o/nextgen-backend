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
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.errors import AppError
from backend.app.events.rules import TRADE
from backend.app.events.service import business_access_decision
from shared.enums import EVENT_PLAN
from shared.models.business import Business

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


def validate_capture_window(
    captured_at: datetime, now: datetime | None = None, max_age: timedelta = OFFLINE_CAPTURE_MAX_AGE,
) -> datetime:
    """
    Return `captured_at` as an aware UTC datetime, or raise 422
    OFFLINE_CAPTURE_TOO_OLD / OFFLINE_CAPTURE_IN_FUTURE. A naive timestamp is
    refused (422 OFFLINE_CAPTURE_NO_TIMEZONE): guessing a zone would let the
    event-day decision move by hours.
    """
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise AppError("OFFLINE_CAPTURE_NO_TIMEZONE", "offline_captured_at must include a timezone offset", 422)
    now = now or datetime.now(timezone.utc)
    captured = captured_at.astimezone(timezone.utc)
    if captured < now - max_age:
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
    """
    now = now or datetime.now(timezone.utc)
    plan = (await db.execute(select(Business.plan).where(Business.id == business_id))).scalar_one_or_none()
    is_event = plan == EVENT_PLAN
    captured = validate_capture_window(
        captured_at, now, EVENT_OFFLINE_CAPTURE_MAX_AGE if is_event else OFFLINE_CAPTURE_MAX_AGE,
    )
    reasons: list[str] = []
    if is_event:
        await _require_trade_at(db, business_id, captured)
        if (await business_access_decision(db, business_id, now)).level != TRADE:
            reasons.append(REASON_CAPTURE_DAY_UNPAID_AT_SYNC)
    if captured < now - OFFLINE_LATE_SYNC_THRESHOLD:
        reasons.append(REASON_LATE_SYNC)
    return OfflineCaptureGate(captured_at=captured, reasons=reasons)


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
