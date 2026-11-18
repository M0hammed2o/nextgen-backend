"""
Real-Postgres tests for the souk foundation (migration 0062_events_foundation,
backend/app/events/service.py): constraints hold, and vendors are activated
and expired automatically, without ever touching a business that is not on
the event plan or a suspension imposed for any other reason.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from backend.app.events.rules import quote
from backend.app.events.service import EVENT_SUSPENSION_REASON, reconcile_event_vendor_access
from shared.utils import generate_business_code
from shared.enums import EVENT_PLAN
from shared.models.business import Business
from shared.models.event import Event, EventDay, EventVendor, EventVendorSubscription

pytestmark = pytest.mark.asyncio

SAST = ZoneInfo("Africa/Johannesburg")
START = date(2027, 3, 1)
DAYS = [START + timedelta(days=i) for i in range(19)]


def _noon(d: date) -> datetime:
    """12:00 SAST on day d, as UTC: the moment the hourly job would run."""
    return datetime.combine(d, time(12, 0), tzinfo=SAST).astimezone(timezone.utc)


async def _business(db, plan=EVENT_PLAN, industry="EVENT_FOOD", **kw) -> Business:
    unique = uuid.uuid4().hex[:8]
    b = Business(
        name=f"Souk Vendor {unique}", slug=f"souk-vendor-{unique}", business_code=generate_business_code(),
        currency="ZAR", timezone="Africa/Johannesburg", plan=plan, industry=industry,
        billing_status="ACTIVE", **kw,
    )
    db.add(b)
    await db.flush()
    return b


@pytest.fixture
async def souk(db):
    """A 19-day souk at R100 food / R50 clothing, removed (with everything under it) afterwards."""
    unique = uuid.uuid4().hex[:8]
    event = Event(
        name=f"Test Souk {unique}", slug=f"test-souk-{unique}", status="OPEN",
        food_day_rate_cents=10_000, clothing_day_rate_cents=5_000,
    )
    db.add(event)
    await db.flush()
    db.add_all([EventDay(event_id=event.id, day=d, is_billable=True) for d in DAYS])
    await db.commit()
    await db.refresh(event)
    created_businesses: list[uuid.UUID] = []
    event_id = event.id  # captured now: rollback() below expires the instance
    yield event, created_businesses
    await db.rollback()
    await db.execute(delete(Event).where(Event.id == event_id))
    if created_businesses:
        await db.execute(delete(Business).where(Business.id.in_(created_businesses)))
    await db.commit()


async def _vendor(db, event, created, category="FOOD", paid_ranges=(), plan=EVENT_PLAN, stall=None, **biz_kw):
    biz = await _business(db, plan=plan, industry="EVENT_FOOD" if category == "FOOD" else "EVENT_RETAIL", **biz_kw)
    created.append(biz.id)
    vendor = EventVendor(event_id=event.id, business_id=biz.id, category=category, stall_code=stall)
    db.add(vendor)
    await db.flush()
    for starts_on, ends_on in paid_ranges:
        q = quote(category, event.food_day_rate_cents, event.clothing_day_rate_cents, DAYS, starts_on, ends_on)
        db.add(EventVendorSubscription(
            event_vendor_id=vendor.id, business_id=biz.id, starts_on=starts_on, ends_on=ends_on,
            day_rate_cents=q.day_rate_cents, billable_days=q.billable_days, amount_cents=q.amount_cents,
            status="PAID", paid_at=datetime.now(timezone.utc), payment_reference="test",
        ))
    await db.commit()
    return biz, vendor


async def _state(db, biz_id, vendor_id):
    db.expire_all()
    biz = (await db.execute(select(Business).where(Business.id == biz_id))).scalar_one()
    vendor = (await db.execute(select(EventVendor).where(EventVendor.id == vendor_id))).scalar_one()
    return biz.is_active, biz.suspended_reason, vendor.status


# ── Lifecycle ─────────────────────────────────────────────────────────────────

async def test_vendor_is_active_on_paid_days_and_suspended_when_cover_ends(db, souk):
    event, created = souk
    biz, vendor = await _vendor(db, event, created, paid_ranges=[(DAYS[0], DAYS[4])])

    await reconcile_event_vendor_access(db, _noon(DAYS[2])); await db.commit()
    assert await _state(db, biz.id, vendor.id) == (True, None, "ACTIVE")

    res = await reconcile_event_vendor_access(db, _noon(DAYS[5])); await db.commit()
    assert biz.id in res.businesses_suspended
    assert await _state(db, biz.id, vendor.id) == (False, EVENT_SUSPENSION_REASON, "EXPIRED")


async def test_renewal_reactivates_automatically(db, souk):
    event, created = souk
    biz, vendor = await _vendor(db, event, created, paid_ranges=[(DAYS[0], DAYS[4])])
    await reconcile_event_vendor_access(db, _noon(DAYS[5])); await db.commit()
    assert (await _state(db, biz.id, vendor.id))[0] is False

    q = quote("FOOD", 10_000, 5_000, DAYS, DAYS[5], DAYS[18])
    db.add(EventVendorSubscription(
        event_vendor_id=vendor.id, business_id=biz.id, starts_on=DAYS[5], ends_on=DAYS[18],
        day_rate_cents=q.day_rate_cents, billable_days=q.billable_days, amount_cents=q.amount_cents,
        status="PAID", paid_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    res = await reconcile_event_vendor_access(db, _noon(DAYS[6])); await db.commit()
    assert biz.id in res.businesses_reactivated
    assert await _state(db, biz.id, vendor.id) == (True, None, "ACTIVE")


async def test_setup_before_the_event_is_allowed_unpaid(db, souk):
    event, created = souk
    biz, vendor = await _vendor(db, event, created)
    await reconcile_event_vendor_access(db, _noon(START - timedelta(days=2))); await db.commit()
    assert await _state(db, biz.id, vendor.id) == (True, None, "PENDING")
    await reconcile_event_vendor_access(db, _noon(START)); await db.commit()
    assert await _state(db, biz.id, vendor.id) == (False, EVENT_SUSPENSION_REASON, "UNPAID")


async def test_an_admin_suspension_is_never_lifted(db, souk):
    event, created = souk
    biz, vendor = await _vendor(
        db, event, created, paid_ranges=[(DAYS[0], DAYS[18])], is_active=False, suspended_reason="Fraud review",
    )
    res = await reconcile_event_vendor_access(db, _noon(DAYS[3])); await db.commit()
    assert biz.id not in res.businesses_reactivated
    is_active, reason, _ = await _state(db, biz.id, vendor.id)
    assert (is_active, reason) == (False, "Fraud review")


async def test_a_business_not_on_the_event_plan_is_never_suspended(db, souk):
    """A permanent restaurant that also traded at the souk keeps its normal access after the event."""
    event, created = souk
    biz, vendor = await _vendor(db, event, created, paid_ranges=[(DAYS[0], DAYS[1])], plan="GROWTH")
    res = await reconcile_event_vendor_access(db, _noon(DAYS[10])); await db.commit()
    assert biz.id not in res.businesses_suspended
    is_active, reason, status = await _state(db, biz.id, vendor.id)
    assert (is_active, reason) == (True, None)
    assert status == "EXPIRED"  # the event membership still expires; the business does not


async def test_reconcile_is_idempotent(db, souk):
    event, created = souk
    await _vendor(db, event, created, paid_ranges=[(DAYS[0], DAYS[4])])
    first = await reconcile_event_vendor_access(db, _noon(DAYS[7])); await db.commit()
    second = await reconcile_event_vendor_access(db, _noon(DAYS[7])); await db.commit()
    assert first.businesses_suspended
    assert (second.vendor_status_changes, second.businesses_suspended, second.businesses_reactivated) == (0, [], [])


async def test_access_at_any_event_keeps_a_multi_event_vendor_active(db, souk):
    event, created = souk
    biz, vendor = await _vendor(db, event, created, paid_ranges=[(DAYS[0], DAYS[1])])
    unique = uuid.uuid4().hex[:8]
    other = Event(name=f"Other {unique}", slug=f"other-{unique}", status="OPEN",
                  food_day_rate_cents=10_000, clothing_day_rate_cents=5_000)
    db.add(other)
    await db.flush()
    other_id = other.id
    db.add_all([EventDay(event_id=other.id, day=d, is_billable=True) for d in DAYS])
    other_vendor = EventVendor(event_id=other.id, business_id=biz.id, category="FOOD")
    db.add(other_vendor)
    await db.flush()
    db.add(EventVendorSubscription(
        event_vendor_id=other_vendor.id, business_id=biz.id, starts_on=DAYS[0], ends_on=DAYS[18],
        day_rate_cents=10_000, billable_days=19, amount_cents=190_000, status="PAID", paid_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    try:
        await reconcile_event_vendor_access(db, _noon(DAYS[10])); await db.commit()
        is_active, _, status_first_event = await _state(db, biz.id, vendor.id)
        assert is_active is True and status_first_event == "EXPIRED"
    finally:
        await db.rollback()
        await db.execute(delete(Event).where(Event.id == other_id))
        await db.commit()


# ── Constraints (migration 0062) ──────────────────────────────────────────────

async def test_amount_must_equal_rate_times_days(db, souk):
    event, created = souk
    biz, vendor = await _vendor(db, event, created)
    db.add(EventVendorSubscription(
        event_vendor_id=vendor.id, business_id=biz.id, starts_on=DAYS[0], ends_on=DAYS[1],
        day_rate_cents=10_000, billable_days=2, amount_cents=1, status="UNPAID",
    ))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_paid_requires_paid_at(db, souk):
    event, created = souk
    biz, vendor = await _vendor(db, event, created)
    db.add(EventVendorSubscription(
        event_vendor_id=vendor.id, business_id=biz.id, starts_on=DAYS[0], ends_on=DAYS[0],
        day_rate_cents=10_000, billable_days=1, amount_cents=10_000, status="PAID", paid_at=None,
    ))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_stall_codes_are_unique_per_event(db, souk):
    event, created = souk
    await _vendor(db, event, created, stall="A12")
    with pytest.raises(IntegrityError):
        await _vendor(db, event, created, stall="A12")


async def test_industry_is_checked(db):
    unique = uuid.uuid4().hex[:8]
    db.add(Business(name=f"X {unique}", slug=f"x-{unique}", business_code=generate_business_code(),
                    currency="ZAR", timezone="Africa/Johannesburg", industry="SPACESHIPS"))
    with pytest.raises(IntegrityError):
        await db.commit()
