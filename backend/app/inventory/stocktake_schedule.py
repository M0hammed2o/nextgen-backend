"""
Stocktake cadence: is this business overdue for a count, given its own
`stocktake_frequency` / `stocktake_times_json` configuration?

Read-only surfacing only — this never blocks a count (a restaurant that
needs to recount at 3pm must always be able to, per shared/models/business.py's
own comment on the schedule columns) and it sends no notifications; it only
answers one question for a dashboard to display.

REAL FREQUENCY VALUES (verified against shared/models/business.py and
backend/app/api/v1/routes_business.py's own validator — the only two places
that define/enforce this enum): DAILY_1, DAILY_2, DAILY_3, DAILY_4, CUSTOM.
There is no WEEKLY value anywhere in this codebase. `stocktake_times_json`
is a list of local "HH:MM" times and, per the model's own comment, is used
by BOTH DAILY_n (which holds its n chosen times) and CUSTOM (up to 12
arbitrary times) — every frequency in this schema is a same-day schedule,
not a weekly one.

CADENCE RULE (documented judgement call — see "no stocktake ever" below)
--------------------------------------------------------------------------
A business is overdue if its most recent completed count happened before
the most recently-passed scheduled deadline:

  * If `stocktake_times_json` has one or more local times, each one that
    has already occurred TODAY (business-local) is a deadline; the overdue
    check uses the latest one that has passed. If none has occurred yet
    today (e.g. it's 06:00 and the first count isn't due until 09:00), the
    check falls back to YESTERDAY's last scheduled time, so a business
    isn't wrongly flagged overdue before its first slot of the day has even
    arrived.
  * If no times are configured (a fresh DAILY_n business that hasn't picked
    specific times, or a CUSTOM business mid-setup), the deadline is simply
    local midnight today — i.e. "at least one count per calendar day".

"No stocktake ever" (documented judgement call): rather than a bare True/
False, this is resolved by using `business.created_at` in place of a missing
`last_completed_stocktake_at`. A business created after the deadline hasn't
had a real chance to count yet and is NOT flagged overdue; a business that
existed before the deadline and has still never counted IS overdue. This
avoids showing "overdue" to a restaurant on its very first day while still
correctly flagging one that has genuinely never done a count.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone as dt_timezone

import zoneinfo

from shared.utils.time import local_midnight_to_utc, to_business_tz, utc_now

DEFAULT_TIMEZONE = "Africa/Johannesburg"


def _parse_hhmm(value: str) -> time:
    hh_str, mm_str = value.split(":")
    return time(int(hh_str), int(mm_str))


def _most_recent_deadline_utc(
    tz_name: str,
    times_json: list[str] | None,
    now: datetime,
) -> datetime:
    """
    The most recent scheduled cadence deadline that has already passed, as
    a real UTC instant.
    """
    tz = zoneinfo.ZoneInfo(tz_name)
    now_local = to_business_tz(now, tz_name)
    today_local = now_local.date()

    times = sorted(times_json) if times_json else []
    if not times:
        # No specific times configured — plain "once per calendar day" rule.
        return local_midnight_to_utc(today_local, tz_name)

    passed_today = [
        datetime.combine(today_local, _parse_hhmm(t), tzinfo=tz)
        for t in times
        if datetime.combine(today_local, _parse_hhmm(t), tzinfo=tz) <= now_local
    ]
    if passed_today:
        return max(passed_today).astimezone(dt_timezone.utc)

    # None of today's scheduled times has arrived yet — judge against
    # yesterday's last scheduled time instead.
    yesterday_local = today_local - timedelta(days=1)
    last_time = _parse_hhmm(max(times))
    deadline_local = datetime.combine(yesterday_local, last_time, tzinfo=tz)
    return deadline_local.astimezone(dt_timezone.utc)


def is_stocktake_overdue(
    business,
    last_completed_stocktake_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """
    True if `business` has missed its own configured stocktake cadence.

    `business` needs `.stocktake_frequency` (informational only — the real
    driver is `.stocktake_times_json`, see module docstring), `.timezone`,
    and `.created_at`. `last_completed_stocktake_at` should be the
    `completed_at` of the most recent APPROVED `StocktakeSession` (APPROVED
    because it's the one-way-final state; `completed_at` rather than
    `approved_at` because it's when the physical count actually happened,
    not whatever later moment a manager got around to signing off on it) —
    pass None if this business has never completed one.
    """
    now = now or utc_now()
    tz_name = business.timezone or DEFAULT_TIMEZONE

    deadline = _most_recent_deadline_utc(tz_name, business.stocktake_times_json, now)

    reference_at = last_completed_stocktake_at or getattr(business, "created_at", None)
    if reference_at is None:
        # No count history and no creation timestamp to fall back on —
        # can't establish "hasn't had a chance yet", so default to overdue.
        return True

    return reference_at < deadline
