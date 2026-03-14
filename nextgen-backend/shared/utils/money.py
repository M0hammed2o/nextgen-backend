"""
Money utilities — all monetary values stored as integer cents.
Never use floats for money.
"""


def to_cents(rands: float) -> int:
    """Convert a rand amount to cents. Use only for user input parsing."""
    return round(rands * 100)


def to_rands(cents: int) -> float:
    """Convert cents to rands for display. Returns float with 2 decimals."""
    return cents / 100.0


def format_zar(cents: int) -> str:
    """Format cents as ZAR string. Example: 1550 -> 'R15.50'"""
    return f"R{cents / 100:.2f}"


def format_currency(cents: int, currency: str = "ZAR") -> str:
    """Format cents with currency symbol."""
    symbols = {"ZAR": "R", "USD": "$", "EUR": "€", "GBP": "£"}
    symbol = symbols.get(currency, currency + " ")
    return f"{symbol}{cents / 100:.2f}"


def calculate_line_total(unit_price_cents: int, quantity: int) -> int:
    """Calculate line total in cents."""
    if quantity < 0:
        raise ValueError("Quantity cannot be negative")
    return unit_price_cents * quantity
