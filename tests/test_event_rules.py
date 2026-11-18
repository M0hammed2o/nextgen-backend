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
    CONFIGURE,
    NONE,
    TRADE,
    access_level,
    best_decision,
    billable_days_in_range,
    evaluate_access,
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


def test_unpaid_billable_day_cannot_trade_but_may_configure():
    # Phase 1 (owner 2026-11-18): was "refused" (suspended); now CONFIGURE so the vendor can log in and pay.
    assert access_level(START, NINETEEN_DAYS, []) == CONFIGURE
    assert vendor_status(START, NINETEEN_DAYS, []) == "UNPAID"


def test_paid_day_is_allowed():
    assert has_access(START + timedelta(days=2), NINETEEN_DAYS, FIRST_FIVE)
    assert vendor_status(START + timedelta(days=2), NINETEEN_DAYS, FIRST_FIVE) == "ACTIVE"


def test_billable_day_after_paid_cover_ends_cannot_trade_and_reads_expired():
    d = START + timedelta(days=5)
    assert access_level(d, NINETEEN_DAYS, FIRST_FIVE) == CONFIGURE  # was NONE before Phase 1
    assert vendor_status(d, NINETEEN_DAYS, FIRST_FIVE) == "EXPIRED"


def test_unpaid_billable_gap_between_paid_ranges_cannot_trade():
    paid = [PaidRange(START, START), PaidRange(START + timedelta(days=4), START + timedelta(days=4))]
    gap = START + timedelta(days=2)
    assert access_level(gap, NINETEEN_DAYS, paid) == CONFIGURE  # was NONE before Phase 1
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


# ── Setup window (security review M3, 2026-11-18) ─────────────────────────────

from backend.app.events.rules import SETUP_WINDOW_DAYS  # noqa: E402


def test_setup_window_opens_exactly_14_days_before_the_first_billable_day():
    assert SETUP_WINDOW_DAYS == 14
    assert has_access(START - timedelta(days=14), NINETEEN_DAYS, [])
    assert not has_access(START - timedelta(days=15), NINETEEN_DAYS, [])


def test_far_future_registration_gets_no_free_access_and_reads_pending():
    d = START - timedelta(days=120)
    assert not has_access(d, NINETEEN_DAYS, [])
    assert vendor_status(d, NINETEEN_DAYS, []) == "PENDING"


def test_paying_early_does_not_open_access_before_the_window():
    """A vendor who paid for the whole event still cannot trade months ahead."""
    d = START - timedelta(days=60)
    paid = [PaidRange(START, LAST)]
    assert not has_access(d, NINETEEN_DAYS, paid)
    assert vendor_status(d, NINETEEN_DAYS, paid) == "PENDING"
    assert has_access(START - timedelta(days=3), NINETEEN_DAYS, paid)  # inside the window


def test_gap_day_inside_the_event_is_unaffected_by_the_window():
    calendar = {START, START + timedelta(days=2)}
    paid = [PaidRange(START, START + timedelta(days=2))]
    assert has_access(START + timedelta(days=1), calendar, paid)


# ── Access levels (Phase 1, owner 2026-11-18) ─────────────────────────────────

def test_trade_only_on_a_paid_billable_day():
    dec = evaluate_access(START + timedelta(days=2), NINETEEN_DAYS, FIRST_FIVE)
    assert (dec.level, dec.reason) == (TRADE, "PAID_DAY")


def test_no_calendar_is_configure_never_trade_even_with_paid_cover():
    paid = [PaidRange(START, LAST)]
    assert evaluate_access(START, set(), []).level == CONFIGURE
    dec = evaluate_access(START, set(), paid)
    assert (dec.level, dec.reason) == (CONFIGURE, "CALENDAR_NOT_PUBLISHED")


def test_non_billable_day_during_event_is_configure_even_inside_a_paid_range():
    calendar = {START, START + timedelta(days=2)}
    paid = [PaidRange(START, START + timedelta(days=2))]
    dec = evaluate_access(START + timedelta(days=1), calendar, paid)
    assert (dec.level, dec.reason) == (CONFIGURE, "NOT_AN_EVENT_DAY")


def test_unpaid_billable_day_reason():
    assert evaluate_access(START, NINETEEN_DAYS, []).reason == "NO_PAID_COVER_TODAY"


def test_prep_window_is_configure_and_never_trade_even_when_paid():
    paid = [PaidRange(START, LAST)]
    dec = evaluate_access(START - timedelta(days=1), NINETEEN_DAYS, paid)
    assert (dec.level, dec.reason) == (CONFIGURE, "PREP_WINDOW")


def test_before_prep_window_and_after_event_are_none():
    assert evaluate_access(START - timedelta(days=15), NINETEEN_DAYS, []).level == NONE
    dec = evaluate_access(LAST + timedelta(days=1), NINETEEN_DAYS, [PaidRange(START, LAST)])
    assert (dec.level, dec.reason) == (NONE, "EVENT_ENDED")


def test_cancelled_is_none_even_on_a_paid_day():
    assert evaluate_access(START, NINETEEN_DAYS, [PaidRange(START, LAST)], cancelled=True).level == NONE


def test_prep_days_is_configurable_per_event():
    assert access_level(START - timedelta(days=30), NINETEEN_DAYS, [], prep_days=30) == CONFIGURE
    assert access_level(START - timedelta(days=31), NINETEEN_DAYS, [], prep_days=30) == NONE
    assert access_level(START - timedelta(days=1), NINETEEN_DAYS, [], prep_days=0) == NONE
    assert access_level(START, NINETEEN_DAYS, [], prep_days=0) == CONFIGURE
    assert vendor_status(START - timedelta(days=5), NINETEEN_DAYS, [], prep_days=3) == "PENDING"


def test_best_decision_takes_the_highest_level():
    low = evaluate_access(START - timedelta(days=60), NINETEEN_DAYS, [])
    mid = evaluate_access(START, NINETEEN_DAYS, [])
    top = evaluate_access(START, NINETEEN_DAYS, [PaidRange(START, START)])
    assert best_decision([low, mid, top]).level == TRADE
    assert best_decision([top, low]).level == TRADE
    assert best_decision([low, mid]).level == CONFIGURE
    assert best_decision([]).level == NONE
