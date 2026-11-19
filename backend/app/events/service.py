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

Concurrency (security review L1). Every write is a compare-and-set against the
value that was read (vendor ``status``; business ``is_active`` and
``suspended_reason``), so a change committed by another transaction between
this job's read and write is never overwritten. The hourly worker run also
takes ``FOR UPDATE SKIP LOCKED`` on the event rows (``lock_events=True``): an
event whose row is locked by an in-flight mark-paid / calendar change is left
to the next run, and so is every business that is a vendor at such an event
(its access would otherwise be decided from incomplete data). Request-path
callers do not lock events, to keep lock order simple (they already hold their
own row locks); the compare-and-set protects them.
"""
from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from backend.app.events.rules import (
    CONFIGURE,
    NONE,
    TRADE,
    AccessDecision,
    PaidRange,
    best_decision,
    evaluate_access,
    vendor_status,
)
from backend.app.services.email_verification import EMAIL_NOT_VERIFIED, owner_email_unverified
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


async def reconcile_event_vendor_access(
    db: AsyncSession, now: datetime | None = None, *, lock_events: bool = False
) -> ReconcileResult:
    """Recompute vendor statuses and switch event-plan businesses on/off. Caller commits."""
    now = now or datetime.now(timezone.utc)
    result = ReconcileResult()

    skipped_event_ids: list[uuid.UUID] = []
    query = (
        select(Event)
        .where(Event.status.in_(_MANAGED_EVENT_STATUSES))
        .options(
            selectinload(Event.days),
            selectinload(Event.vendors).selectinload(EventVendor.subscriptions),
        )
    )
    if lock_events:
        all_ids = set((await db.execute(
            select(Event.id).where(Event.status.in_(_MANAGED_EVENT_STATUSES))
        )).scalars())
        query = query.order_by(Event.id).with_for_update(skip_locked=True, of=Event)
        events = (await db.execute(query)).scalars().all()
        skipped_event_ids = list(all_ids - {e.id for e in events})
    else:
        events = (await db.execute(query)).scalars().all()

    decisions_by_business: dict[uuid.UUID, list[AccessDecision]] = defaultdict(list)
    for event in events:
        local_day = now.astimezone(ZoneInfo(event.timezone)).date()
        billable = {d.day for d in event.days if d.is_billable}
        for vendor in event.vendors:
            paid = [PaidRange(s.starts_on, s.ends_on) for s in vendor.subscriptions if s.status == "PAID"]
            cancelled = vendor.status == "CANCELLED" or event.status == "CANCELLED"
            new_status = vendor_status(local_day, billable, paid, cancelled=cancelled, prep_days=event.prep_days)
            if new_status != vendor.status:
                # Compare-and-set: only if nobody changed the status since we read it.
                changed = await db.execute(
                    update(EventVendor)
                    .where(EventVendor.id == vendor.id, EventVendor.status == vendor.status)
                    .values(status=new_status, status_changed_at=now)
                    .execution_options(synchronize_session=False)
                )
                if changed.rowcount == 1:
                    set_committed_value(vendor, "status", new_status)
                    set_committed_value(vendor, "status_changed_at", now)
                    result.vendor_status_changes += 1
            decisions_by_business[vendor.business_id].append(
                evaluate_access(local_day, billable, paid, event.prep_days, cancelled=cancelled)
            )

    if not decisions_by_business:
        return result

    # A business that is also a vendor at an event we could not lock is not
    # touched this run: its decision would be missing that event.
    excluded: set[uuid.UUID] = set()
    if skipped_event_ids:
        excluded = set((await db.execute(
            select(EventVendor.business_id).where(EventVendor.event_id.in_(skipped_event_ids))
        )).scalars())

    businesses = (
        await db.execute(
            select(Business).where(Business.id.in_(list(decisions_by_business)), Business.plan == EVENT_PLAN)
        )
    ).scalars().all()

    for business in businesses:
        if business.id in excluded:
            continue
        should_have_access = best_decision(decisions_by_business[business.id]).level != NONE
        await _apply_business_access(db, business, should_have_access, result)

    if result.businesses_suspended or result.businesses_reactivated or result.vendor_status_changes:
        logger.info(
            "Event access reconcile: %d vendor status change(s), %d suspended, %d reactivated",
            result.vendor_status_changes, len(result.businesses_suspended), len(result.businesses_reactivated),
        )
    return result


async def _apply_business_access(
    db: AsyncSession, business: Business, should_have_access: bool, result: ReconcileResult,
) -> None:
    """Compare-and-set the business's active flag from the access decision (shared by both reconcilers)."""
    old_active, old_reason = business.is_active, business.suspended_reason
    if should_have_access and not old_active and old_reason == EVENT_SUSPENSION_REASON:
        new_active, new_reason, bucket = True, None, result.businesses_reactivated
    elif not should_have_access and old_active:
        new_active, new_reason, bucket = False, EVENT_SUSPENSION_REASON, result.businesses_suspended
    else:
        # Every other combination is left alone, deliberately: a business
        # suspended for another reason stays suspended, and an active one
        # with access stays active.
        return
    changed = await db.execute(
        update(Business)
        .where(
            Business.id == business.id,
            Business.is_active == old_active,
            Business.suspended_reason.is_not_distinct_from(old_reason),
        )
        .values(is_active=new_active, suspended_reason=new_reason)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount == 1:
        set_committed_value(business, "is_active", new_active)
        set_committed_value(business, "suspended_reason", new_reason)
        bucket.append(business.id)


async def reconcile_single_business_access(
    db: AsyncSession, business_id: uuid.UUID, now: datetime | None = None,
) -> ReconcileResult:
    """
    The same decision and compare-and-set writes as reconcile_event_vendor_access,
    but for ONE business: only that business's own EventVendor rows and its own
    Business row can be written (souk security review M-2: a public request must
    never write other tenants' rows). business_id is chosen by the caller from
    state it just created, never from client input. Caller commits.
    """
    now = now or datetime.now(timezone.utc)
    result = ReconcileResult()
    vendors = (await db.execute(
        select(EventVendor)
        .join(Event, Event.id == EventVendor.event_id)
        .where(EventVendor.business_id == business_id, Event.status.in_(_MANAGED_EVENT_STATUSES))
        .options(selectinload(EventVendor.subscriptions), selectinload(EventVendor.event).selectinload(Event.days))
    )).scalars().all()
    if not vendors:
        return result
    decisions: list[AccessDecision] = []
    for vendor in vendors:
        event = vendor.event
        local_day = now.astimezone(ZoneInfo(event.timezone)).date()
        billable = {d.day for d in event.days if d.is_billable}
        paid = [PaidRange(s.starts_on, s.ends_on) for s in vendor.subscriptions if s.status == "PAID"]
        cancelled = vendor.status == "CANCELLED" or event.status == "CANCELLED"
        new_status = vendor_status(local_day, billable, paid, cancelled=cancelled, prep_days=event.prep_days)
        if new_status != vendor.status:
            changed = await db.execute(
                update(EventVendor)
                .where(EventVendor.id == vendor.id, EventVendor.status == vendor.status)
                .values(status=new_status, status_changed_at=now)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount == 1:
                set_committed_value(vendor, "status", new_status)
                set_committed_value(vendor, "status_changed_at", now)
                result.vendor_status_changes += 1
        decisions.append(evaluate_access(local_day, billable, paid, event.prep_days, cancelled=cancelled))
    business = (await db.execute(
        select(Business).where(Business.id == business_id, Business.plan == EVENT_PLAN)
    )).scalar_one_or_none()
    if business is not None:
        await _apply_business_access(db, business, best_decision(decisions).level != NONE, result)
    return result


async def business_access_decision(db: AsyncSession, business_id: uuid.UUID,
                                   now: datetime | None = None) -> AccessDecision:
    """
    Live access decision for one business across every event it is a vendor at
    (highest level wins). Computed from the database now; no cached flag is
    trusted. A business with no event vendor row is NONE / NO_EVENT.
    """
    now = now or datetime.now(timezone.utc)
    # Filtered in SQL by business_id (security review L2): the cost is this
    # business's own vendor rows, not every vendor at its events. Event.vendors
    # is deliberately not populated here, so a partial collection can never be
    # left in the session's identity map for a later full reconcile to trust.
    vendors = (
        await db.execute(
            select(EventVendor)
            .where(EventVendor.business_id == business_id)
            .options(
                selectinload(EventVendor.subscriptions),
                selectinload(EventVendor.event).selectinload(Event.days),
            )
        )
    ).scalars().all()
    decisions: list[AccessDecision] = []
    for vendor in vendors:
        event = vendor.event
        local_day = now.astimezone(ZoneInfo(event.timezone)).date()
        billable = {d.day for d in event.days if d.is_billable}
        paid = [PaidRange(s.starts_on, s.ends_on) for s in vendor.subscriptions if s.status == "PAID"]
        cancelled = vendor.status == "CANCELLED" or event.status == "CANCELLED"
        decisions.append(evaluate_access(local_day, billable, paid, event.prep_days, cancelled=cancelled))
    decision = best_decision(decisions)
    if decision.level == TRADE:
        # Souk email verification: a self-registered vendor whose owner has not
        # verified their email may configure but never trade. Computed live like
        # everything above, so it flows through assert_trading_allowed (POS,
        # orders, payment intents, offline sync) with no caller changes. NONE
        # and other CONFIGURE reasons are left as they are (they are the more
        # fundamental answer; GET /business/event-access reports verification
        # separately).
        if await owner_email_unverified(db, business_id):
            return AccessDecision(CONFIGURE, EMAIL_NOT_VERIFIED)
    return decision
