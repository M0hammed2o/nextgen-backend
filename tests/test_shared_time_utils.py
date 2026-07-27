"""
shared/utils/time.py -- previously had zero test coverage, which is exactly
how BUG-0032 (analytics cutoff shifted by the business's UTC offset) went
unnoticed. Focused on local_midnight_to_utc(), the function that fix
introduced, plus a basic sanity check on the pre-existing helpers it builds on.
"""
from datetime import date, datetime, timezone

from shared.utils.time import local_midnight_to_utc, to_business_tz, today_date_for_business


def test_local_midnight_to_utc_positive_offset_timezone():
    # Africa/Johannesburg is UTC+2 year-round (no DST) -- local midnight on
    # 2026-07-27 must be 22:00 UTC on 2026-07-26, not "2026-07-27 00:00 UTC"
    # (the exact bug: naively tagging the local date's y/m/d with UTC tzinfo).
    result = local_midnight_to_utc(date(2026, 7, 27), "Africa/Johannesburg")
    assert result == datetime(2026, 7, 26, 22, 0, 0, tzinfo=timezone.utc)


def test_local_midnight_to_utc_negative_offset_timezone():
    # America/New_York is UTC-5 (EST, outside DST) -- local midnight on
    # 2026-01-15 must be 05:00 UTC on the SAME date, not shifted the other way.
    result = local_midnight_to_utc(date(2026, 1, 15), "America/New_York")
    assert result == datetime(2026, 1, 15, 5, 0, 0, tzinfo=timezone.utc)


def test_local_midnight_to_utc_utc_timezone_is_a_no_op():
    result = local_midnight_to_utc(date(2026, 3, 1), "UTC")
    assert result == datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_an_order_just_after_local_midnight_is_on_or_after_the_cutoff():
    # This is the exact real-world symptom BUG-0032 caused: an order created
    # at 00:05 local time (Africa/Johannesburg) on 2026-07-27 must count as
    # "today" -- i.e. its UTC timestamp must be >= today's cutoff.
    order_created_at_utc = datetime(2026, 7, 26, 22, 5, 0, tzinfo=timezone.utc)  # 00:05 SAST on 07-27
    cutoff = local_midnight_to_utc(date(2026, 7, 27), "Africa/Johannesburg")
    assert order_created_at_utc >= cutoff


def test_today_date_for_business_returns_a_plain_date():
    result = today_date_for_business("Africa/Johannesburg")
    assert isinstance(result, date)


def test_to_business_tz_converts_utc_to_local():
    utc_dt = datetime(2026, 7, 26, 22, 5, 0, tzinfo=timezone.utc)
    local_dt = to_business_tz(utc_dt, "Africa/Johannesburg")
    assert local_dt.hour == 0
    assert local_dt.day == 27
