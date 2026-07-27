"""
Till (cash-up) arithmetic — kept as pure functions so they're trivially
unit-testable without a database.
"""


def compute_expected_cash_cents(opening_float_cents: int, cash_sales_cents: int) -> int:
    """Expected cash in the drawer at close = opening float + cash-paid sales since open."""
    return opening_float_cents + cash_sales_cents


def compute_variance_cents(expected_cash_cents: int, counted_cash_cents: int) -> int:
    """Positive = over (more cash than expected), negative = short."""
    return counted_cash_cents - expected_cash_cents
