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
- A business trading at several events keeps access if *any* of them grants it.
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

from backend.app.events.rules import PaidRange, has_access, vendor_status
from shared.enums import EVENT_PLAN
from shared.models.business import Business
from shared.models.event import Event, EventVendor

logger = logging.getLogger(__name__)

EVENT_SUSPENSION_REASON = "EVENT_SUBSCRIPTION_ENDED"
_MANAGED_EVENT_STATUSES = ("DRAFT", "OPEN", "CLOSED")


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

    access_by_business: dict[uuid.UUID, bool] = defaultdict(bool)
    for event in events:
        local_day = now.astimezone(ZoneInfo(event.timezone)).date()
        billable = {d.day for d in event.days if d.is_billable}
        for vendor in event.vendors:
            paid = [PaidRange(s.starts_on, s.ends_on) for s in vendor.subscriptions if s.status == "PAID"]
            cancelled = vendor.status == "CANCELLED"
            new_status = vendor_status(local_day, billable, paid, cancelled=cancelled)
            if new_status != vendor.status:
                vendor.status = new_status
                vendor.status_changed_at = now
                result.vendor_status_changes += 1
            grants = (not cancelled) and has_access(local_day, billable, paid)
            access_by_business[vendor.business_id] = access_by_business[vendor.business_id] or grants

    if not access_by_business:
        return result

    businesses = (
        await db.execute(
            select(Business).where(Business.id.in_(list(access_by_business)), Business.plan == EVENT_PLAN)
        )
    ).scalars().all()

    for business in businesses:
        should_have_access = access_by_business[business.id]
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
