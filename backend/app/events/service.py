"""
Automatic event-vendor activation and expiry (no manual NextGen step).

``reconcile_event_vendor_access`` runs hourly from the worker. For every
vendor at a DRAFT, OPEN or CLOSED event, it recomputes the vendor's status
from its PAID subscriptions with ``rules.vendor_status``. It then switches
the vendor's business on or off, following the same reversible convention
as the trial sweep:

- It only manages businesses on the event plan (``EVENT_PLAN``). An ordinary
  restaurant is never touched, even if it once traded at a souk.
- It suspends with the exact reason ``EVENT_SUBSCRIPTION_ENDED``, and only
  ever lifts a suspension carrying that exact reason. An administrator's
  suspension, or one for any other reason, is never overridden.
- Access has three levels (rules.py): NONE suspends; CONFIGURE and TRADE both
  leave the business active (CONFIGURE may log in but the trading gate in
  backend/app/events/trading.py refuses real sales).
- A business trading at several events takes the highest level of any of them.
- Nothing is deleted. A suspended vendor's products, stock and history remain,
  so renewing at a future event restores everything.

Idempotent: running it twice in a row changes nothing the second time.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.events.rules import (
    NONE,
    AccessDecision,
    PaidRange,
    best_decision,
    evaluate_access,
    vendor_status,
)
from shared.enums import EVENT_PLAN
from shared.models.business import Business
from shared.models.event import Event, EventVendor

logger = logging.getLogger(__name__)

EVENT_SUSPENSION_REASON = "EVENT_SUBSCRIPTION_ENDED"
# Set by a SUPER_ADMIN suspending one vendor (admin_api routes_events). The
# reconcile job never lifts this reason, and only reinstate does.
EVENT_ADMIN_SUSPENSION_REASON = "EVENT_ADMIN_SUSPENDED"
# CANCELLED is included so cancelling an event ends its vendors' access
# (security review M2); a cancelled event never grants access.
_MANAGED_EVENT_STATUSES = ("DRAFT", "OPEN", "CLOSED", "CANCELLED")


@dataclass
class ReconcileResult:
    vendor_status_changes: int = 0
    businesses_suspended: list[uuid.UUID] = field(default_factory=list)
    businesses_reactivated: list[uuid.UUID] = field(default_factory=list)


async def reconcile_event_vendor_access(db: AsyncSession, now: datetime | None = None) -> ReconcileResult:
    """Recompute vendor statuses and switch event-plan businesses on/off. Caller commits."""
    now = now or datetime.now(timezone.utc)
    result = ReconcileResult()

    events = (
        await db.execute(
            select(Event)
            .where(Event.status.in_(_MANAGED_EVENT_STATUSES))
            .options(
                selectinload(Event.days),
                selectinload(Event.vendors).selectinload(EventVendor.subscriptions),
            )
        )
    ).scalars().all()

    decisions_by_business: dict[uuid.UUID, list[AccessDecision]] = defaultdict(list)
    for event in events:
        local_day = now.astimezone(ZoneInfo(event.timezone)).date()
        billable = {d.day for d in event.days if d.is_billable}
        for vendor in event.vendors:
            paid = [PaidRange(s.starts_on, s.ends_on) for s in vendor.subscriptions if s.status == "PAID"]
            cancelled = vendor.status == "CANCELLED" or event.status == "CANCELLED"
            new_status = vendor_status(local_day, billable, paid, cancelled=cancelled, prep_days=event.prep_days)
            if new_status != vendor.status:
                vendor.status = new_status
                vendor.status_changed_at = now
                result.vendor_status_changes += 1
            decisions_by_business[vendor.business_id].append(
                evaluate_access(local_day, billable, paid, event.prep_days, cancelled=cancelled)
            )

    if not decisions_by_business:
        return result

    businesses = (
        await db.execute(
            select(Business).where(Business.id.in_(list(decisions_by_business)), Business.plan == EVENT_PLAN)
        )
    ).scalars().all()

    for business in businesses:
        should_have_access = best_decision(decisions_by_business[business.id]).level != NONE
        if should_have_access and not business.is_active and business.suspended_reason == EVENT_SUSPENSION_REASON:
            business.is_active = True
            business.suspended_reason = None
            result.businesses_reactivated.append(business.id)
        elif not should_have_access and business.is_active:
            business.is_active = False
            business.suspended_reason = EVENT_SUSPENSION_REASON
            result.businesses_suspended.append(business.id)
        # Every other combination is left alone, deliberately: a business
        # suspended for another reason stays suspended, and an active one
        # with access stays active.

    if result.businesses_suspended or result.businesses_reactivated or result.vendor_status_changes:
        logger.info(
            "Event access reconcile: %d vendor status change(s), %d suspended, %d reactivated",
            result.vendor_status_changes, len(result.businesses_suspended), len(result.businesses_reactivated),
        )
    return result


async def business_access_decision(db: AsyncSession, business_id: uuid.UUID,
                                   now: datetime | None = None) -> AccessDecision:
    """
    Live access decision for one business across every event it is a vendor at
    (highest level wins). Computed from the database now; no cached flag is
    trusted. A business with no event vendor row is NONE / NO_EVENT.
    """
    now = now or datetime.now(timezone.utc)
    events = (
        await db.execute(
            select(Event)
            .join(EventVendor, EventVendor.event_id == Event.id)
            .where(EventVendor.business_id == business_id)
            .options(
                selectinload(Event.days),
                selectinload(Event.vendors).selectinload(EventVendor.subscriptions),
            )
        )
    ).scalars().all()
    decisions: list[AccessDecision] = []
    for event in events:
        local_day = now.astimezone(ZoneInfo(event.timezone)).date()
        billable = {d.day for d in event.days if d.is_billable}
        for vendor in event.vendors:
            if vendor.business_id != business_id:
                continue
            paid = [PaidRange(s.starts_on, s.ends_on) for s in vendor.subscriptions if s.status == "PAID"]
            cancelled = vendor.status == "CANCELLED" or event.status == "CANCELLED"
            decisions.append(evaluate_access(local_day, billable, paid, event.prep_days, cancelled=cancelled))
    return best_decision(decisions)
