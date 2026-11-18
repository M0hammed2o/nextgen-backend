"""
Pure event rules (backend/app/events/rules.py): what a vendor pays, and on
which days a vendor may trade. Owner decisions 2026-11-18: R100/day food,
R50/day clothing, VAT-inclusive; a subscribed day is a billable event day
whether or not the vendor trades.
"""
from datetime import date, timedelta

import pytest

from backend.app.events.rules import (
    CLOTHING,
    FOOD,
    PaidRange,
    billable_days_in_range,
    has_access,
    quote,
    vendor_status,
)

START = date(2027, 3, 1)
NINETEEN_DAYS = {START + timedelta(days=i) for i in range(19)}
LAST = START + timedelta(days=18)
FOOD_RATE, CLOTHING_RATE = 10_000, 5_000  # cents, VAT-inclusive


# ── Pricing ───────────────────────────────────────────────────────────────────

def test_full_19_day_event_food_vendor_pays_r1900():
    q = quote(FOOD, FOOD_RATE, CLOTHING_RATE, NINETEEN_DAYS, START, LAST)
    assert (q.day_rate_cents, q.billable_days, q.amount_cents) == (10_000, 19, 190_000)


def test_full_19_day_event_clothing_vendor_pays_r950():
    q = quote(CLOTHING, FOOD_RATE, CLOTHING_RATE, NINETEEN_DAYS, START, LAST)
    assert (q.billable_days, q.amount_cents) == (19, 95_000)


def test_one_day_subscription():
    q = quote(FOOD, FOOD_RATE, CLOTHING_RATE, NINETEEN_DAYS, START, START)
    assert (q.billable_days, q.amount_cents) == (1, 10_000)


def test_only_billable_event_days_are_charged():
    calendar = {START, START + timedelta(days=1), START + timedelta(days=5)}  # non-contiguous
    q = quote(CLOTHING, FOOD_RATE, CLOTHING_RATE, calendar, START, START + timedelta(days=10))
    assert q.billable_days == 3 and q.amount_cents == 15_000


def test_range_with_no_billable_day_is_refused():
    with pytest.raises(ValueError):
        quote(FOOD, FOOD_RATE, CLOTHING_RATE, NINETEEN_DAYS, START - timedelta(days=5), START - timedelta(days=1))


def test_reversed_range_is_refused():
    with pytest.raises(ValueError):
        billable_days_in_range(NINETEEN_DAYS, LAST, START)


def test_unknown_category_is_refused():
    with pytest.raises(ValueError):
        quote("JEWELLERY", FOOD_RATE, CLOTHING_RATE, NINETEEN_DAYS, START, LAST)


# ── Access ────────────────────────────────────────────────────────────────────

FIRST_FIVE = [PaidRange(START, START + timedelta(days=4))]


def test_setup_before_the_event_is_allowed_without_paying():
    assert has_access(START - timedelta(days=3), NINETEEN_DAYS, [])
    assert vendor_status(START - timedelta(days=3), NINETEEN_DAYS, []) == "PENDING"


def test_event_without_a_published_calendar_is_a_setup_window():
    assert has_access(START, set(), [])
    assert vendor_status(START, set(), []) == "PENDING"


def test_unpaid_billable_day_is_refused():
    assert not has_access(START, NINETEEN_DAYS, [])
    assert vendor_status(START, NINETEEN_DAYS, []) == "UNPAID"


def test_paid_day_is_allowed():
    assert has_access(START + timedelta(days=2), NINETEEN_DAYS, FIRST_FIVE)
    assert vendor_status(START + timedelta(days=2), NINETEEN_DAYS, FIRST_FIVE) == "ACTIVE"


def test_billable_day_after_paid_cover_ends_is_refused_and_expired():
    d = START + timedelta(days=5)
    assert not has_access(d, NINETEEN_DAYS, FIRST_FIVE)
    assert vendor_status(d, NINETEEN_DAYS, FIRST_FIVE) == "EXPIRED"


def test_unpaid_billable_gap_between_paid_ranges_is_refused():
    paid = [PaidRange(START, START), PaidRange(START + timedelta(days=4), START + timedelta(days=4))]
    gap = START + timedelta(days=2)
    assert not has_access(gap, NINETEEN_DAYS, paid)
    assert vendor_status(gap, NINETEEN_DAYS, paid) == "UNPAID"  # future cover exists, so not EXPIRED


def test_non_billable_day_between_paid_days_is_allowed():
    calendar = {START, START + timedelta(days=2)}  # the middle day is a closed day
    paid = [PaidRange(START, START + timedelta(days=2))]
    assert has_access(START + timedelta(days=1), calendar, paid)


def test_after_the_event_access_ends():
    after = LAST + timedelta(days=1)
    full = [PaidRange(START, LAST)]
    assert not has_access(after, NINETEEN_DAYS, full)
    assert vendor_status(after, NINETEEN_DAYS, full) == "EXPIRED"


def test_cancelled_vendor_is_always_cancelled():
    assert vendor_status(START, NINETEEN_DAYS, [PaidRange(START, LAST)], cancelled=True) == "CANCELLED"
