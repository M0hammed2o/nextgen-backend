"""
backend/app/inventory/stocktake_schedule.py::is_stocktake_overdue

Real frequency values are DAILY_1..DAILY_4 and CUSTOM (verified against
shared/models/business.py and routes_business.py's own field_validator —
there is no WEEKLY value in this codebase, unlike an earlier assumption).
Every frequency is a same-day schedule; `stocktake_times_json` (used by
both DAILY_n and CUSTOM) is what actually drives precise deadlines.
"""
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.inventory.stocktake_schedule import is_stocktake_overdue

TZ = "Africa/Johannesburg"  # UTC+2, no DST — deterministic for tests


def make_business(frequency="DAILY_1", times=None, created_days_ago=365, timezone_name=TZ):
    return types.SimpleNamespace(
        stocktake_frequency=frequency,
        stocktake_times_json=times,
        timezone=timezone_name,
        created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago),
    )


# Fixed "now": 2026-09-22 14:00 UTC == 16:00 Africa/Johannesburg.
NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)


class TestNoTimesConfigured:
    """DAILY_n / CUSTOM with no stocktake_times_json set — plain day-boundary rule."""

    def test_completed_today_is_not_overdue(self):
        business = make_business(frequency="DAILY_1", times=None)
        last = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)  # today, before `now`
        assert is_stocktake_overdue(business, last, now=NOW) is False

    def test_completed_ten_days_ago_is_overdue(self):
        business = make_business(frequency="DAILY_2", times=None)
        last = NOW - timedelta(days=10)
        assert is_stocktake_overdue(business, last, now=NOW) is True

    def test_completed_yesterday_is_overdue(self):
        business = make_business(frequency="DAILY_4", times=None)
        last = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)  # 12:00 local on the 21st — yesterday
        assert is_stocktake_overdue(business, last, now=NOW) is True

    def test_never_counted_but_business_created_today_is_not_overdue(self):
        """A restaurant on day one hasn't had a real chance to count yet."""
        business = make_business(frequency="DAILY_1", times=None, created_days_ago=0)
        assert is_stocktake_overdue(business, None, now=NOW) is False

    def test_never_counted_and_business_is_old_is_overdue(self):
        business = make_business(frequency="DAILY_1", times=None, created_days_ago=365)
        assert is_stocktake_overdue(business, None, now=NOW) is True

    def test_no_history_and_no_created_at_defaults_to_overdue(self):
        business = types.SimpleNamespace(
            stocktake_frequency="DAILY_1", stocktake_times_json=None, timezone=TZ,
        )
        assert is_stocktake_overdue(business, None, now=NOW) is True


class TestCustomWithTimes:
    """CUSTOM (or DAILY_n) with explicit stocktake_times_json — time-of-day precision."""

    def test_last_count_after_todays_only_passed_time_is_not_overdue(self):
        # Scheduled 09:00 and 15:00 local; now is 16:00 local — both passed.
        business = make_business(frequency="CUSTOM", times=["09:00", "15:00"])
        last = datetime(2026, 9, 22, 13, 5, tzinfo=timezone.utc)  # 15:05 local, after 15:00 deadline
        assert is_stocktake_overdue(business, last, now=NOW) is False

    def test_last_count_before_latest_passed_time_is_overdue(self):
        business = make_business(frequency="CUSTOM", times=["09:00", "15:00"])
        last = datetime(2026, 9, 22, 6, 30, tzinfo=timezone.utc)  # 08:30 local, before 15:00 deadline
        assert is_stocktake_overdue(business, last, now=NOW) is True

    def test_before_first_scheduled_time_of_day_falls_back_to_yesterday(self):
        # now = 04:00 local (02:00 UTC) — before today's only 09:00 slot.
        early_now = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
        business = make_business(frequency="DAILY_1", times=["09:00"])
        # Counted yesterday at 09:30 local — satisfies yesterday's 09:00 deadline.
        last = datetime(2026, 9, 21, 7, 30, tzinfo=timezone.utc)
        assert is_stocktake_overdue(business, last, now=early_now) is False

    def test_before_first_scheduled_time_missed_yesterday_is_overdue(self):
        early_now = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
        business = make_business(frequency="DAILY_1", times=["09:00"])
        last = datetime(2026, 9, 20, 7, 30, tzinfo=timezone.utc)  # two days ago
        assert is_stocktake_overdue(business, last, now=early_now) is True


class TestTimezoneHandling:
    def test_uses_business_own_timezone_not_utc(self):
        # 23:30 UTC on 2026-09-21 is already 01:30 local (Africa/Johannesburg,
        # UTC+2) on 2026-09-22 — "today" must be computed in local time.
        now_late_utc = datetime(2026, 9, 21, 23, 30, tzinfo=timezone.utc)
        business = make_business(frequency="DAILY_1", times=None)
        last = datetime(2026, 9, 22, 0, 30, tzinfo=timezone.utc)  # 02:30 local on the 22nd
        # "Today" locally is the 22nd; midnight-22nd-local is 2026-09-21 22:00 UTC.
        assert is_stocktake_overdue(business, last, now=now_late_utc) is False

    def test_missing_timezone_falls_back_to_default(self):
        business = make_business(frequency="DAILY_1", times=None, timezone_name=None)
        last = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
        assert is_stocktake_overdue(business, last, now=NOW) is False
