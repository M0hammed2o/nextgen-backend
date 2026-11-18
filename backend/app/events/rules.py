"""
Pure event rules: no database, no clock. Everything that decides money or
access for an event vendor lives here, so it can be tested exhaustively.

Owner decisions (2026-11-18):
- Food vendors R100 and clothing vendors R50 per *subscribed* day, VAT-inclusive.
- A subscribed day is a billable event day, whether or not the vendor trades.
- Real transactions need a PAID subscription covering the current trading date.

Three access levels for a vendor on local date ``D`` (the event's timezone),
with ``first``/``last`` the first/last billable day and ``prep_days`` the
event's preparation period:

    NONE       business suspended: D is before ``first - prep_days``; D is after
               ``last``; or the vendor/event is cancelled.
    CONFIGURE  may log in and set up products, stock, staff and images, but may
               NOT trade: inside the prep window before ``first``; the calendar
               is not published; an unpaid billable day during the event; a
               non-billable day during the event.
    TRADE      D is a billable day covered by a PAID subscription.

A business at several events takes the highest level.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

FOOD = "FOOD"
CLOTHING = "CLOTHING"

# Default preparation period (days before the first billable day during which a
# vendor may log in and configure). Per-event override: events.prep_days.
# Security review M3: a far-future registration gets no early access.
DEFAULT_PREP_DAYS = 14
SETUP_WINDOW_DAYS = DEFAULT_PREP_DAYS  # backwards-compatible alias

NONE = "NONE"
CONFIGURE = "CONFIGURE"
TRADE = "TRADE"
_RANK = {NONE: 0, CONFIGURE: 1, TRADE: 2}


@dataclass(frozen=True)
class AccessDecision:
    level: str
    reason: str


@dataclass(frozen=True)
class PaidRange:
    starts_on: date
    ends_on: date

    def covers(self, d: date) -> bool:
        return self.starts_on <= d <= self.ends_on


@dataclass(frozen=True)
class Quote:
    day_rate_cents: int
    billable_days: int
    amount_cents: int


def day_rate_for(category: str, food_day_rate_cents: int, clothing_day_rate_cents: int) -> int:
    if category == FOOD:
        return food_day_rate_cents
    if category == CLOTHING:
        return clothing_day_rate_cents
    raise ValueError(f"unknown vendor category {category!r}")


def billable_days_in_range(billable_days: Iterable[date], starts_on: date, ends_on: date) -> int:
    if ends_on < starts_on:
        raise ValueError("ends_on is before starts_on")
    return sum(1 for d in set(billable_days) if starts_on <= d <= ends_on)


def quote(
    category: str,
    food_day_rate_cents: int,
    clothing_day_rate_cents: int,
    billable_days: Iterable[date],
    starts_on: date,
    ends_on: date,
) -> Quote:
    """Price a subscription for [starts_on, ends_on]. Only billable event days are charged."""
    rate = day_rate_for(category, food_day_rate_cents, clothing_day_rate_cents)
    days = billable_days_in_range(billable_days, starts_on, ends_on)
    if days == 0:
        raise ValueError("the range contains no billable event day")
    return Quote(day_rate_cents=rate, billable_days=days, amount_cents=rate * days)


def evaluate_access(
    d: date,
    billable_days: set[date],
    paid: list[PaidRange],
    prep_days: int = DEFAULT_PREP_DAYS,
    cancelled: bool = False,
) -> AccessDecision:
    """The access level and a machine-readable reason for date ``d``."""
    if cancelled:
        return AccessDecision(NONE, "CANCELLED")
    if not billable_days:
        return AccessDecision(CONFIGURE, "CALENDAR_NOT_PUBLISHED")
    first, last = min(billable_days), max(billable_days)
    if d < first - timedelta(days=prep_days):
        return AccessDecision(NONE, "BEFORE_PREP_WINDOW")
    if d < first:
        return AccessDecision(CONFIGURE, "PREP_WINDOW")
    if d > last:
        return AccessDecision(NONE, "EVENT_ENDED")
    if d not in billable_days:
        return AccessDecision(CONFIGURE, "NOT_AN_EVENT_DAY")
    if any(p.covers(d) for p in paid):
        return AccessDecision(TRADE, "PAID_DAY")
    return AccessDecision(CONFIGURE, "NO_PAID_COVER_TODAY")


def best_decision(decisions: Iterable[AccessDecision]) -> AccessDecision:
    """Highest level wins (first one on a tie). No decisions at all means NONE."""
    best = AccessDecision(NONE, "NO_EVENT")
    for dec in decisions:
        if _RANK[dec.level] > _RANK[best.level] or best.reason == "NO_EVENT":
            best = dec
    return best


def access_level(d: date, billable_days: set[date], paid: list[PaidRange],
                 prep_days: int = DEFAULT_PREP_DAYS, cancelled: bool = False) -> str:
    return evaluate_access(d, billable_days, paid, prep_days, cancelled).level


def has_access(d: date, billable_days: set[date], paid: list[PaidRange],
               prep_days: int = DEFAULT_PREP_DAYS) -> bool:
    """May the vendor log in at all (CONFIGURE or TRADE)?"""
    return access_level(d, billable_days, paid, prep_days) != NONE


def vendor_status(d: date, billable_days: set[date], paid: list[PaidRange], cancelled: bool = False,
                  prep_days: int = DEFAULT_PREP_DAYS) -> str:
    """The EventVendorStatus value the vendor should have on date ``d``."""
    if cancelled:
        return "CANCELLED"
    dec = evaluate_access(d, billable_days, paid, prep_days)
    covered_or_future = any(p.covers(d) or p.starts_on > d for p in paid)
    if dec.level == TRADE:
        return "ACTIVE"
    if dec.reason == "BEFORE_PREP_WINDOW":
        return "PENDING"  # registered (paid or not); the prep window has not opened
    if dec.level == CONFIGURE:
        if dec.reason in ("PREP_WINDOW", "CALENDAR_NOT_PUBLISHED"):
            return "ACTIVE" if covered_or_future else "PENDING"
        # During the event, not trading today.
        if dec.reason == "NOT_AN_EVENT_DAY" and covered_or_future:
            return "ACTIVE"  # a closed day between paid days
        # An unpaid billable day: UNPAID while later cover exists (or none was
        # ever bought), EXPIRED once all paid cover is behind us.
        return "EXPIRED" if paid and not any(p.starts_on > d for p in paid) else "UNPAID"
    # NONE after the event
    return "EXPIRED" if paid else "UNPAID"
