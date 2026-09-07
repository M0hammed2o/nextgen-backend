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


# ── VAT / tax ────────────────────────────────────────────────────────────────
#
# Added for the Ciao Pizza P0 (2026-09-07). Before this, routes_pos.py wrote
# a hardcoded tax_cents=0 with the comment "No tax-rate configuration exists
# yet", which meant a VAT-registered tenant's till slip was not a valid South
# African tax invoice.
#
# The rate is an integer in BASIS POINTS (15% = 1500) so no float ever
# touches a tax figure. Rounding is half-up, applied exactly once, at the
# end — never per line item, which would let rounding error accumulate
# across a large order.


def calculate_vat_cents(
    amount_cents: int,
    vat_rate_basis_points: int,
    vat_inclusive: bool = True,
) -> int:
    """
    VAT contained in (inclusive) or due on (exclusive) `amount_cents`.

    Inclusive — the South African norm, where menu prices already include
    VAT — back-calculates the tax out of the gross amount:

        vat = amount * rate / (10000 + rate)
        R115 at 15% -> 1500 cents of VAT, gross stays R115

    Exclusive adds it on top:

        vat = amount * rate / 10000
        R100 at 15% -> 1500 cents of VAT, gross becomes R115

    Returns 0 for a zero/negative rate or a non-positive amount.
    """
    from decimal import Decimal, ROUND_HALF_UP

    rate = int(vat_rate_basis_points or 0)
    if rate <= 0 or amount_cents <= 0:
        return 0

    gross = Decimal(int(amount_cents))
    if vat_inclusive:
        exact = gross * Decimal(rate) / (Decimal(10_000) + Decimal(rate))
    else:
        exact = gross * Decimal(rate) / Decimal(10_000)

    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def apply_vat_to_total(
    net_total_cents: int,
    vat_rate_basis_points: int,
    vat_inclusive: bool = True,
) -> tuple[int, int]:
    """
    Resolve an order total and its VAT component together.

    Returns ``(total_cents, vat_cents)``:

      * inclusive — the total is unchanged and VAT is the portion inside it,
        so switching VAT on never silently reprices a tenant's whole menu.
      * exclusive — VAT is added on top and the total grows.

    This asymmetry is the entire reason the two modes exist, and it is why
    the caller must use the returned total rather than recomputing one.
    """
    vat_cents = calculate_vat_cents(net_total_cents, vat_rate_basis_points, vat_inclusive)
    if vat_inclusive:
        return net_total_cents, vat_cents
    return net_total_cents + vat_cents, vat_cents
