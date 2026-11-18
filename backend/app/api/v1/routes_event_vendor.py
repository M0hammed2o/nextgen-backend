"""
Souk vendor self-service: quote, claim payment for, and cancel own event
subscriptions.

    GET  /v1/business/event-subscriptions
    POST /v1/business/event-subscriptions
    POST /v1/business/event-subscriptions/{id}/payment-claim
    POST /v1/business/event-subscriptions/{id}/cancel

Tenant isolation: business_id comes ONLY from the JWT (``user.business_id``).
Every read and write filters on it; a subscription that belongs to another
business is indistinguishable from one that does not exist (404).

These routes deliberately do NOT use the trading gate: a vendor pays during
CONFIGURE access, which is exactly when it needs them. They never mark
anything paid. A payment claim only records the vendor's reference; a
SUPER_ADMIN confirms it with the existing admin mark-paid route, which is the
only thing that grants TRADE access.

Lock order matches the admin routes: event row first, then the subscription.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.errors import AppError, NotFoundError
from backend.app.core.rbac import AuthUser, require_owner_or_manager
from backend.app.db.session import get_db
from backend.app.events.subscriptions import (
    assert_no_overlap,
    find_vendor,
    load_event_locked,
    normalise_payment_reference,
    payment_reference_in_use,
    quote_or_422,
)
from shared.enums import AuditScope
from shared.models.audit import AuditEvent
from shared.models.event import Event, EventVendor, EventVendorSubscription

router = APIRouter(prefix="/business/event-subscriptions", tags=["event-vendor-self-service"])


class SubscriptionRequest(BaseModel):
    event_id: uuid.UUID
    starts_on: date
    ends_on: date


class PaymentClaim(BaseModel):
    reference: str = Field(min_length=1, max_length=255)

    @field_validator("reference")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = normalise_payment_reference(v)
        if not v:
            raise ValueError("reference must not be blank")
        return v


def _sub_out(s: EventVendorSubscription) -> dict:
    return {"id": s.id, "starts_on": s.starts_on, "ends_on": s.ends_on, "billable_days": s.billable_days,
            "day_rate_cents": s.day_rate_cents, "amount_cents": s.amount_cents, "status": s.status,
            "paid_at": s.paid_at, "payment_claim_reference": s.payment_claim_reference,
            "payment_claimed_at": s.payment_claimed_at}


def _audit(user: AuthUser, action: str, sub: EventVendorSubscription, diff: dict) -> AuditEvent:
    return AuditEvent(scope=AuditScope.BUSINESS.value, business_id=user.business_id, actor_user_id=user.user_id,
                      action=action, target_type="event_vendor_subscription", target_id=sub.id, diff_json=diff)


@router.get("")
async def list_event_subscriptions(user: AuthUser = Depends(require_owner_or_manager),
                                   db: AsyncSession = Depends(get_db)):
    """This business's subscriptions across its events, with the per-event quote options."""
    vendors = (await db.execute(
        select(EventVendor)
        .where(EventVendor.business_id == user.business_id)
        .options(selectinload(EventVendor.subscriptions), selectinload(EventVendor.event).selectinload(Event.days))
    )).scalars().all()
    events = []
    for v in vendors:
        ev = v.event
        billable = sorted(d.day for d in ev.days if d.is_billable)
        rate = ev.food_day_rate_cents if v.category == "FOOD" else ev.clothing_day_rate_cents
        events.append({
            "event_id": ev.id, "event_name": ev.name, "venue": ev.venue, "timezone": ev.timezone,
            "event_status": ev.status, "vendor_status": v.status, "category": v.category,
            "can_subscribe": ev.status != "CANCELLED" and v.status != "CANCELLED" and bool(billable),
            "quote_options": {"day_rate_cents": rate, "billable_days": billable,
                              "billable_day_count": len(billable)},
            "subscriptions": [_sub_out(s) for s in sorted(v.subscriptions, key=lambda x: x.starts_on)],
        })
    return {"events": events}


