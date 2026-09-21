"""
Time & timezone utilities.
All DB timestamps are UTC. Business hours interpreted in business timezone.
"""

from datetime import datetime, time, timedelta, timezone

import zoneinfo


def utc_now() -> datetime:
    """Current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def to_business_tz(dt: datetime, tz_name: str) -> datetime:
    """Convert a UTC datetime to a business's local timezone."""
    tz = zoneinfo.ZoneInfo(tz_name)
    return dt.astimezone(tz)


def is_business_open(business_hours: dict, tz_name: str) -> bool:
    """
    Check if a business is currently open based on business_hours config.

    If business_hours is empty or not configured, the business is treated
    as always open (24/7). This prevents blocking all messages for new
    businesses that haven't set their hours yet.

    business_hours format:
    {
        "mon": {"open": "08:00", "close": "22:00"},
        "tue": {"open": "08:00", "close": "22:00"},
        ...
        "sun": null  // closed all day
    }
    """
    # No hours configured → treat as always open
    if not business_hours:
        return True

    now_local = to_business_tz(utc_now(), tz_name)
    day_key = now_local.strftime("%a").lower()[:3]

    day_hours = business_hours.get(day_key)
    if not day_hours:
        return False

    open_time = time.fromisoformat(day_hours["open"])
    close_time = time.fromisoformat(day_hours["close"])
    current_time = now_local.time()

    # Handle overnight hours (e.g., open 18:00, close 02:00)
    if close_time < open_time:
        return current_time >= open_time or current_time <= close_time

    return open_time <= current_time <= close_time


def today_date_for_business(tz_name: str):
    """Get today's date in the business's timezone (for daily_usage keying)."""
    now_local = to_business_tz(utc_now(), tz_name)
    return now_local.date()


def local_midnight_to_utc(local_date, tz_name: str) -> datetime:
    """
    Convert a business-local calendar date's midnight into a real UTC
    instant, for comparing against UTC-stored timestamps (e.g.
    `Order.created_at >= local_midnight_to_utc(today, tz_name)`).

    BUG-0032: a previous version of this logic (duplicated inline in
    `routes_analytics.py`) built `datetime(y, m, d, tzinfo=timezone.utc)`
    directly from the local date's own year/month/day -- correct only for
    UTC-offset-zero timezones. For any positive-offset timezone (e.g. the
    platform default "Africa/Johannesburg", UTC+2), that shifted the real
    cutoff 2 hours LATER than true local midnight, silently excluding
    every order placed in the first 2 hours of each local day from
    "today"'s analytics. Construct midnight IN the business's own
    timezone first, then convert -- not the other way around.
    """
    local_midnight = datetime(
        local_date.year, local_date.month, local_date.day,
        tzinfo=zoneinfo.ZoneInfo(tz_name),
    )
    return local_midnight.astimezone(timezone.utc)


def end_of_local_day_utc(local_date, tz_name: str) -> datetime:
    """
    The last instant (23:59:59.999999) of `local_date` in the business's own
    timezone, expressed as a UTC datetime — used for "trial ends at the end
    of the local day N days from now" (commercial trials/invoices, Wave 0A).

    Computed as (start of next local day) minus 1 microsecond rather than
    constructing 23:59:59.999999 directly, so it inherits the same
    correct-DST-and-offset handling as `local_midnight_to_utc` instead of
    re-implementing it.
    """
    start_of_next_day = local_midnight_to_utc(local_date + timedelta(days=1), tz_name)
    return start_of_next_day - timedelta(microseconds=1)
