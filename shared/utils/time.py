"""
Time & timezone utilities.
All DB timestamps are UTC. Business hours interpreted in business timezone.
"""

from datetime import datetime, time, timezone

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