@router.post("", status_code=201)
async def create_event_subscription(body: SubscriptionRequest, user: AuthUser = Depends(require_owner_or_manager),
                                    db: AsyncSession = Depends(get_db)):
    """Quote an UNPAID subscription for [starts_on, ends_on]. Price comes only from rules.quote."""
    # Existence of the event is revealed only to a vendor registered at it.
    if await find_vendor(db, body.event_id, user.business_id) is None:
        raise NotFoundError("Event", str(body.event_id))
    event = await load_event_locked(db, body.event_id)
    vendor = await find_vendor(db, body.event_id, user.business_id)
    if vendor is None or vendor.status == "CANCELLED":
        raise NotFoundError("Event", str(body.event_id))
    if event.status == "CANCELLED":
        raise AppError("EVENT_CANCELLED", "this event was cancelled", 409)
    q = quote_or_422(event, vendor.category, body.starts_on, body.ends_on)
    await assert_no_overlap(db, vendor.id, body.starts_on, body.ends_on)
    sub = EventVendorSubscription(
        event_vendor_id=vendor.id, business_id=user.business_id, starts_on=body.starts_on, ends_on=body.ends_on,
        day_rate_cents=q.day_rate_cents, billable_days=q.billable_days, amount_cents=q.amount_cents, status="UNPAID",
    )
    db.add(sub)
    await db.flush()
    db.add(_audit(user, "event.subscription_requested", sub,
                  {"amount_cents": q.amount_cents, "billable_days": q.billable_days}))
    out = _sub_out(sub)
    await db.commit()
    return out


async def _own_subscription_locked(db: AsyncSession, user: AuthUser, subscription_id: uuid.UUID) -> EventVendorSubscription:
    """The caller's own subscription, locked (event row first). 404 for anyone else's."""
    event_id = (await db.execute(
        select(EventVendor.event_id)
        .join(EventVendorSubscription, EventVendorSubscription.event_vendor_id == EventVendor.id)
        .where(EventVendorSubscription.id == subscription_id,
               EventVendorSubscription.business_id == user.business_id,
               EventVendor.business_id == user.business_id)
    )).scalar_one_or_none()
    if event_id is None:
        raise NotFoundError("Event subscription", str(subscription_id))
    await load_event_locked(db, event_id)
    sub = (await db.execute(
        select(EventVendorSubscription)
        .where(EventVendorSubscription.id == subscription_id,
               EventVendorSubscription.business_id == user.business_id)
        .with_for_update()
    )).scalar_one_or_none()
    if sub is None:
        raise NotFoundError("Event subscription", str(subscription_id))
    return sub


@router.post("/{subscription_id}/payment-claim")
async def claim_payment(subscription_id: uuid.UUID, body: PaymentClaim,
                        user: AuthUser = Depends(require_owner_or_manager), db: AsyncSession = Depends(get_db)):
    """Record the reference the vendor paid with. Does NOT mark the subscription paid."""
    sub = await _own_subscription_locked(db, user, subscription_id)
    if sub.status != "UNPAID":
        raise AppError("SUBSCRIPTION_NOT_UNPAID", f"only an unpaid subscription can be claimed (this one is {sub.status})", 409)
    if await payment_reference_in_use(db, body.reference):
        raise AppError("PAYMENT_REFERENCE_USED", "this payment reference is already used on a paid subscription", 409)
    if sub.payment_claim_reference == body.reference:
        return {**_sub_out(sub), "already_claimed": True}
    sub.payment_claim_reference = body.reference
    sub.payment_claimed_at = datetime.now(timezone.utc)
    db.add(_audit(user, "event.subscription_payment_claimed", sub, {}))
    await db.flush()
    out = {**_sub_out(sub), "already_claimed": False}
    await db.commit()
    return out


@router.post("/{subscription_id}/cancel")
async def cancel_subscription(subscription_id: uuid.UUID, user: AuthUser = Depends(require_owner_or_manager),
                              db: AsyncSession = Depends(get_db)):
    """Void the vendor's own UNPAID subscription (idempotent for one already void)."""
    sub = await _own_subscription_locked(db, user, subscription_id)
    if sub.status == "VOID":
        return _sub_out(sub)
    if sub.status != "UNPAID":
        raise AppError("SUBSCRIPTION_NOT_UNPAID", "only an unpaid subscription can be cancelled here", 409)
    sub.status = "VOID"
    db.add(_audit(user, "event.subscription_cancelled_by_vendor", sub, {}))
    await db.flush()
    out = _sub_out(sub)
    await db.commit()
    return out
