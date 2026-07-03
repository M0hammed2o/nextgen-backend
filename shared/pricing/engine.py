"""
Unified Pricing Engine — single source of truth for all price calculations.

Every workflow (WhatsApp bot, Staff App, Manager App, Admin, future POS)
must call these functions. No endpoint or frontend calculates totals independently.

Pipeline per line item:
  base_price_cents
  + option_adjustment_cents   (sum of selected option price_delta_cents, may be negative)
  + add_on_total_cents        (sum of add-on price_cents × quantity, always >= 0)
  = unit_price_cents          (clamped to 0, never negative)
  × quantity
  = line_total_cents
"""

from dataclasses import dataclass


@dataclass
class SelectedOption:
    """One choice from an option group (e.g. 'Oat Milk' from 'Milk Type')."""
    group_id: str
    group_name: str
    option_id: str
    option_name: str
    price_delta_cents: int   # signed: 0 = no change, +1000 = +R10, -500 = -R5


@dataclass
class SelectedAddOn:
    """A paid add-on applied to a line item (e.g. 'Extra Cheese × 2')."""
    add_on_id: str
    name: str
    price_cents: int     # per unit; always >= 0
    quantity: int        # number of units selected


@dataclass
class LineItemBreakdown:
    """Full price breakdown for one cart line item."""
    base_price_cents: int
    option_adjustment_cents: int   # may be negative
    add_on_total_cents: int        # always >= 0
    unit_price_cents: int          # computed, always >= 0
    quantity: int
    line_total_cents: int          # unit_price_cents × quantity


def _option_delta(o) -> int:
    """Extract price_delta_cents from a typed SelectedOption or plain dict."""
    if isinstance(o, SelectedOption):
        return o.price_delta_cents
    return int(o.get("price_delta_cents", 0))


def _add_on_cost(a) -> int:
    """Extract total cost (price × qty) from a typed SelectedAddOn or plain dict."""
    if isinstance(a, SelectedAddOn):
        return a.price_cents * a.quantity
    return int(a.get("price_cents", 0)) * int(a.get("quantity", 1))


def calculate_unit_price(
    base_price_cents: int,
    selected_options: list,
    add_ons: list,
) -> int:
    """
    Compute the effective per-unit price.

    Accepts both typed dataclasses and plain dicts for each list entry.
    Result is clamped to 0 — a unit price can never be negative.
    """
    option_adjustment = sum(_option_delta(o) for o in selected_options)
    add_on_total = sum(_add_on_cost(a) for a in add_ons)
    return max(0, base_price_cents + option_adjustment + add_on_total)


def calculate_line_item(
    base_price_cents: int,
    selected_options: list,
    add_ons: list,
    quantity: int,
) -> LineItemBreakdown:
    """
    Full breakdown for one cart line item.
    Returns a LineItemBreakdown with every pricing component isolated.
    """
    option_adjustment = sum(_option_delta(o) for o in selected_options)
    add_on_total = sum(_add_on_cost(a) for a in add_ons)
    unit_price = max(0, base_price_cents + option_adjustment + add_on_total)
    return LineItemBreakdown(
        base_price_cents=base_price_cents,
        option_adjustment_cents=option_adjustment,
        add_on_total_cents=add_on_total,
        unit_price_cents=unit_price,
        quantity=quantity,
        line_total_cents=unit_price * quantity,
    )
