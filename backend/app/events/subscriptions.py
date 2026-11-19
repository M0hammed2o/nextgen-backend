"""
Shared subscription helpers for the admin and vendor self-service routes, so
both apply exactly the same locking, pricing and overlap rules.
"""
from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.core.errors import AppError, NotFoundError
from backend.app.events.rules import Quote, quote
from shared.models.event import Event, EventVendor, EventVendorSubscription


async def load_event_locked(db: AsyncSession, event_id: uuid.UUID) -> Event:
    """Lock the event row (SELECT ... FOR UPDATE) then read its days. Same pattern as admin _load_event(lock=True)."""
    stmt = select(Event).where(Event.id == event_id).options(selectinload(Event.days)).with_for_update(of=Event)
    event = (await db.execute(stmt)).scalar_one_or_none()
    if not event:
        raise NotFoundError("Event", str(event_id))
    return event


def normalise_payment_reference(ref: str) -> str:
    """Strip and upper-case, so 'ab 1' vs ' AB 1' cannot both be accepted as different references."""
    return ref.strip().upper()


async def payment_reference_in_use(db: AsyncSession, reference: str, exclude_id: uuid.UUID | None = None) -> bool:
    """
    Is this (normalised) reference already on a PAID subscription? The stored
    side is normalised in SQL too, so a legacy mixed-case value still matches.
    """
    stmt = select(EventVendorSubscription.id).where(
        EventVendorSubscription.status == "PAID",
        func.upper(func.btrim(EventVendorSubscription.payment_reference)) == reference,
    )
    if exclude_id is not None:
        stmt = stmt.where(EventVendorSubscription.id != exclude_id)
    return (await db.execute(stmt.limit(1))).first() is not None


def is_payment_reference_violation(exc: Exception) -> bool:
    """True only for the partial unique index on a PAID payment_reference (not any other IntegrityError)."""
    return "payment_reference_paid" in str(getattr(exc, "orig", exc))


def quote_or_422(event: Event, category: str, starts_on: date, ends_on: date) -> Quote:
    billable = [d.day for d in event.days if d.is_billable]
    try:
        return quote(category, event.food_day_rate_cents, event.clothing_day_rate_cents,
                     billable, starts_on, ends_on)
    except ValueError as exc:
        raise AppError("INVALID_SUBSCRIPTION_RANGE", str(exc), 422)


async def assert_no_overlap(db: AsyncSession, event_vendor_id: uuid.UUID, starts_on: date, ends_on: date) -> None:
    """Refuse a range that overlaps any non-void subscription of the same vendor (409 OVERLAPS_EXISTING)."""
    clash = (await db.execute(
        select(EventVendorSubscription.id).where(
            EventVendorSubscription.event_vendor_id == event_vendor_id,
            EventVendorSubscription.status != "VOID",
            EventVendorSubscription.starts_on <= ends_on,
            EventVendorSubscription.ends_on >= starts_on,
        ).limit(1)
    )).first()
    if clash:
        raise AppError("OVERLAPS_EXISTING",
                       "these dates overlap an existing subscription for this vendor", 409)


async def find_vendor(db: AsyncSession, event_id: uuid.UUID, business_id: uuid.UUID) -> EventVendor | None:
    return (await db.execute(
        select(EventVendor).where(EventVendor.event_id == event_id, EventVendor.business_id == business_id)
    )).scalar_one_or_none()
