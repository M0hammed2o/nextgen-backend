"""
Pure event rules: no database, no clock. Everything that decides money or
access for an event vendor lives here, so it can be tested exhaustively.

Owner decisions (2026-11-18):
- Food vendors R100 and clothing vendors R50 per *subscribed* day, VAT-inclusive.
- A subscribed day is a billable event day, whether or not the vendor trades.

Access rule, for a vendor on local date ``D`` (the event's timezone):

    access(D) = covered(D)
             or (not billable(D) and (has_future_paid(D) or D < first_billable_day))

- ``covered(D)``: a PAID subscription has starts_on <= D <= ends_on.
- A billable day with no paid cover is never accessible. Nobody trades at an
  event day they have not paid for.
- Non-billable days before the event, or between paid days, stay accessible
  so a vendor can register and set up products in advance.
- After the last paid day, with nothing further paid, access ends.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

FOOD = "FOOD"
CLOTHING = "CLOTHING"


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


def has_access(d: date, billable_days: set[date], paid: list[PaidRange]) -> bool:
    if any(p.covers(d) for p in paid):
        return True
    if d in billable_days:
        return False
    has_future_paid = any(p.starts_on > d for p in paid)
    # An event whose calendar has not been published yet (no billable days)
    # is entirely a setup window: vendors can register and load products.
    before_event = (not billable_days) or d < min(billable_days)
    return has_future_paid or before_event


def vendor_status(d: date, billable_days: set[date], paid: list[PaidRange], cancelled: bool = False) -> str:
    """The EventVendorStatus value the vendor should have on date ``d``."""
    if cancelled:
        return "CANCELLED"
    covered_or_future = any(p.covers(d) or p.starts_on > d for p in paid)
    if has_access(d, billable_days, paid):
        return "ACTIVE" if covered_or_future else "PENDING"
    if paid:
        # Paid cover existed. Either it ended, or this is an unpaid billable
        # day between paid ranges; with no future cover it has expired.
        return "UNPAID" if covered_or_future else "EXPIRED"
    return "UNPAID"
