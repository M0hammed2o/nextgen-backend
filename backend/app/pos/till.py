"""
Till (cash-up) arithmetic — kept as pure functions so they're trivially
unit-testable without a database.
"""


def compute_expected_cash_cents(
    opening_float_cents: int, cash_sales_cents: int, cash_refunds_cents: int = 0,
) -> int:
    """
    Expected cash in the drawer at close = opening float + cash sales - cash
    refunds. Cash sales here must already be the SALE total, never the
    amount physically tendered by the customer — a R200 tender on a R45
    sale increases the drawer by R45, not R200 (the R155 change came back
    out of the same drawer). Cash-paid-in/paid-out ledger entries (Phase 14)
    are not modelled yet — see KNOWN_BUGS.md / TODO.md.
    """
    return opening_float_cents + cash_sales_cents - cash_refunds_cents


def compute_variance_cents(expected_cash_cents: int, counted_cash_cents: int) -> int:
    """Positive = over (more cash than expected), negative = short."""
    return counted_cash_cents - expected_cash_cents
