"""login_opens_on on the public registration answer (souk e2e finding 2026-11-19:
'You can log in from 05 Nov 2026' when login was already open)."""
from datetime import date, timedelta

from backend.app.api.v1.routes_event_public import login_opens_on

TODAY = date(2026, 11, 19)


def test_future_prep_window_returns_the_opening_day():
    first = TODAY + timedelta(days=30)
    assert login_opens_on(TODAY, {first, first + timedelta(days=1)}, 14) == first - timedelta(days=14)


def test_window_opening_today_returns_none():
    assert login_opens_on(TODAY, {TODAY + timedelta(days=14)}, 14) is None


def test_window_already_open_returns_none_not_a_past_date():
    assert login_opens_on(TODAY, {TODAY}, 14) is None
    assert login_opens_on(TODAY, {TODAY - timedelta(days=3), TODAY + timedelta(days=2)}, 14) is None


def test_no_billable_days_returns_none():
    assert login_opens_on(TODAY, set(), 14) is None
