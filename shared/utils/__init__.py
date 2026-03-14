"""
ID generation utilities.
- UUIDs for primary keys
- Business codes (6-char alphanumeric)
- Human-friendly order numbers
"""

import secrets
import string
import uuid


def generate_uuid() -> uuid.UUID:
    """Generate a new UUID v4."""
    return uuid.uuid4()


def generate_business_code(length: int = 6) -> str:
    """
    Generate a 6-character alphanumeric business code (uppercase).
    Example: 'B7K3Q9'
    Caller must check uniqueness before saving.
    """
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def format_order_number(sequence: int, prefix: str = "BO") -> str:
    """
    Format a human-friendly order number from a sequence integer.
    Example: format_order_number(123) -> 'BO-000123'
    """
    return f"{prefix}-{sequence:06d}"


def generate_refresh_token() -> str:
    """Generate a cryptographically secure refresh token string."""
    return secrets.token_urlsafe(48)
